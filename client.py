from __future__ import annotations

import ctypes
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import atexit  # Registramos callbacks de limpieza para cerrar recursos IPC de consola aunque el proceso termine abruptamente.
import errno  # Interpretamos códigos de error de sockets al intentar abrir el canal IPC sin colisionar con instancias previas.
import msvcrt  # Ajustamos el modo texto de los descriptores CONIN$/CONOUT$ tras adjuntar la consola de Windows al proceso actual.
import socket  # Implementamos un canal TCP local mínimo para que la segunda instancia solicite la unión de la consola al proceso residente.
import sys  # Redirigimos sys.stdout/sys.stderr/sys.stdin hacia los manejadores de la consola recién obtenida o hacia el fallback.
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

import keyboard  # type: ignore
import psutil
import pyautogui  # type: ignore
import pygetwindow as gw  # type: ignore
import speech_recognition as sr

import hud
from hud import log
# Comentario general del cliente:
# - Esta unidad orquesta la interacción entre reconocimiento de voz, atajos de teclado,
#   control de ventanas y el HUD para feedback al usuario.
# - También resuelve apertura/cierre de aplicaciones consultando un catálogo JSON.
# - Cuando es necesario, usa Cohere para sintetizar comandos exactos y los registra.

try:
    import cohere
except Exception:  # pragma: no cover - dependencia opcional
    cohere = None
    # Cohere es opcional. Si no está instalado o la importación falla,
    # las funciones generativas retornarán None y el sistema seguirá operativo.


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rai.client")
usuario = os.getlogin()
texto_acumulado = ""
CATALOGO_PATH = Path(__file__).with_name("apps.json")
COHERE_LOG_PATH = Path(__file__).with_name("cohere.log")
CLIENT_LOG_PATH = Path(__file__).with_name("client.log")  # Archivo de log persistente cuyo tail mostramos al adjuntar la consola para contextualizar al operador.
CONSOLE_SERVER_HOST = "127.0.0.1"  # Limitamos el servidor IPC de consola al loopback para impedir peticiones remotas que no pertenecen a esta máquina.
CONSOLE_SERVER_PORT = 57317  # Puerto TCP fijo elegido adrede para detectar colisiones y coordinar a la instancia mensajera con el proceso residente.
CONSOLE_SERVER_TOKEN = "rai-mini-console-v1"  # Token estático sencillo que filtra peticiones accidentales y asegura que el comando proviene de nuestro cliente.
_console_server_socket: Optional[socket.socket] = None  # Guardamos la referencia del socket de escucha para cerrarlo explícitamente en la rutina de apagado.
_console_server_thread: Optional[threading.Thread] = None  # Conservamos el hilo del bucle de aceptación para evitar fugas y facilitar el join implícito al terminar.
_console_stream_refs: Dict[str, Any] = {}  # Retenemos los file objects de CONIN$/CONOUT$ para impedir que el GC los cierre y mantener vivos los flujos redirigidos.
_client_file_handler: Optional[logging.Handler] = None  # Handler global que persistirá durante toda la vida del proceso para escribir el historial en client.log sin duplicar instancias al reiniciar la app.
try:
    CLIENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)  # Creamos la carpeta del log si aún no existe, evitando excepciones al inicializar el FileHandler.
    _client_file_handler = logging.FileHandler(CLIENT_LOG_PATH, mode="a", encoding="utf-8")  # Abrimos el archivo en modo append para conservar el historial previo cada vez que arranca el cliente.
    _client_file_handler.setLevel(logging.INFO)  # Sincronizamos el nivel del handler con el resto del sistema (INFO) para capturar los mismos eventos que van a consola.
    _client_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))  # Reutilizamos el formato estándar de timestamp/nivel para mantener coherencia visual.
    logging.getLogger().addHandler(_client_file_handler)  # Registramos el handler en el logger raíz para que toda la jerarquía propague sus mensajes al archivo.
except Exception as exc:
    logger.error("No pude inicializar el file handler de client.log: %s", exc)  # Dejamos constancia en logs si la inicialización falló, manteniendo al usuario informado.
catalogo_lock = threading.Lock()
_catalogo_cache: Optional[Dict[str, Any]] = None
COHERE_MODEL = os.getenv("COHERE_MODEL", "command-r-plus-08-2024")
COHERE_API_KEY = "ppBVjJhTQ1vCU7WVBKt1wYKpDUZW97LhZ1PrHsBJ"
_cohere_client: Optional["cohere.Client"] = None
historial_acciones: Deque[Dict[str, Any]] = deque(maxlen=5)
memoria_redaccion: Dict[str, Any] = {
    "texto": "",
    "solicitud": "",
    "instrucciones": [],
}
USER32 = ctypes.windll.user32
KERNEL32 = ctypes.windll.kernel32
"""
Notas sobre configuración:
- CATALOGO_PATH: ruta del catálogo JSON con apps/acciones.
- COHERE_LOG_PATH: ruta donde se registran prompts y respuestas de Cohere para depuración.
- COHERE_API_KEY/COHERE_MODEL: controlan el cliente Cohere; si la API Key falta, no se usa Cohere.
- historial_acciones: último puñado de acciones efectuadas, útil para auditoría en tiempo de ejecución.
"""

follow_up_mode = False
follow_up_lock = threading.Lock()
shutdown_confirmation_lock = threading.Lock()
shutdown_confirmation_pending = False
shutdown_timer: Optional[threading.Timer] = None
FOLLOW_UP_PROMPT = "¿Necesitás algo más?"
FOLLOW_UP_EXIT_FRASES = {
    "nada",
    "nada mas",
    "nada más",
    "no",
    "no gracias",
    "gracias",
    "listo",
    "estoy bien",
    "eso es todo",
    "seria todo",
    "sería todo",
}


def _cerrar_servidor_consola() -> None:
    """Cierra el servidor TCP que escucha solicitudes de adjuntar consola."""
    global _console_server_socket  # Indicamos que vamos a modificar la referencia global del socket para actualizarla tras cerrarlo.
    if _console_server_socket is None:  # Si no hay socket activo, no hay nada que limpiar y evitamos excepciones.
        return  # Terminamos temprano porque no hay recursos que liberar.
    try:
        _console_server_socket.close()  # Cerramos el descriptor para liberar el puerto y permitir reinicios posteriores sin TIME_WAIT prolongado.
    except OSError:
        pass  # Ignoramos errores de cierre porque el objetivo es únicamente liberar recursos y fallos aquí no afectan la lógica.
    finally:
        _console_server_socket = None  # Resetemos la referencia global para que futuras inicializaciones comprendan que no hay servidor activo.


def _bucle_servidor_consola(sock: socket.socket) -> None:
    """Atiende conexiones entrantes de auxiliares que piden adjuntar la consola al proceso residente."""
    while True:  # Mantenemos el hilo vivo aceptando peticiones hasta que el socket se cierre explícitamente.
        try:
            conn, _ = sock.accept()  # Bloqueamos esperando un cliente; obtenemos el socket dedicado a la sesión.
        except OSError:
            break  # Si accept lanza error es porque el socket se cerró; abandonamos el bucle sin propagar excepciones al hilo daemon.
        with conn:  # Garantizamos que el socket de cliente se cierre automáticamente al salir del bloque, evitando fugas.
            try:
                conn.settimeout(2.0)  # Limitamos el tiempo de lectura para no bloquear indefinidamente si el cliente deja de enviar datos.
                payload_bytes = conn.recv(65536)  # Leemos hasta 64 KB, suficiente para nuestra pequeña orden JSON sin gastar memoria excesiva.
            except OSError:
                continue  # Un fallo de lectura implica un cliente defectuoso; ignoramos la petición y seguimos con la siguiente.
            if not payload_bytes:  # Si no se recibió información, la sesión no es válida y no vale la pena procesarla.
                continue  # Reintentamos con la próxima conexión, dejando la presente sin respuesta.
            try:
                payload = json.loads(payload_bytes.decode("utf-8"))  # Parseamos el JSON enviado para obtener el comando estructurado.
            except json.JSONDecodeError:
                continue  # Un formato inválido supone que no es nuestra petición esperada; descartamos sin hacer nada.
            if payload.get("token") != CONSOLE_SERVER_TOKEN:  # Validamos el token para asegurarnos de que la orden proviene de nuestro lanzador.
                continue  # Si el token no coincide, no ejecutamos acciones para evitar ejecuciones accidentales.
            if payload.get("command") != "attach_console":  # Solo entendemos la orden de adjuntar consola; ignoramos cualquier otro comando futuro.
                continue  # Al no ser el comando esperado, terminamos la iteración actual.
            host_pid = int(payload.get("host_pid") or 0)  # Extraemos el PID del proceso anfitrión de la consola (generalmente cmd.exe) como entero.
            helper_pid = int(payload.get("helper_pid") or 0)  # Recuperamos el PID del proceso auxiliar por si necesitamos adjuntarnos a su consola como respaldo.
            logger.info("Solicitud de adjuntar consola recibida (host_pid=%s, helper_pid=%s).", host_pid, helper_pid)  # Registramos los detalles para diagnosticar posibles fallos.
            success = _adjuntar_consola_desde_peticion(host_pid=host_pid, helper_pid=helper_pid)  # Ejecutamos la lógica de adjuntar y capturamos si funcionó.
            try:
                conn.sendall(json.dumps({"ok": success}).encode("utf-8"))  # Respondemos al cliente con un JSON simple para confirmar el resultado.
            except OSError:
                pass  # Si no podemos responder no es crítico; la acción ya ocurrió en el proceso residente.


def _adjuntar_consola_desde_peticion(host_pid: int, helper_pid: int) -> bool:
    """Adjunta la consola visible al proceso actual o lanza un fallback si no es posible."""
    target_pid = host_pid or helper_pid  # Priorizamos el PID de la consola anfitriona (cmd.exe) y usamos el auxiliar si el primero no estuviera disponible.
    KERNEL32.FreeConsole()  # Soltamos cualquier consola previa asociada al proceso para que AttachConsole/AllocConsole puedan ejecutarse sin error.
    attached = False  # Flag que refleja si logramos vincularnos a la consola existente o tendremos que crear una propia.
    if target_pid:  # Solo intentamos el adjunte si recibimos un PID válido desde el mensajero.
        attached = bool(KERNEL32.AttachConsole(target_pid))  # Compartimos la consola abierta por el lanzador (cmd.exe) para reutilizar la misma ventana visible.
        if not attached:  # Si AttachConsole devolvió cero, registramos el error devuelto por Windows para diagnóstico.
            logger.error("AttachConsole falló (pid=%s, error=%s).", target_pid, ctypes.get_last_error())
    if not attached:  # Si no hubo consola disponible (pythonw, permisos), creamos una nueva para el proceso residente.
        if KERNEL32.AllocConsole() == 0:  # Pedimos a Windows una consola propia; si falla, no tendremos dónde mostrar logs.
            logger.error("AllocConsole falló (error=%s).", ctypes.get_last_error())  # Registramos el código de error Win32 por si necesitamos investigar derechos o políticas.
            _abrir_powershell_fallback()  # Activamos el fallback en PowerShell para asegurar al menos el tail de client.log.
            return False  # Comunicamos al llamador que la unión directa fracasó y recurrimos a la alternativa.
    if not _configurar_consola_actual(mostrar_banner=True):  # Reutilizamos la rutina común de redirección; si falla recurrimos al fallback.
        _abrir_powershell_fallback()  # Si la configuración no se pudo completar, garantizamos al menos el tail vía PowerShell.
        return False  # Comunicamos que no se pudo adjuntar la consola nativa.
    logger.info("Consola de logs adjuntada correctamente al proceso principal.")  # Registramos en el log que la consola quedó enlazada para trazabilidad posterior.
    return True  # Informamos que la operación fue satisfactoria y no se requirió el fallback.


def _reconfigurar_handlers_logging() -> None:
    """Actualiza los StreamHandler de logging para que escriban en la consola recién adjuntada."""
    for active_logger in (logging.getLogger(), logger):  # Iteramos tanto el logger raíz como el específico de este módulo para cubrir todos los handlers.
        for handler in list(active_logger.handlers):  # Copiamos la lista para poder modificarla sin interferir con iteraciones internas de logging.
            if isinstance(handler, logging.StreamHandler):  # Solo los StreamHandler dependen de un descriptor mutable; los demás (p.ej. FileHandler) no se tocan.
                handler.setStream(sys.stdout)  # Redirigimos el stream hacia sys.stdout, que ahora apunta a la consola activa.


def _imprimir_tail_log_consola() -> None:
    """Imprime las últimas 200 líneas de client.log si existe, cumpliendo el requerimiento de contexto."""
    if not CLIENT_LOG_PATH.exists():  # Validamos la existencia del log para evitar excepciones al intentar leerlo.
        print("No se encontró client.log; comenzando captura en vivo sin historial previo.")  # Avisamos al operador que no hay historial que mostrar.
        return  # Salimos porque no hay líneas previas que imprimir.
    try:
        with CLIENT_LOG_PATH.open("r", encoding="utf-8", errors="replace") as log_file:  # Abrimos el archivo tolerando caracteres inválidos para no fallar en lectura.
            ultimas_lineas = deque(log_file, maxlen=200)  # Aprovechamos deque para retener eficientemente solo las 200 líneas finales.
    except OSError as exc:
        print(f"No pude leer {CLIENT_LOG_PATH}: {exc}.")  # Comunicamos el error de IO para facilitar depuración si el archivo está bloqueado.
        return  # Sin lectura posible, no hay nada que mostrar en consola.
    if not ultimas_lineas:  # Si el archivo existe pero está vacío, lo indicamos explícitamente.
        print("El archivo client.log está vacío por el momento.")  # Mensaje orientado al operador dejando claro que la falta de líneas no es un fallo.
        return  # Detenemos el proceso porque no hay contenido que desplegar.
    for linea in ultimas_lineas:  # Iteramos las líneas retenidas respetando su orden cronológico.
        print(linea.rstrip("\n"))  # Mostramos cada línea eliminando el salto final para evitar dobles saltos en la consola.


def _configurar_consola_actual(mostrar_banner: bool) -> bool:
    """Redirige stdout/stderr/stdin a la consola actual y opcionalmente imprime encabezado + tail del log."""
    try:
        stdout_stream = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")  # Abrimos el dispositivo de salida estándar asociado a la consola visible con codificación robusta.
        stderr_stream = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")  # Reutilizamos el mismo descriptor para stderr garantizando que ambos flujos salgan por la ventana adjunta.
        stdin_stream = open("CONIN$", "r", buffering=1, encoding="utf-8", errors="replace")  # Habilitamos la lectura desde la consola para comandos interactivos futuros.
    except OSError as exc:
        logger.error("No pude abrir los pseudodispositivos de consola: %s", exc)  # Dejamos registro del fallo para diagnóstico.
        return False  # Avisamos al llamador que la configuración no pudo completarse.
    msvcrt.setmode(stdout_stream.fileno(), os.O_TEXT)  # Forzamos modo texto en stdout para que Windows no interprete bytes como binario.
    msvcrt.setmode(stderr_stream.fileno(), os.O_TEXT)  # Repetimos el ajuste en stderr evitando caracteres corruptos en mensajes de error.
    msvcrt.setmode(stdin_stream.fileno(), os.O_TEXT)  # Ajustamos stdin para que lecturas con input() funcionen correctamente.
    sys.stdout = stdout_stream  # Reemplazamos sys.stdout global para que print() escriba en la nueva consola.
    sys.stderr = stderr_stream  # Redirigimos sys.stderr asegurando que tracebacks y logging estándar aparezcan en pantalla.
    sys.stdin = stdin_stream  # Actualizamos sys.stdin, permitiendo interacción si se requiere.
    os.dup2(stdout_stream.fileno(), 1)  # Duplicamos el descriptor de bajo nivel STDOUT hacia la consola para cubrir extensiones C.
    os.dup2(stderr_stream.fileno(), 2)  # Hacemos lo mismo con STDERR para capturar mensajes nativos.
    os.dup2(stdin_stream.fileno(), 0)  # Reenrutamos STDIN de bajo nivel para que cualquier lectura raw provenga de la consola.
    _console_stream_refs.update({"stdout": stdout_stream, "stderr": stderr_stream, "stdin": stdin_stream})  # Conservamos referencias a los streams para impedir que el GC los cierre inadvertidamente.
    _reconfigurar_handlers_logging()  # Actualizamos los handlers de logging para que escriban en el nuevo sys.stdout.
    if mostrar_banner:  # Solo imprimimos encabezado y tail cuando se solicita (por ejemplo, al adjuntar o al iniciar con consola visible).
        print("=== Consola de logs adjuntada al proceso actual ===")  # Encabezado requerido que indica que la consola quedó enlazada.
        _imprimir_tail_log_consola()  # Volcamos las últimas líneas del log histórico para llenar la ventana con contexto inmediato.
    logger.info("Streams de consola redirigidos correctamente (mostrar_banner=%s).", mostrar_banner)  # Confirmamos en el log que la reconfiguración se completó.
    sys.stdout.flush()  # Vaciamos el buffer para que el encabezado/tail se vean al instante.
    return True  # Indicamos éxito al llamador.


def _abrir_powershell_fallback() -> None:
    """Lanza la alternativa Get-Content en PowerShell cuando no se pudo adjuntar la consola tradicional."""
    comando = f'Get-Content -Path "{CLIENT_LOG_PATH}" -Wait'  # Preparamos el comando que mantendrá un tail en vivo sobre el log del cliente.
    try:
        subprocess.Popen(  # Iniciamos un nuevo proceso independiente para no bloquear al hilo de atención.
            ["powershell", "-NoExit", "-Command", comando],  # Ejecutamos PowerShell manteniéndolo abierto para que siga actualizando el tail.
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),  # Pedimos una ventana separada garantizando visibilidad incluso desde pythonw.exe.
        )
    except Exception as exc:
        logger.error("No pude iniciar el fallback de PowerShell: %s", exc)  # Registramos el fallo para que quede constancia en el log principal.


def _enviar_solicitud_adjuntar_consola() -> bool:
    """Envía la orden de adjuntar consola a la instancia residente y devuelve True si respondió OK."""
    try:
        parent_pid = psutil.Process(os.getpid()).ppid()  # Identificamos el PID del proceso padre (cmd.exe) para aprovechar su consola existente.
    except Exception:
        parent_pid = 0  # Si psutil falla (procesos zombis o permisos), continuamos con cero para forzar AllocConsole en el residente.
    payload = {  # Construimos el mensaje que viajará por el socket TCP local con todos los datos necesarios.
        "token": CONSOLE_SERVER_TOKEN,  # Incluimos el token de autenticación compartido.
        "command": "attach_console",  # Indicamos que se trata de la orden específica de adjuntar consola.
        "host_pid": parent_pid,  # Transmitimos el PID del anfitrión detectado para intentar AttachConsole.
        "helper_pid": os.getpid(),  # Enviamos también el PID de este auxiliar como respaldo.
    }
    try:
        with socket.create_connection((CONSOLE_SERVER_HOST, CONSOLE_SERVER_PORT), timeout=2.0) as conn:  # Nos conectamos al servidor residente con timeout corto para no bloquear la UI.
            conn.sendall(json.dumps(payload).encode("utf-8"))  # Serializamos el payload y lo enviamos completo.
            conn.shutdown(socket.SHUT_WR)  # Cerramos el canal de escritura para señalar fin del mensaje y permitir al servidor responder.
            respuesta = conn.recv(4096)  # Leemos la confirmación que indica si la operación fue aceptada.
    except OSError:
        return False  # Si no se pudo establecer la conexión, devolvemos False para que el llamador actúe en consecuencia.
    if not respuesta:  # Si el servidor no devolvió nada, asumimos que algo falló.
        return False  # Comunicamos que no hay éxito.
    try:
        data = json.loads(respuesta.decode("utf-8"))  # Parseamos la respuesta para inspeccionar el campo "ok".
    except json.JSONDecodeError:
        return False  # Una respuesta malformada implica fallo; devolvemos False.
    return bool(data.get("ok"))  # Evaluamos el indicador de éxito y lo transformamos en bool explícito.


def _inicializar_ipc_consola() -> bool:
    """Prepara el servidor IPC o, si ya existe, actúa como mensajero para solicitar la consola."""
    consola_presente = bool(KERNEL32.GetConsoleWindow())  # Detectamos si esta instancia ya tiene consola disponible (p.ej. cuando nos lanza cmd.exe directamente).
    servidor: Optional[socket.socket] = None  # Socket que terminará escuchando peticiones si logramos reservar el puerto.
    for intento in range(5):  # Intentamos hasta cinco veces reservar el puerto para tolerar estados TIME_WAIT tras reinicios rápidos.
        servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # Creamos el socket para intentar asumir el rol de servidor residente.
        servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Permitimos reusar el puerto rápidamente tras cierres recientes.
        try:
            servidor.bind((CONSOLE_SERVER_HOST, CONSOLE_SERVER_PORT))  # Intentamos reservar el puerto; si funciona somos la instancia principal.
        except OSError as exc:
            servidor.close()  # Liberamos el descriptor antes de reintentar o abortar, evitando fugas.
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES):  # Para errores inesperados (no solo puerto ocupado), registramos y seguimos sin IPC.
                logger.error("No pude inicializar el servidor de consola: %s", exc)  # Dejamos constancia en el log para diagnóstico.
                servidor = None  # Aclaramos que no conservamos el socket fallido.
                break  # Salimos del bucle; continuaremos la ejecución principal sin IPC.
            if not consola_presente:  # Si el puerto está ocupado pero no tenemos consola, asumimos que otra instancia sigue activa; no duplicamos procesos.
                logger.info("Otra instancia de RAI ya está activa; no se iniciará un segundo proceso.")  # Informamos en logs y cancelamos el arranque duplicado.
                return False  # Detenemos la ejecución de este proceso auxiliar.
            if _enviar_solicitud_adjuntar_consola():  # Si venimos desde el menú Abrir terminal y la petición se procesó con éxito...
                print("Adjuntando consola al proceso residente. Esta sesión auxiliar se cerrará automáticamente.", flush=True)  # Damos feedback inmediato en la CMD recién abierta.
                return False  # Finalizamos esta instancia porque solo actuaba como mensajera.
            logger.warning("No pude contactar al proceso residente (intento %s/5). Reintentando reservar el puerto...", intento + 1)  # Anotamos el fallo para análisis.
            time.sleep(0.6)  # Esperamos un poco para que el SO libere recursos antes de volver a intentar la reserva.
            continue  # Volvemos al principio del bucle para reintentar el bind.
        else:
            break  # El bind funcionó; salimos del bucle con el socket listo para escuchar.
    else:
        servidor = None  # Si agotamos los intentos sin éxito, dejamos el socket en None.

    if servidor is None:  # Si no logramos reservar el puerto, continuamos sin IPC pero manteniendo la consola actual si existe.
        if consola_presente and not _configurar_consola_actual(mostrar_banner=True):  # Tratamos de redirigir la consola actual aunque no tengamos canal IPC.
            _abrir_powershell_fallback()  # Si tampoco pudimos configurar la consola, recurrimos al tail de PowerShell como última opción visible.
        return True  # Continuamos ejecutando el cliente para no perder funcionalidad, aunque no podamos servir futuras peticiones Abrir terminal.

    servidor.listen(5)  # Comenzamos a escuchar conexiones entrantes aceptando hasta 5 pendientes para tolerar clics repetidos rápidos.
    global _console_server_socket, _console_server_thread  # Indicamos que modificaremos las referencias globales guardadas.
    _console_server_socket = servidor  # Guardamos el socket activo para uso posterior y cierre ordenado.
    hilo = threading.Thread(target=_bucle_servidor_consola, args=(servidor,), daemon=True)  # Creamos el hilo daemon que atenderá las solicitudes sin bloquear el hilo principal.
    hilo.start()  # Lanzamos el hilo inmediatamente para que el canal IPC quede operativo.
    _console_server_thread = hilo  # Mantenemos la referencia por si necesitamos inspeccionarlo (p.ej. en depuración).
    atexit.register(_cerrar_servidor_consola)  # Registramos el cierre automático para liberar el puerto cuando el proceso termine.
    if consola_presente:  # Si ya tenemos consola visible (modo interactivo desde cmd.exe), la configuramos de inmediato.
        if not _configurar_consola_actual(mostrar_banner=True):  # Reutilizamos la misma rutina de redirección para que prints/logs fluyan y se vea el tail histórico.
            _abrir_powershell_fallback()  # Si falló la configuración, abrimos el fallback para no dejar la ventana vacía.
    return True  # Confirmamos que somos la instancia principal y que debemos continuar con la ejecución normal.


def _normalizar(texto: str) -> str:
    """Minusculiza y colapsa espacios múltiples a uno para comparar frases."""
    return re.sub(r"\s+", " ", texto.strip().lower())


def _sin_acentos(texto: str) -> str:
    """Devuelve el texto en minusculas y sin acentos para comparaciones flexibles."""
    texto_lower = texto.lower()
    descompuesto = unicodedata.normalize("NFD", texto_lower)
    return "".join(ch for ch in descompuesto if not unicodedata.combining(ch))


ATAJOS_VOZ: List[Dict[str, Any]] = [
    # Tabla de atajos por voz: patrones de regex en español mapeados a combinaciones de teclas.
    # Cada entrada incluye: id, descripcion, combos (tuplas de teclas) y patrones de activación.
    {
        "id": "mostrar_escritorio",
        "descripcion": "Mostrando el escritorio.",
        "combos": [("winleft", "d")],
        "patrones": [
            r"\b(mostrar|mostrame|mostrarme|muestrame|muestreme)\s+(el\s+)?escritorio\b",
            r"\b(minimiza(?:r|me)?|oculta(?:r|me)?|esconde)\s+(todo|todas\s+las\s+ventanas)\b",
        ],
    },
    {
        "id": "restaurar_ventanas",
        "descripcion": "Restaurando las ventanas.",
        "combos": [("winleft", "shift", "m")],
        "patrones": [
            r"\b(restaura(?:r|me)?|recupera|mostra)\s+(las\s+)?ventanas\b",
        ],
    },
    {
        "id": "cerrar_ventana",
        "descripcion": "Cerrando la ventana actual.",
        "combos": [("alt", "f4")],
        "patrones": [
            r"\b(cierra|cerrame|cerrar)\s+(la\s+)?ventana\b",
            r"\b(salir\s+de|cerrar)\s+(esta\s+)?aplicacion\b",
        ],
    },
    {
        "id": "cerrar_pestana",
        "descripcion": "Cerrando la pestaña.",
        "combos": [("ctrl", "w")],
        "patrones": [
            r"\b(cierra|cerrame|cerrar)\s+(la\s+)?pestana\b",
            r"\b(cerrar)\s+(esta\s+)?pestana\b",
        ],
    },
    {
        "id": "nueva_pestana",
        "descripcion": "Abriendo una nueva pestaña.",
        "combos": [("ctrl", "t")],
        "patrones": [
            r"\b(nueva|abrir)\s+(pestana|pestania)\b",
        ],
    },
    {
        "id": "reabrir_pestana",
        "descripcion": "Reabriendo la última pestaña.",
        "combos": [("ctrl", "shift", "t")],
        "patrones": [
            r"\b(reabrir|recupera|volver a abrir)\s+(la\s+)?(ultima|última)\s+pestana\b",
        ],
    },
    {
        "id": "nueva_ventana",
        "descripcion": "Abriendo una nueva ventana.",
        "combos": [("ctrl", "n")],
        "patrones": [
            r"\b(nueva|abrir)\s+ventana\b",
        ],
    },
    {
        "id": "ventana_incognita",
        "descripcion": "Abriendo una ventana de incógnito.",
        "combos": [("ctrl", "shift", "n")],
        "patrones": [
            r"\b(incognito|incognita|privada)\b",
        ],
    },
    {
        "id": "mostrar_explorador",
        "descripcion": "Abriendo el Explorador de archivos.",
        "combos": [("winleft", "e")],
        "patrones": [
            r"\b(abrir|abre)\s+(el\s+)?explorador\b",
            r"\b(abrir|abre)\s+(mis\s+)?archivos\b",
        ],
    },
    {
        "id": "bloquear_equipo",
        "descripcion": "Bloqueando el equipo.",
        "combos": [("winleft", "l")],
        "patrones": [
            r"\b(bloquea|bloquear|bloqueame)\s+(el\s+)?equipo\b",
            r"\b(bloquea|bloquear)\s+(la\s+)?pantalla\b",
        ],
    },
    {
        "id": "nuevo_escritorio",
        "descripcion": "Creando un escritorio nuevo.",
        "combos": [("winleft", "ctrl", "d")],
        "patrones": [
            r"\b(crea|crear|creame|agrega|agregame|sum[aá]me)\s+(un|otro)\s+escritorio\b",
            r"\b(nuevo|gener[aá])\s+escritorio\b",
        ],
    },
    {
        "id": "apagar_equipo",
        "descripcion": "Preparando apagado del equipo.",
        "combos": [],
        "patrones": [
            r"\b(apaga|apagar|apagame|apagalo|apaguen|apaguenla|apaguenlo)\s+(la\s+)?(computadora|pc|compu|equipo)\b",
            r"\b(apaga|apagar)\s+todo\b",
        ],
        "manual_follow_up": True,
    },
    {
        "id": "escritorio_derecha",
        "descripcion": "Pasando al escritorio de la derecha.",
        "combos": [("winleft", "ctrl", "right")],
        "patrones": [
            r"\b(pas[aá]|cambi[aá]|mueve(?:me)?|llev[aá](?:me)?)\s+(al|para)\s+(escritorio)\s+(de\s+)?(la\s+)?derecha\b",
            r"\b(escritorio)\s+(siguiente|que\s+sigue)\b",
        ],
    },
    {
        "id": "escritorio_izquierda",
        "descripcion": "Pasando al escritorio de la izquierda.",
        "combos": [("winleft", "ctrl", "left")],
        "patrones": [
            r"\b(pas[aá]|cambi[aá]|mueve(?:me)?|llev[aá](?:me)?)\s+(al|para)\s+(escritorio)\s+(de\s+)?(la\s+)?izquierda\b",
            r"\b(escritorio)\s+anterior\b",
        ],
    },
    {
        "id": "captura_pantalla",
        "descripcion": "Capturando pantalla.",
        "combos": [("winleft", "shift", "s")],
        "patrones": [
            r"\b(captura|capturar|sacar)\s+(de\s+)?pantalla\b",
            r"\b(screenshot|recorte)\b",
        ],
    },
    {
        "id": "grabar_pantalla",
        "descripcion": "Alternando grabación de pantalla.",
        "combos": [("winleft", "alt", "r")],
        "patrones": [
            r"\b(grabar|graba|grabame)\s+(la\s+)?pantalla\b",
            r"\b(termina|detener)\s+(la\s+)?grabacion\b",
        ],
    },
    {
        "id": "mostrar_busqueda",
        "descripcion": "Abriendo la búsqueda.",
        "combos": [("winleft", "s")],
        "patrones": [
            r"\b(abrir|abre)\s+(la\s+)?busqueda\b",
            r"\b(buscar|buscame)\b",
        ],
    },
    {
        "id": "seleccionar_omnibox",
        "descripcion": "Resaltando la barra de direcciones.",
        "combos": [("ctrl", "l")],
        "patrones": [
            r"\b(selecciona|marca|resalta)\s+(la\s+)?barra\b",
            r"\b(ir\s+a\s+la\s+barra)\b",
        ],
    },
    {
        "id": "seleccionar_todo",
        "descripcion": "Seleccionando todo.",
        "combos": [("ctrl", "a")],
        "patrones": [
            r"\b(selecciona|marca)\s+todo\b",
            r"\b(seleccionar\s+todo)\b",
        ],
    },
    {
        "id": "copiar",
        "descripcion": "Copiando selección.",
        "combos": [("ctrl", "c")],
        "patrones": [
            r"\b(copia|copiame|copialo|copiar)\b",
        ],
    },
    {
        "id": "pegar",
        "descripcion": "Pegando.",
        "combos": [("ctrl", "v")],
        "patrones": [
            r"\b(peg(a|á|ame|alo)|pegar)\b",
            r"\b(pega\s+lo\b)",
        ],
    },
    {
        "id": "cortar",
        "descripcion": "Cortando selección.",
        "combos": [("ctrl", "x")],
        "patrones": [
            r"\b(corta|cortame|cortalo|cortar)\b",
        ],
    },
    {
        "id": "deshacer",
        "descripcion": "Deshaciendo la última acción.",
        "combos": [("ctrl", "z")],
        "patrones": [
            r"\b(deshac(e|é)|deshacelo|deshacer)\b",
        ],
    },
    {
        "id": "rehacer",
        "descripcion": "Rehaciendo la acción.",
        "combos": [("ctrl", "y"), ("ctrl", "shift", "z")],
        "patrones": [
            r"\b(rehac(e|é)|rehacelo|repeti|repetí)\b",
        ],
    },
    {
        "id": "buscar_en_pantalla",
        "descripcion": "Buscando en la página.",
        "combos": [("ctrl", "f")],
        "patrones": [
            r"\b(busca|buscar)\s+(en\s+la\s+)?p[áa]gina\b",
            r"\b(encontr(a|á|ame)|encontrar)\b",
        ],
    },
    {
        "id": "guardar",
        "descripcion": "Guardando.",
        "combos": [("ctrl", "s")],
        "patrones": [
            r"\b(guarda|guardar|guardame)\b",
        ],
    },
    {
        "id": "guardar_como",
        "descripcion": "Guardando como.",
        "combos": [("ctrl", "shift", "s")],
        "patrones": [
            r"\b(guardar|guardame)\s+como\b",
        ],
    },
    {
        "id": "imprimir",
        "descripcion": "Abriendo la impresión.",
        "combos": [("ctrl", "p")],
        "patrones": [
            r"\b(imprim(e|é)|imprimir|impresi[oó]n)\b",
        ],
    },
    {
        "id": "abrir_archivo",
        "descripcion": "Abriendo archivo.",
        "combos": [("ctrl", "o")],
        "patrones": [
            r"\b(abr(i|í)|abrime)\s+archivo\b",
            r"\b(abrir)\s+un\s+archivo\b",
        ],
    },
    {
        "id": "actualizar_ventana",
        "descripcion": "Actualizando la ventana.",
        "combos": [("f5",)],
        "patrones": [
            r"\b(actualiza|actualiz[a|á]lo|refresca|recarga)\b",
        ],
    },
]

ATAJOS_IDS: Dict[str, Dict[str, Any]] = {atajo["id"]: atajo for atajo in ATAJOS_VOZ}  # Índice rápido por id.


def _asegurar_catalogo_unlocked() -> Dict[str, Any]:
    """Carga el catálogo desde disco a caché si no está cargado.

    No usa locks; el llamador externo (asegurar_catalogo/cargar_catalogo) maneja sincronización.
    """
    global _catalogo_cache
    if _catalogo_cache is None:
        try:
            with CATALOGO_PATH.open("r", encoding="utf-8") as fh:
                _catalogo_cache = json.load(fh)
        except FileNotFoundError:
            _catalogo_cache = {"aplicaciones": []}
        except Exception as exc:
            logger.error(f"No pude cargar el catálogo JSON: {exc}")
            _catalogo_cache = {"aplicaciones": []}
    aplicaciones = _catalogo_cache.get("aplicaciones")
    if not isinstance(aplicaciones, list):
        _catalogo_cache["aplicaciones"] = []
    return _catalogo_cache


def _guardar_catalogo_unlocked(catalogo: Dict[str, Any]) -> None:
    """Guarda de forma atómica el catálogo (write a tmp + replace)."""
    tmp_path = CATALOGO_PATH.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(catalogo, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(CATALOGO_PATH)
        global _catalogo_cache
        _catalogo_cache = json.loads(json.dumps(catalogo))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def registrar_accion(accion: Dict[str, Any]) -> None:
    """Añade la acción al historial en formato serializable (combos como listas de strings)."""
    accion_copia = dict(accion)
    if "combos" in accion_copia:
        combos_guardar: List[List[str]] = []
        for combo in accion_copia["combos"] or []:
            if isinstance(combo, (list, tuple)):
                combos_guardar.append([str(k) for k in combo])
            else:
                combos_guardar.append([str(combo)])
        accion_copia["combos"] = combos_guardar
    historial_acciones.append(accion_copia)
    logger.debug("Historial actualizado con: %s", accion_copia)




def _reiniciar_memoria_redaccion(texto: str, solicitud: str) -> None:
    """Inicializa la memoria de redacción con un texto base y su solicitud original."""
    memoria_redaccion["texto"] = texto.strip()
    memoria_redaccion["solicitud"] = solicitud.strip()
    memoria_redaccion["instrucciones"] = []


def _actualizar_texto_memoria(texto: str) -> None:
    """Sobrescribe el texto actual en memoria de redacción."""
    memoria_redaccion["texto"] = texto.strip()


def _agregar_instruccion_memoria(instruccion: str) -> None:
    """Añade una instrucción (pedido adicional) manteniendo un máximo de 5 entradas."""
    instruccion_limpia = (instruccion or "").strip()
    if not instruccion_limpia:
        return
    instrucciones = memoria_redaccion.get("instrucciones")
    if not isinstance(instrucciones, list):
        instrucciones = []
    instrucciones.append(instruccion_limpia)
    if len(instrucciones) > 5:
        instrucciones = instrucciones[-5:]
    memoria_redaccion["instrucciones"] = instrucciones


def _obtener_instrucciones_memoria() -> List[str]:
    """Devuelve la lista de instrucciones adicionales limpias (sin vacíos)."""
    instrucciones = memoria_redaccion.get("instrucciones")
    if isinstance(instrucciones, list):
        return [str(instr).strip() for instr in instrucciones if str(instr).strip()]
    return []


def _hay_redaccion_en_memoria() -> bool:
    """Indica si hay texto en memoria para intentar ajustes/redacción incremental."""
    return bool(str(memoria_redaccion.get("texto") or "").strip())

def _log_cohere_event(titulo: str, contenido: str) -> None:
    """Anexa un bloque de log sobre interacción con Cohere en cohere.log (best-effort)."""
    marca = datetime.datetime.now().isoformat(timespec="seconds")
    linea = f"[{marca}] {titulo}\n{contenido}\n{'-' * 60}\n"
    try:
        with COHERE_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(linea)
    except Exception:
        logger.debug("No pude escribir el log de Cohere.")


def cargar_catalogo() -> Dict[str, Any]:
    """Devuelve el catálogo en memoria, cargándolo si es necesario (thread-safe)."""
    with catalogo_lock:
        return _asegurar_catalogo_unlocked()


def asegurar_catalogo() -> None:
    """Garantiza que exista un archivo de catálogo en disco (crea uno vacío si no existe)."""
    with catalogo_lock:
        catalogo = _asegurar_catalogo_unlocked()
        if not CATALOGO_PATH.exists():
            _guardar_catalogo_unlocked(catalogo)


def obtener_cliente_cohere() -> Optional["cohere.Client"]:
    """Inicializa perezosamente el cliente Cohere si hay API Key y dependencia disponible."""
    global _cohere_client
    if cohere is None:
        logger.debug("Cohere no está instalado; omito generación asistida.")
        return None
    if _cohere_client is not None:
        return _cohere_client

    api_key = COHERE_API_KEY
    if not api_key:
        logger.error("COHERE_API_KEY no está configurada en el código.")
        return None
    try:
        _cohere_client = cohere.Client(api_key)
    except Exception as exc:  # pragma: no cover
        logger.error(f"No pude inicializar Cohere: {exc}")
        return None
    return _cohere_client


def _componer_contexto_catalogo(catalogo: Dict[str, Any], app_obj: Optional[Dict[str, Any]] = None) -> str:
    """Construye un texto JSON compacto con datos relevantes para orientar a Cohere."""
    bloques: List[str] = []
    if app_obj:
        detalles = {
            "nombre": app_obj.get("nombre"),
            "tipo": app_obj.get("tipo"),
            "paths": app_obj.get("paths"),
            "acciones": list((app_obj.get("acciones") or {}).keys()),
        }
        bloques.append(json.dumps(detalles, ensure_ascii=False))
    else:
        bloques.append("(catálogo deshabilitado)")
    return "\n".join(bloques)


def _extraer_json(texto: str) -> Optional[Dict[str, Any]]:
    """Intenta encontrar y parsear un objeto JSON embebido en un texto arbitrario."""
    match = re.search(r"\{.*\}", texto, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _extraer_comandos_desde_texto(texto: str) -> List[str]:
    """Parsea líneas de comandos desde un texto, tolerando formatos comunes de respuesta."""
    if not texto:
        return []

    bruto = texto.strip()
    if not bruto or bruto.upper() == "NINGUNO":
        return []

    comandos: List[str] = []
    en_bloque_codigo = False

    for linea in bruto.splitlines():
        contenido = linea.strip()
        if not contenido:
            continue
        if contenido.startswith("```"):
            en_bloque_codigo = not en_bloque_codigo
            continue
        if not en_bloque_codigo and contenido.lower().startswith(("comandos", "descripcion")):
            continue
        if contenido in {"[", "]", "{", "}"}:
            continue
        if contenido.startswith('"') and contenido.endswith('"') and len(contenido) >= 2:
            contenido = contenido[1:-1].strip()
        elif contenido.startswith("'") and contenido.endswith("'") and len(contenido) >= 2:
            contenido = contenido[1:-1].strip()
        if contenido.endswith(","):
            contenido = contenido[:-1].rstrip()
        if not contenido:
            continue
        comandos.append(contenido)

    if not comandos:
        candidato = bruto
        if candidato.startswith('"') and candidato.endswith('"') and len(candidato) >= 2:
            candidato = candidato[1:-1].strip()
        elif candidato.startswith("'") and candidato.endswith("'") and len(candidato) >= 2:
            candidato = candidato[1:-1].strip()
        candidato = candidato.rstrip(",").strip()
        if candidato and candidato.upper() != "NINGUNO":
            comandos.append(candidato)

    return comandos


def _extraer_ruta_exe(comando: str) -> Optional[str]:
    """Localiza la primera ruta de .exe presente en un comando plano."""
    if not comando:
        return None

    texto = comando.strip()
    if not texto:
        return None

    comando_lower = texto.lower()

    def _normalizar_candidato(candidato: str) -> str:
        limpio = candidato.strip().strip('"')
        expandido = os.path.expanduser(os.path.expandvars(limpio))
        return os.path.normpath(expandido)

    patrones = re.findall(r'"([^"]+\.exe)"', texto, flags=re.IGNORECASE)
    for candidato in patrones:
        ruta = _normalizar_candidato(candidato)
        if ruta.lower().endswith("explorer.exe") and "shell:appsfolder" in comando_lower:
            continue
        return ruta

    for fragmento in re.split(r"\s+", texto):
        if not fragmento:
            continue
        candidato = fragmento.strip().strip('"')
        if not candidato.lower().endswith(".exe"):
            continue
        if candidato.lower() == "explorer.exe" and "shell:appsfolder" in comando_lower:
            continue
        return _normalizar_candidato(candidato)

    return None


def _ajustar_ruta_disponible(ruta: str) -> Optional[str]:
    """Devuelve una ruta existente ajustando variables, comillas y variantes comunes."""
    if not ruta:
        return None

    ruta_limpia = ruta.strip().strip('"')
    if not ruta_limpia:
        return None

    candidatos: List[str] = []

    def _agregar(candidato: str) -> None:
        if not candidato:
            return
        norm = os.path.normpath(candidato)
        if norm not in candidatos:
            candidatos.append(norm)

    expandida = os.path.expanduser(os.path.expandvars(ruta_limpia))
    _agregar(expandida)

    exe_name = Path(expandida).name.lower()
    encontrado = shutil.which(exe_name if exe_name else expandida)
    if encontrado:
        _agregar(encontrado)

    pf = os.environ.get("ProgramFiles")
    pfx86 = os.environ.get("ProgramFiles(x86)")
    expandida_lower = expandida.lower()
    if pf and pfx86:
        pf_lower = pf.lower()
        pfx86_lower = pfx86.lower()
        if expandida_lower.startswith(pfx86_lower):
            resto = expandida[len(pfx86):].lstrip("\\/")
            _agregar(os.path.join(pf, resto))
        elif expandida_lower.startswith(pf_lower):
            resto = expandida[len(pf):].lstrip("\\/")
            _agregar(os.path.join(pfx86, resto))

    for candidato in candidatos:
        if os.path.exists(candidato):
            return candidato

    return None


def generar_comandos_con_cohere(
    peticion: str,
    *,
    contexto_app: Optional[Dict[str, Any]] = None,
    catalogo_actual: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Pide a Cohere comandos ejecutables para Windows y los parsea a lista.

    - Retorna {"comandos": List[str], "descripcion": str} o None si no hay respuesta utilizable.
    - Registra prompts y respuestas en COHERE_LOG_PATH para depurar.
    """
    cliente = obtener_cliente_cohere()
    if not cliente:
        return None

    catalogo = catalogo_actual or cargar_catalogo()
    contexto_catalogo = _componer_contexto_catalogo(catalogo, contexto_app)
    instrucciones = (
        "Eres un asistente que genera comandos exactos para Windows.\n"
        "Ignora comandos pregrabados en catalogos y genera siempre instrucciones directas.\n"
        "Si el usuario pide cerrar o terminar una aplicacion, responde solamente con Stop-Process -Name \"NOMBRE\" -Force.\n"
        "Reemplaza NOMBRE por el nombre del proceso sin la extension .exe.\n"
        "Si necesitas abrir un .exe existente, responde con start \"\" \"RUTA\" o con la ruta exacta.\n"
        "Para apps UWP responde exactamente explorer.exe shell:appsFolder\\<AppUserModelID>.\n"
        "Evita comandos genericos o rutas inventadas.\n"
        "Si la orden requiere varias acciones, escribe un comando por linea en el orden correcto.\n"
        "Responde unicamente con comandos ejecutables en texto plano.\n"
        "Si no puedes ayudar, responde exactamente NINGUNO."
    )
    prompt = (
        f"{instrucciones}\n"
        f"Aplicaciones conocidas:\n{contexto_catalogo}\n"
        f"Solicitud del usuario: \"{peticion.strip()}\""
    )

    respuesta_texto = ""
    _log_cohere_event("PROMPT", prompt)
    try:
        respuesta_chat = cliente.chat(
            model=COHERE_MODEL,
            message=prompt,
            temperature=0.1,
        )
        logger.debug("COHERE PROMPT:\n%s", prompt)
        logger.debug("COHERE RAW RESPONSE: %s", respuesta_chat)
        _log_cohere_event("RAW RESPONSE", str(respuesta_chat))
        if hasattr(respuesta_chat, "text") and respuesta_chat.text:
            respuesta_texto = respuesta_chat.text.strip()
        elif hasattr(respuesta_chat, "message"):
            contenido = getattr(respuesta_chat.message, "content", [])
            partes: List[str] = []
            for bloque in contenido or []:
                if isinstance(bloque, dict):
                    if bloque.get("type") == "text":
                        partes.append(str(bloque.get("text", "")))
                else:
                    tipo = getattr(bloque, "type", None)
                    texto = getattr(bloque, "text", "")
                    if tipo == "text" and texto:
                        partes.append(str(texto))
            respuesta_texto = "".join(partes).strip()
        elif hasattr(respuesta_chat, "output_text"):
            respuesta_texto = (respuesta_chat.output_text or "").strip()
    except Exception as exc:
        logger.error(f"Cohere chat falló: {exc}")
        return None

    if not respuesta_texto:
        logger.warning("Cohere no devolvió texto.")
        return None

    descripcion = ""
    comandos_filtrados: List[str] = []

    datos = _extraer_json(respuesta_texto)
    if datos:
        comandos_obj = datos.get("comandos")
        if isinstance(comandos_obj, list):
            comandos_filtrados = [cmd.strip() for cmd in comandos_obj if isinstance(cmd, str) and cmd.strip()]
        elif isinstance(comandos_obj, str) and comandos_obj.strip():
            comandos_filtrados = [comandos_obj.strip()]
        else:
            logger.warning("Cohere devolvio comandos invalidos.")
            return None
        descripcion_obj = datos.get("descripcion")
        if isinstance(descripcion_obj, str):
            descripcion = descripcion_obj
    else:
        comandos_filtrados = _extraer_comandos_desde_texto(respuesta_texto)
        if not comandos_filtrados:
            logger.warning(f"Cohere no proporciono comandos interpretables: {respuesta_texto}")
            return None

    try:
        _log_cohere_event("COMANDOS PARSEADOS", json.dumps({"comandos": comandos_filtrados, "descripcion": descripcion}, ensure_ascii=False, indent=2))
    except Exception:
        logger.debug("No pude registrar el resultado parseado.")

    return {"comandos": comandos_filtrados, "descripcion": descripcion}


def generar_respuesta_con_cohere(mensaje: str) -> Optional[str]:
    """Obtiene una respuesta conversacional breve en español rioplatense usando Cohere."""
    cliente = obtener_cliente_cohere()
    if not cliente:
        return None
    instrucciones = (
        "Eres un asistente conversacional en español rioplatense. "
        "Responde de forma breve, cordial y útil. Evita mencionar que eres una IA. "
        "Si no tienes contexto suficiente, muestra empatía y pide aclaraciones. No superes los 70 caracteres por respuesta."
    )
    prompt = f"{instrucciones}\nUsuario: {mensaje.strip()}\nRespuesta:"
    _log_cohere_event("PROMPT_RESPUESTA", prompt)
    try:
        respuesta = cliente.chat(
            model=COHERE_MODEL,
            message=prompt,
            temperature=0.6,
        )
    except Exception as exc:
        logger.error(f"Cohere respuesta falló: {exc}")
        return None

    texto = ""
    if hasattr(respuesta, "text") and respuesta.text:
        texto = respuesta.text.strip()
    elif hasattr(respuesta, "message"):
        contenido = getattr(respuesta.message, "content", [])
        partes: List[str] = []
        for bloque in contenido or []:
            if isinstance(bloque, dict) and bloque.get("type") == "text":
                partes.append(str(bloque.get("text", "")))
        texto = "".join(partes).strip()
    elif hasattr(respuesta, "output_text"):
        texto = (respuesta.output_text or "").strip()

    texto = texto.strip()
    if not texto:
        return None
    _log_cohere_event("RESPUESTA_CONVERSACIONAL", texto)
    return texto

def generar_redaccion_desde_memoria(nueva_instruccion: str) -> Optional[str]:
    """Ajusta/redacta un texto ya almacenado en memoria según un pedido adicional."""
    texto_actual = str(memoria_redaccion.get("texto") or "").strip()
    if not texto_actual:
        return None
    cliente = obtener_cliente_cohere()
    if not cliente:
        return None
    solicitud_original = str(memoria_redaccion.get("solicitud") or "").strip()
    instrucciones_previas = _obtener_instrucciones_memoria()
    pedidos_previos = ""
    if instrucciones_previas:
        pedidos_previos = "\nPedidos adicionales previos:\n" + "\n".join(f"- {item}" for item in instrucciones_previas)
    instrucciones_generales = (
        "Eres un redactor en español rioplatense. Ajusta el mensaje original para que cumpla las nuevas indicaciones. "
        "Mantén el mismo destinatario y propósito. Devuelve únicamente el texto final listo para enviar, sin comillas ni explicaciones."
    )
    prompt = (
        f"{instrucciones_generales}\n"
        f"Solicitud original: {solicitud_original or 'Mensaje para redactar'}\n"
        f"Texto actual:\n{texto_actual}\n"
    )
    if pedidos_previos:
        prompt += f"{pedidos_previos}\n"
    prompt += f"Nuevo pedido del usuario: {nueva_instruccion.strip()}\nTexto ajustado:"
    _log_cohere_event("PROMPT_REDACCION_AJUSTADA", prompt)
    try:
        respuesta = cliente.chat(
            model=COHERE_MODEL,
            message=prompt,
            temperature=0.5,
        )
    except Exception as exc:
        logger.error(f"Cohere ajuste redacción falló: {exc}")
        return None
    texto_respuesta = ""
    if hasattr(respuesta, "text") and respuesta.text:
        texto_respuesta = respuesta.text.strip()
    elif hasattr(respuesta, "message"):
        contenido = getattr(respuesta.message, "content", [])
        partes: List[str] = []
        for bloque in contenido or []:
            if isinstance(bloque, dict) and bloque.get("type") == "text":
                partes.append(str(bloque.get("text", "")))
        texto_respuesta = "".join(partes).strip()
    elif hasattr(respuesta, "output_text"):
        texto_respuesta = (respuesta.output_text or "").strip()
    texto_respuesta = texto_respuesta.strip()
    if not texto_respuesta:
        return None
    if texto_respuesta.startswith('"') and texto_respuesta.endswith('"'):
        texto_respuesta = texto_respuesta[1:-1].strip()
    if not texto_respuesta:
        return None
    _log_cohere_event("RESPUESTA_REDACCION_AJUSTADA", texto_respuesta)
    return texto_respuesta


def _buscar_app(catalogo: Dict[str, Any], nombre_app: str) -> Optional[Dict[str, Any]]:
    """Busca una app por nombre/id/alias con comparación flexible (normalizada)."""
    objetivo = _normalizar(nombre_app)
    for app in catalogo.get("aplicaciones", []):
        candidatos = [
            str(app.get("nombre", "")),
            str(app.get("id", "")),
        ]
        aliases = app.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        candidatos.extend(str(alias) for alias in aliases)
        for candidato in candidatos:
            if not candidato:
                continue
            cand_norm = _normalizar(candidato)
            if objetivo in cand_norm or cand_norm in objetivo:
                return app
    return None


def buscar_comando_por_nombre(nombre_app: str) -> Optional[tuple[str, str, str]]:
    """Devuelve (nombre, comando, tipo) para abrir una app conocida, o None si no hay definición."""
    catalogo = cargar_catalogo()
    app = _buscar_app(catalogo, nombre_app)
    if not app:
        return None
    acciones = app.get("acciones") or {}
    if not isinstance(acciones, dict):
        acciones = {} 
    comando = acciones.get("abrir") or app.get("launch") or app.get("comando")
    if not comando:
        return None
    tipo = str(app.get("tipo") or app.get("type") or "exe").lower()
    nombre = str(app.get("nombre") or app.get("id") or nombre_app)
    return nombre, comando, tipo


def escaner_inteligente(tipo: str) -> None:
    """Imprime en logs diagnósticos de RAM/CPU/Discos usando psutil según el tipo solicitado."""
    try:
        if tipo == "ram":
            procesos = sorted(
                psutil.process_iter(["pid", "name", "memory_info"]),
                key=lambda p: p.info["memory_info"].rss,
                reverse=True,
            )
            logger.info("Procesos con mayor uso de RAM:")
            for proc in procesos[:10]:
                logger.info(
                    " - %s (PID: %s) - %.2f MB",
                    proc.info["name"],
                    proc.info["pid"],
                    proc.info["memory_info"].rss / (1024 * 1024),
                )
        elif tipo == "cpu":
            procesos = sorted(
                psutil.process_iter(["pid", "name", "cpu_percent"]),
                key=lambda p: p.info["cpu_percent"],
                reverse=True,
            )
            logger.info("Procesos con mayor uso de CPU:")
            for proc in procesos[:10]:
                logger.info(
                    " - %s (PID: %s) - %s%%",
                    proc.info["name"],
                    proc.info["pid"],
                    proc.info["cpu_percent"],
                )
        elif tipo.startswith("disco"):
            letra = tipo.split(":")[1].upper() if ":" in tipo else "TODOS"
            particiones = (
                psutil.disk_partitions()
                if letra == "TODOS"
                else [p for p in psutil.disk_partitions() if p.device.upper().startswith(letra + ":")]
            )
            logger.info("Estado del disco:")
            for p in particiones:
                try:
                    uso = psutil.disk_usage(p.mountpoint)
                except PermissionError:
                    continue
                logger.info(
                    " - %s (%s): Total %.2f GB | Usado %.2f GB | Libre %.2f GB | %s%% usado",
                    p.device,
                    p.mountpoint,
                    uso.total / (1024**3),
                    uso.used / (1024**3),
                    uso.free / (1024**3),
                    uso.percent,
                )
        else:
            logger.warning("Tipo de escaneo no reconocido: %s", tipo)
    except Exception as exc:
        logger.error("Error en escaneo: %s", exc)


def ejecutar_accion_ventana(accion: str, nombre_ventana: str) -> None:
    try:
        objetivo_norm = _sin_acentos(nombre_ventana.lower().strip())
        ventana = None
        try:
            posibles = gw.getWindowsWithTitle(nombre_ventana)
        except Exception:
            posibles = []
        for candidato in posibles:
            titulo = (getattr(candidato, "title", "") or "").strip()
            if not titulo:
                continue
            if objetivo_norm in _sin_acentos(titulo.lower()):
                ventana = candidato
                break
        if ventana is None:
            try:
                for candidato in gw.getAllWindows():
                    titulo = (getattr(candidato, "title", "") or "").strip()
                    if not titulo:
                        continue
                    if objetivo_norm in _sin_acentos(titulo.lower()):
                        ventana = candidato
                        break
            except Exception:
                ventana = None
        if ventana:
            if accion == "maximizar":
                hwnd = getattr(ventana, "_hWnd", None)
                max_exitoso = False

                if hwnd:
                    try:
                        USER32.ShowWindow(hwnd, 9)  # SW_RESTORE
                        USER32.ShowWindow(hwnd, 5)  # SW_SHOW
                        logger.debug("Restauré la ventana %s mediante ShowWindow.", hwnd)
                    except Exception as exc:
                        logger.debug("No pude restaurar con ShowWindow: %s", exc)

                    current_thread = KERNEL32.GetCurrentThreadId()
                    target_thread = USER32.GetWindowThreadProcessId(hwnd, None)
                    attached = False
                    if current_thread and target_thread and current_thread != target_thread:
                        try:
                            attached = bool(USER32.AttachThreadInput(current_thread, target_thread, True))
                        except Exception as exc:
                            logger.debug("AttachThreadInput falló: %s", exc)
                    try:
                        try:
                            USER32.BringWindowToTop(hwnd)
                        except Exception:
                            pass
                        try:
                            USER32.SetForegroundWindow(hwnd)
                        except Exception as exc:
                            logger.debug("SetForegroundWindow falló: %s", exc)
                        try:
                            USER32.SwitchToThisWindow(hwnd, True)
                        except Exception:
                            pass
                    finally:
                        if attached:
                            try:
                                USER32.AttachThreadInput(current_thread, target_thread, False)
                            except Exception:
                                pass

                try:
                    ventana.restore()
                except Exception as exc:
                    logger.debug("pygetwindow.restore falló: %s", exc)

                try:
                    ventana.activate()
                except Exception as exc:
                    logger.debug("pygetwindow.activate falló: %s", exc)

                try:
                    ventana.maximize()
                    time.sleep(0.05)
                    if not hwnd or USER32.IsZoomed(hwnd):
                        max_exitoso = True
                        logger.debug("Maximización directa confirmada.")
                except Exception as exc:
                    logger.debug("pygetwindow.maximize falló: %s", exc)

                if not max_exitoso and hwnd:
                    try:
                        USER32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
                        USER32.PostMessageW(hwnd, 0x0112, 0xF030, 0)  # WM_SYSCOMMAND SC_MAXIMIZE
                        time.sleep(0.05)
                        if USER32.IsZoomed(hwnd):
                            max_exitoso = True
                            logger.debug("Maximización via ShowWindow/PostMessage confirmada.")
                    except Exception as win_exc:
                        logger.debug("Maximización con ShowWindow/PostMessage falló: %s", win_exc)

                if not max_exitoso:
                    try:
                        if hwnd:
                            USER32.SetForegroundWindow(hwnd)
                        time.sleep(0.1)
                        pyautogui.hotkey("win", "up")
                        time.sleep(0.05)
                        if not hwnd or USER32.IsZoomed(hwnd):
                            max_exitoso = True
                            logger.debug("Maximización via Win+Up confirmada.")
                    except Exception as hotkey_exc:
                        logger.debug("Atajo Win+Up falló: %s", hotkey_exc)

                if not max_exitoso:
                    raise RuntimeError("No pude maximizar la ventana, incluso con los métodos alternativos.")
                elif accion == "minimizar":
                    ventana.minimize()
                elif accion == "enfocar":
                    ventana.activate()
            logger.info("Acción '%s' ejecutada sobre '%s'.", accion, nombre_ventana)
        else:
            raise ValueError("ventana_no_encontrada")
    except Exception as exc:
        raise RuntimeError(f"Error en acción de ventana: {exc}") from exc


def listar_ventanas_y_procesos() -> None:
    logger.info("Ventanas abiertas:")
    for w in gw.getAllWindows():
        if w.title:
            logger.info(" - %s", w.title)
    logger.info("\nProcesos activos:")
    for proc in psutil.process_iter(["name"]):
        nombre = proc.info["name"]
        if nombre:
            logger.info(" - %s", nombre)


def procesar_emocion_y_puntuacion(texto: str) -> str:
    texto = texto.strip()
    if texto.endswith(("que", "como", "donde", "cuando", "por qué")) or texto.lower().startswith(
        ("qué ", "cómo ", "cuándo ", "dónde ", "por qué ")
    ):
        return texto[0].upper() + texto[1:] + "?"
    emocion = ["dale", "vamos", "sí", "listo", "buenísimo", "perfecto", "increíble", "genial", "me encanta", "de una"]
    for palabra in emocion:
        if re.search(rf"\b{palabra}\b", texto.lower()):
            return texto[0].upper() + texto[1:] + "!"
    texto = re.sub(r"\b(osea|oseas|eh|emm|mm+)\b", "", texto, flags=re.IGNORECASE)
    texto = re.sub(r"\s{2,}", " ", texto).strip()
    if not texto.endswith((".", "!", "?")):
        texto += "."
    return texto[0].upper() + texto[1:]


def grabar_y_procesar_orden() -> None:
    from hud import mostrar, ocultar, set_estado, set_texto_animado

    global texto_acumulado
    mostrar(es_bienvenida=True)
    set_estado("procesando", "")

    def despues_del_typing() -> None:
        global texto_acumulado
        recognizer = sr.Recognizer()
        mic = sr.Microphone()
        with mic as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.5)
            audio = recognizer.listen(source, timeout=None)
            set_estado("escuchando", "Escuchando...")
        log("Procesando orden...")

        try:
            texto = recognizer.recognize_google(audio, language="es-AR")
            texto = procesar_emocion_y_puntuacion(texto)
            log(f'Fragmento capturado: "{texto}"')
            texto_acumulado += " " + texto
            texto_acumulado = texto_acumulado.strip()
            log(f'Mensaje acumulado: "{texto_acumulado}"')
        except sr.UnknownValueError:
            log("No entendí lo que dijiste.")
        except sr.RequestError as exc:
            log(f"Error de reconocimiento: {exc}")

        enviar_mensaje_final()

    set_texto_animado(
        "Hola, soy RAI. En que puedo ayudarte?",
        after=despues_del_typing,
    )


def escuchar_fragmento() -> Optional[str]:
    recognizer = sr.Recognizer()
    audio: Optional["sr.AudioData"] = None
    try:
        with sr.Microphone() as source:
            try:
                recognizer.adjust_for_ambient_noise(source, duration=0.5)
            except AssertionError as exc:
                logger.error("No pude calibrar el micrófono: %s", exc)
                return None
            audio = recognizer.listen(source, phrase_time_limit=5)
    except (AttributeError, AssertionError, OSError, ValueError) as exc:
        logger.error("No se pudo acceder al micrófono: %s", exc)
        return None
    except Exception as exc:
        logger.error("Error inesperado al abrir el micrófono: %s", exc)
        return None
    if audio is None:
        return None
    try:
        texto = recognizer.recognize_google(audio, language="es-AR")
        logger.info("Escuchado: %s", texto)
        return texto.lower()
    except sr.UnknownValueError:
        return None
    except sr.RequestError as exc:
        logger.error("Error con el reconocimiento de voz: %s", exc)
        return None


def escuchar_hotword() -> None:
    logger.info("Decí 'okay rey' para dar una orden...")
    while True:
        texto = escuchar_fragmento()
        if not texto:
            continue
        if any(h in texto for h in ["okay rey", "okey rey", "hola rey", "hey rey"]):
            logger.info("Hola, soy RAI. ¿Cómo puedo ayudarte?")
            grabar_y_procesar_orden()


def ejecutar_comando_cmd(comando: str) -> bool:
    try:
        comando = comando.replace("TuUsuario", usuario).replace("%USERNAME%", usuario).strip()

        if comando.lower().startswith("start "):
            partes = comando.split(maxsplit=1)
            if len(partes) == 2:
                resto = partes[1].strip().strip('"')
                comando = f'start "" "{resto}"'

        ruta_candidata = _extraer_ruta_exe(comando)
        if ruta_candidata:
            ruta_ajustada = _ajustar_ruta_disponible(ruta_candidata)
            if ruta_ajustada and ruta_ajustada != ruta_candidata:
                if comando.lower().startswith("start \"\" \""):
                    comando = f'start "" "{ruta_ajustada}"'
                else:
                    comando = ruta_ajustada

        if comando.lower().startswith("start \"\" \"") and comando.lower().endswith(".exe\""):
            ruta_final = _extraer_ruta_exe(comando)
            if ruta_final and os.path.exists(ruta_final):
                subprocess.Popen(ruta_final)
                logger.info("Ejecutable lanzado desde start.")
                return True
        elif comando.lower().endswith(".exe") and os.path.exists(comando.strip('"')):
            subprocess.Popen(comando.strip('"'))
            logger.info("Ejecutable lanzado directamente.")
            return True

        if comando.lower().startswith("explorer.exe shell:appsfolder") and "shell:appsfolder\\" not in comando.lower():
            comando = comando.replace("shell:appsFolder", "shell:appsFolder\\")

        logger.debug("Comando tras normalización: %s", comando)

        if comando.startswith("explorer.exe shell:appsFolder\\"):
            subprocess.Popen(comando, shell=True)
            logger.info("Comando UWP ejecutado (sin salida esperada).")
            return True

        if comando.strip().lower() == "listar_ventanas_y_procesos":
            listar_ventanas_y_procesos()
            return True
        if comando.startswith("tecla:"):
            combinacion = comando.split(":", 1)[1]
            teclas = [t.strip() for t in combinacion.split("+") if t.strip()]
            if teclas:
                pyautogui.hotkey(*teclas)
            return True
        if comando.startswith("ventana:"):
            _, accion, nombre = comando.split(":", 2)
            ejecutar_accion_ventana(accion, nombre)
            return True
        if comando.lower() in {"bloquear_camara", "desbloquear_camara", "bloquear_microfono", "desbloquear_microfono"}:
            valor = "Deny" if "bloquear" in comando else "Allow"
            target = "webcam" if "camara" in comando else "microphone"
            ps_cmd = (
                'Set-ItemProperty -Path '
                '"HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\{target}" '
                '-Name Value -Value {valor}'
            ).format(target=target, valor=valor)
            subprocess.run(["powershell", "-Command", ps_cmd], check=True)
            return True
        if comando.startswith("diagnostico:"):
            escaner_inteligente(comando)
            return True

        if comando.strip().lower().startswith("stop-process"):
            ps_cmd = comando.strip()
            resultado = subprocess.run(
                ["powershell", "-NoLogo", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True,
                text=True,
            )
            if resultado.returncode == 0:
                logger.info("Comando ejecutado con éxito (PowerShell).")
                if resultado.stdout.strip():
                    logger.info(resultado.stdout)
                return True
            logger.error("Error en comando PowerShell: %s", resultado.stderr)
            return False

        resultado = subprocess.run(comando, shell=True, capture_output=True, text=True)
        if resultado.returncode == 0:
            logger.info("Comando ejecutado con éxito.")
            if resultado.stdout.strip():
                logger.info(resultado.stdout)
            return True
        logger.error("Error en comando: %s", resultado.stderr)
        return False
    except Exception as exc:
        logger.error("Error ejecutando comando: %s", exc)
        return False


def ejecutar_comandos_en_cadena(comandos: str) -> bool:
    comandos_lista = [cmd.strip() for cmd in comandos.replace("\n", ";").split(";") if cmd.strip()]
    if not comandos_lista:
        return False
    for comando in comandos_lista:
        logger.info("Ejecutando: %s", comando)
        if not ejecutar_comando_cmd(comando):
            return False
    return True


def es_pregunta_larga(texto: str) -> bool:
    palabras_largas = ["buscar", "explicar", "describir", "resumir", "qué es", "cómo", "quién", "dónde", "por qué"]
    texto_lower = texto.lower()
    return any(p in texto_lower for p in palabras_largas)

def _ejecutar_combos_teclado(combos: List[Tuple[str, ...]]) -> bool:
    if not combos:
        return False
    try:
        for combo in combos:
            teclas = tuple(combo)
            if not teclas:
                continue
            logger.debug("Lanzando atajo: %s", "+".join(teclas))
            pyautogui.hotkey(*teclas)
            time.sleep(0.05)
        return True
    except Exception as exc:
        logger.error("Error ejecutando atajo de teclado: %s", exc)
        return False


def _iniciar_confirmacion_apagado() -> bool:
    global shutdown_confirmation_pending
    with shutdown_confirmation_lock:
        if shutdown_confirmation_pending:
            hud.log("Necesito que confirmes si querés apagar el equipo.")
            iniciar_follow_up(force_start=True)
            return True
        shutdown_confirmation_pending = True
    hud.log("¿Seguro que querés ejecutar esta acción?")
    iniciar_follow_up(force_start=True)
    return True


def _programar_apagado_confirmado() -> None:
    global shutdown_timer

    def _apagar() -> None:
        logger.info("Ejecutando apagado del equipo solicitado por voz.")
        try:
            subprocess.run(["shutdown", "/s", "/t", "0"], check=False)
        except Exception as exc:
            logger.error("No pude apagar el equipo: %s", exc)
            hud.log(f"No pude apagar el equipo: {exc}")

    shutdown_timer = threading.Timer(5, _apagar)
    shutdown_timer.start()


def _procesar_confirmacion_apagado(mensaje: str) -> bool:
    global shutdown_confirmation_pending, texto_acumulado, follow_up_mode
    base = _sin_acentos(mensaje or "").strip()
    if not base:
        hud.log('Necesito que me confirmes con "sí" o "no".')
        iniciar_follow_up(force_start=True)
        return True

    def contiene(palabras: Iterable[str]) -> bool:
        return any(re.search(rf"\b{re.escape(opcion)}\b", base) for opcion in palabras)

    afirmativos = ["si", "dale", "claro", "obvio", "confirmo", "por supuesto", "hazlo", "hacelo", "apagala", "apagalo"]
    negativos = ["no", "mejor no", "cancelar", "cancela", "cancelo", "detene", "detenelo", "para"]

    if contiene(afirmativos):
        with shutdown_confirmation_lock:
            shutdown_confirmation_pending = False
        with follow_up_lock:
            follow_up_mode = False
        hud.log("Bueno, si insistís... Apagando en 5 segundos.")
        registrar_accion({"tipo": "atajo", "id": "apagar_equipo", "descripcion": "Apagando el equipo en 5 segundos."})
        _programar_apagado_confirmado()
        texto_acumulado = ""
        return True

    if contiene(negativos):
        with shutdown_confirmation_lock:
            shutdown_confirmation_pending = False
        with follow_up_lock:
            follow_up_mode = False
        hud.log("Listo, cancelo el apagado.")
        threading.Timer(2, hud.ocultar).start()
        texto_acumulado = ""
        return True

    hud.log('No te entendí. ¿Me confirmás con "sí" o "no"?')
    iniciar_follow_up(force_start=True)
    texto_acumulado = ""
    return True


def _detectar_atajo_teclado(texto: str) -> Optional[Dict[str, Any]]:
    if not texto:
        return None
    texto_norm = _sin_acentos(texto.lower())
    for atajo in ATAJOS_VOZ:
        for patron in atajo.get("patrones", []):
            if re.search(patron, texto_norm):
                return atajo
    return None


def ejecutar_atajo_teclado(atajo: Dict[str, Any]) -> bool:
    atajo_id = str(atajo.get("id") or "")
    if atajo_id == "apagar_equipo":
        return _iniciar_confirmacion_apagado()
    combos_raw = atajo.get("combos") or []
    combos: List[Tuple[str, ...]] = []
    for combo in combos_raw:
        if isinstance(combo, (list, tuple)):
            combos.append(tuple(str(k) for k in combo))
        else:
            combos.append((str(combo),))
    return _ejecutar_combos_teclado(combos)


def _obtener_atajo_por_id(atajo_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not atajo_id:
        return None
    return ATAJOS_IDS.get(atajo_id)

def _es_frase_fin_seguimiento(texto: str) -> bool:
    base = _sin_acentos(texto or "").strip()
    for frase in FOLLOW_UP_EXIT_FRASES:
        if base == frase:
            return True
        if base.startswith(frase + " ") or base.endswith(" " + frase):
            return True
    return False


def _mensaje_follow_up(mensaje: str) -> str:
    base = (mensaje or "").strip()
    if not base:
        return FOLLOW_UP_PROMPT
    if FOLLOW_UP_PROMPT.lower() in base.lower():
        return base
    return f"{base}\n{FOLLOW_UP_PROMPT}"


def notificar_y_activar_follow_up(mensaje: str) -> None:
    hud.log(_mensaje_follow_up(mensaje))
    iniciar_follow_up(force_start=True)


def finalizar_follow_up(mensaje: str) -> None:
    global follow_up_mode, texto_acumulado
    hud.log(mensaje)
    texto_acumulado = ""
    with follow_up_lock:
        follow_up_mode = False
    threading.Timer(2, hud.ocultar).start()


def iniciar_follow_up(force_start: bool = False) -> None:
    global follow_up_mode
    with follow_up_lock:
        if follow_up_mode:
            return
        if not force_start:
            return
        follow_up_mode = True
    threading.Thread(target=_ciclo_follow_up, daemon=True).start()


def _ciclo_follow_up() -> None:
    global follow_up_mode, texto_acumulado
    while True:
        texto = escuchar_fragmento()
        if not texto:
            continue
        texto = texto.strip()
        if not texto:
            continue
        if _es_frase_fin_seguimiento(texto):
            finalizar_follow_up("Listo, cualquier cosa avisame.")
            break
        texto_acumulado = texto
        enviar_mensaje_final()
        with follow_up_lock:
            if not follow_up_mode:
                break


def _detectar_texto_a_escribir(texto: str) -> Optional[str]:
    if not texto:
        return None
    patron = re.compile(
        r"\b(escrib(?:i|í|o|a|ir|ime|eme|ile?s?|les)|escribe(?:le|les)?|escribime|escribeme|escribilo|tipe(?:a|á|ame|alo|ala)|redact(?:a|á|ame|alo|ar|ales))\s+(?P<contenido>.+)",
        re.IGNORECASE,
    )
    match = patron.search(texto.strip())
    if not match:
        return None
    contenido = match.group("contenido").strip()
    if not contenido:
        return None
    quote_chars = {'"', "'", "“", "”", "«", "»"}
    if contenido[0] in quote_chars and contenido[-1:] == contenido[0]:
        contenido = contenido[1:-1].strip()
    return contenido


def _preparar_texto_escribir(contenido: str) -> str:
    base = contenido.strip()
    if not base:
        return base
    patron_mensaje = re.compile(
        r"^un mensaje a (?P<dest>.+?) (?:para que|para|pidi(?:é|e)ndoles que|pidi(?:é|e)ndole que|dici(?:é|e)ndoles que|dici(?:é|e)ndole que|que)\s+(?P<body>.+)",
        re.IGNORECASE,
    )
    match = patron_mensaje.match(base)
    if match:
        dest = match.group("dest").strip()
        body = match.group("body").strip()
        dest_formateado = dest.capitalize() if dest else "todos"
        if body:
            body_limpio = body.strip()
            if not body_limpio.endswith((".", "!", "?")):
                body_limpio = body_limpio.rstrip(".") + "."
            cuerpo = body_limpio[0].upper() + body_limpio[1:]
        else:
            cuerpo = ""
        if cuerpo:
            return f"Hola {dest_formateado}, {cuerpo}"
        return f"Hola {dest_formateado}, ¿todo bien?"
    if not base.endswith((".", "!", "?")):
        base = base + "."
    return base


def interpretar_intencion_con_cohere(mensaje: str) -> Optional[Dict[str, Any]]:
    cliente = obtener_cliente_cohere()
    if not cliente:
        return None
    instrucciones = (
        "Eres Cogere, analista de órdenes. Clasifica la solicitud del usuario.\n"
        "Responde únicamente con JSON. Campos obligatorios:\n"
        "- tipo: uno de [escribir_texto, abrir_app, cerrar_app, atajo, comandos, respuesta, ninguno]\n"
        "- razon: explicación breve.\n"
        "Campos adicionales:\n"
        "* escribir_texto: agrega \"contenido\" (texto listo para escribir).\n"
        "* abrir_app / cerrar_app: agrega \"objetivo\" (nombre de la app o alias encontrado).\n"
        "* atajo: agrega \"atajo_id\" usando uno de estos IDs: "
        + ", ".join(sorted(ATAJOS_IDS.keys()))
        + ".\n"
        "* comandos: opcionalmente \"contexto\" o \"nota\" para guiar la generación de comandos.\n"
        "* respuesta: agrega \"texto\" con la respuesta natural en español.\n"
        "Si no procede ninguna acción, responde tipo=ninguno.\n"
        "No agregues texto fuera del JSON."
    )
    prompt = f"{instrucciones}\nOrden del usuario: \"{mensaje.strip()}\""
    try:
        respuesta = cliente.chat(
            model=COHERE_MODEL,
            message=prompt,
            temperature=0.1,
        )
        texto = ""
        if hasattr(respuesta, "text") and respuesta.text:
            texto = respuesta.text.strip()
        elif hasattr(respuesta, "message"):
            contenido = getattr(respuesta.message, "content", [])
            partes: List[str] = []
            for bloque in contenido or []:
                if isinstance(bloque, dict) and bloque.get("type") == "text":
                    partes.append(str(bloque.get("text", "")))
            texto = "".join(partes).strip()
        elif hasattr(respuesta, "output_text"):
            texto = (respuesta.output_text or "").strip()
        datos = _extraer_json(texto)
        if not datos:
            logger.debug("Interpretación Cohere inválida: %s", texto)
            return None
        return datos
    except Exception as exc:
        logger.error(f"Cohere interpretador falló: {exc}")
        return None


def _detectar_intencion_catalogo(texto: str) -> Optional[tuple[str, str]]:
    texto_base = texto or ""
    texto_normalizado = _sin_acentos(texto_base)
    patrones = [
        (r"\b(abrir|abri|abre|abrime|iniciar|enciende|encender)\s+([^\.,;]+)", "abrir"),
        (r"\b(cerrar|cerra|cerrame|termina|detener)\s+([^\.,;]+)", "cerrar"),
    ]
    for patron, accion in patrones:
        match = re.search(patron, texto_normalizado, re.IGNORECASE)
        if not match:
            continue
        inicio = match.start(2)
        fin = match.end(2)
        fragmento_original = texto_base[inicio:fin].strip()
        fragmento_normalizado = texto_normalizado[inicio:fin].strip()
        fragmento = fragmento_original or fragmento_normalizado
        if not fragmento:
            continue
        # Corto en conectores comunes, utilizando el fragmento sin acentos para buscar.
        fragmento_sin_acentos = _sin_acentos(fragmento)
        for separador in [" y ", " luego ", " despues ", " entonces ", ",", ".", ";"]:
            separador_busqueda = separador.strip()
            pos = fragmento_sin_acentos.find(separador_busqueda)
            if pos > 0:
                fragmento = fragmento[:pos].strip()
                fragmento_sin_acentos = fragmento_sin_acentos[:pos].strip()
                break
        if fragmento:
            return accion, fragmento
    return None


def _es_pedido_repeticion(texto: str) -> bool:
    if not texto:
        return False
    texto_norm = _sin_acentos(texto.lower())
    patrones = [
        r"\blo\s+mismo\s+que\s+antes\b",
        r"\b(lo|haz|hace|haceme|hacelo)\s+(de\s+)?(nuevo|igual)\b",
        r"\brepite\s+lo\s+(anterior|mismo)\b",
        r"\blo\s+de\s+(reci[eé]n|antes)\b",
    ]
    return any(re.search(patron, texto_norm) for patron in patrones)





def _detectar_ajuste_redaccion(texto: str) -> bool:
    if not texto or not _hay_redaccion_en_memoria():
        return False
    texto_norm = _sin_acentos(texto.lower())
    gatillos = [
        "mas largo",
        "mas extenso",
        "mas completo",
        "mas detallado",
        "mas formal",
        "mas informal",
        "mas amigable",
        "mas amable",
        "mas profesional",
        "mas serio",
        "mas motivador",
        "mas entusiasta",
        "mas cercano",
        "mas calido",
        "mas breve",
        "mas corto",
        "mas simple",
        "mas claro",
        "mas resumido",
        "menos largo",
        "menos formal",
        "menos serio",
        "menos rigido",
        "otro mensaje",
        "otra version",
        "otro texto",
    ]
    if any(frase in texto_norm for frase in gatillos):
        return True
    patrones = [
        r"\b(agrega|agregale|sumale|anadile|anadele|incorporale|incluyele)\b",
        r"\b(extendelo|amplialo|alargalo|acortalo|reformulalo|reescribilo|cambialo|modificalo|ajustalo|mejoralo)\b",
        r"\b(extendela|ampliala|alargala|acortala|reformulala|reescribila|cambiala|modificala|ajustala|mejorala)\b",
        r"\b(hacelo|hazlo|ponelo|dejalo)\s+mas\b",
        r"\b(hacelo|hazlo|ponelo|dejalo)\s+menos\b",
        r"\bque\s+el\s+(mensaje|texto)\s+sea\b",
        r"\b(mensaje|texto|redaccion)\s+nuevo\b",
    ]
    if any(re.search(patron, texto_norm) for patron in patrones):
        return True
    if texto_norm.startswith("mas ") or texto_norm.startswith("menos "):
        return True
    if re.search(r"\b(mensaje|texto|redaccion)\b", texto_norm) and re.search(r"\b(mas|menos|otro|diferente|distinto|igual|formal|informal)\b", texto_norm):
        return True
    return False


def _repetir_ultima_accion() -> Tuple[bool, str]:
    if not historial_acciones:
        return False, "No recuerdo una acción previa todavía."
    ultima = historial_acciones[-1]
    tipo = ultima.get("tipo")
    if tipo == "ventana":
        accion = ultima.get("accion")
        objetivo = ultima.get("objetivo")
        if not accion or not objetivo:
            return False, "No pude repetir la acción de ventana."
        try:
            ejecutar_accion_ventana(accion, objetivo)
            registrar_accion({"tipo": "ventana", "accion": accion, "objetivo": objetivo})
            return True, f"Repetí la acción de ventana: {accion} {objetivo}"
        except RuntimeError as exc:
            return False, str(exc)
    if tipo == "atajo":
        combos = ultima.get("combos")
        descripcion = ultima.get("descripcion") or "Atajo de teclado repetido."
        if not combos:
            return False, "No tengo guardado el atajo anterior."
        combos_tuplas: List[Tuple[str, ...]] = []
        for combo in combos:
            if isinstance(combo, (list, tuple)):
                combos_tuplas.append(tuple(str(k) for k in combo))
            else:
                combos_tuplas.append((str(combo),))
        if _ejecutar_combos_teclado(combos_tuplas):
            registrar_accion({"tipo": "atajo", "combos": combos_tuplas, "descripcion": descripcion})
            return True, descripcion
        return False, "El atajo anterior falló al repetirse."
    if tipo == "texto":
        contenido = ultima.get("texto")
        if not contenido:
            return False, "No tengo qué escribir."
        try:
            pyautogui.write(contenido)
            registrar_accion({"tipo": "texto", "texto": contenido})
            return True, f"Volví a escribir: {contenido}"
        except Exception as exc:
            return False, f"No pude escribir otra vez: {exc}"
    if tipo == "respuesta":
        contenido = ultima.get("texto") or ultima.get("respuesta") or ""
        if not contenido:
            return False, "No tengo qué responder."
        hud.log(contenido)
        registrar_accion({"tipo": "respuesta", "texto": contenido})
        return True, contenido
    if tipo == "comandos":
        comandos = ultima.get("comandos")
        if not comandos:
            return False, "No encuentro los comandos anteriores."
        if ejecutar_comandos_en_cadena(comandos):
            registrar_accion({"tipo": "comandos", "comandos": comandos})
            return True, "Repetí los comandos anteriores."
        return False, "Los comandos anteriores fallaron al repetirse."
    return False, "No pude interpretar la última acción."


def _detectar_accion_ventana(texto: str) -> Optional[tuple[str, str]]:
    texto_base = texto or ""
    texto_normalizado = _sin_acentos(texto_base)
    patrones = [
        (
            r"\b(maximiza(?:r|me|lo|la)?|agranda|pon[ei] en pantalla completa)\s+([^\.,;]+)",
            "maximizar",
        ),
        (
            r"\b(minimiza(?:r|me|lo|la)?|achica|reduce)\s+([^\.,;]+)",
            "minimizar",
        ),
        (
            r"\b(enfoca(?:r|me|la)?|pon[ei] al frente|trae al frente)\s+([^\.,;]+)",
            "enfocar",
        ),
    ]
    for patron, accion in patrones:
        match = re.search(patron, texto_normalizado, re.IGNORECASE)
        if not match:
            continue
        inicio = match.start(2)
        fin = match.end(2)
        fragmento_original = texto_base[inicio:fin].strip()
        fragmento_normalizado = texto_normalizado[inicio:fin].strip()
        fragmento = fragmento_original or fragmento_normalizado
        if not fragmento:
            continue
        fragmento_sin_acentos = _sin_acentos(fragmento)
        for separador in [" y ", " luego ", " despues ", " entonces ", ",", ".", ";"]:
            separador_busqueda = separador.strip()
            pos = fragmento_sin_acentos.find(separador_busqueda)
            if pos > 0:
                fragmento = fragmento[:pos].strip()
                fragmento_sin_acentos = fragmento_sin_acentos[:pos].strip()
                break
        if fragmento:
            fragmento = re.sub(r"^(la|el)\s+", "", fragmento, flags=re.IGNORECASE)
            fragmento = re.sub(r"^(ventana|aplicacion|app)\s+de\s+", "", fragmento, flags=re.IGNORECASE)
            fragmento = re.sub(r"^(ventana|aplicacion|app)\s+", "", fragmento, flags=re.IGNORECASE)
            fragmento = fragmento.strip()
            return accion, fragmento
    return None


def comando_abrir_desde_app(app: Dict[str, Any]) -> Optional[str]:
    tipo = str(app.get("tipo") or app.get("type") or "").lower()
    launch = str(app.get("launch") or "").strip()
    acciones = app.get("acciones") or {}
    if isinstance(acciones, dict):
        launch_accion = str(acciones.get("abrir") or "").strip()
        if launch_accion:
            return launch_accion.replace("%USERNAME%", usuario)
    if not launch:
        return None
    if tipo == "exe":
        return f'start "" "{launch.replace("%USERNAME%", usuario)}"'
    return launch


def enviar_mensaje_final(timeout: int = 5) -> None:  # timeout se mantiene por compatibilidad
    del timeout
    global texto_acumulado
    if not texto_acumulado:
        logger.warning("No hay texto para enviar.")
        return

    mensaje = texto_acumulado.strip()
    if shutdown_confirmation_pending:
        if _procesar_confirmacion_apagado(mensaje):
            texto_acumulado = ""
            return

    if _es_pedido_repeticion(mensaje):
        ok, mensaje_historial = _repetir_ultima_accion()
        hud.log(mensaje_historial)
        delay = 6 if historial_acciones and historial_acciones[-1].get("tipo") == "respuesta" else 2
        texto_acumulado = ""
        threading.Timer(delay, hud.ocultar).start()
        return

    if _detectar_ajuste_redaccion(mensaje):
        nuevo_texto = generar_redaccion_desde_memoria(mensaje)
        if nuevo_texto:
            try:
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.05)
                pyautogui.press("backspace")
                time.sleep(0.05)
                pyautogui.write(nuevo_texto)
            except Exception as exc:
                hud.log(f"No pude actualizar el texto: {exc}")
                texto_acumulado = ""
                threading.Timer(2, hud.ocultar).start()
                return
            registrar_accion({"tipo": "texto", "texto": nuevo_texto})
            _actualizar_texto_memoria(nuevo_texto)
            _agregar_instruccion_memoria(mensaje)
            texto_acumulado = ""
            logger.info("Texto ajustado a partir de la memoria de redacción.")
            notificar_y_activar_follow_up("Actualicé el mensaje según tu pedido.")
            return
        hud.log("No pude ajustar el mensaje anterior.")
        texto_acumulado = ""
        threading.Timer(2, hud.ocultar).start()
        return

    interpretacion = interpretar_intencion_con_cohere(mensaje)
    interpret_tipo = ""
    if interpretacion:
        interpret_tipo = str(interpretacion.get("tipo") or "").lower()
        logger.info(
            "Interpretación Cohere: tipo=%s razon=%s",
            interpret_tipo or "desconocido",
            interpretacion.get("razon"),
        )

    contexto_app: Optional[Dict[str, Any]] = None
    catalogo_ref: Optional[Dict[str, Any]] = None
    accion_objetivo: Optional[str] = None
    nombre_objetivo: Optional[str] = None

    if interpret_tipo == "respuesta":
        texto_respuesta = str((interpretacion.get("texto") if interpretacion else "") or "").strip()
        if not texto_respuesta:
            texto_respuesta = generar_respuesta_con_cohere(mensaje) or "Perdón, ¿podrías repetirme?"
        registrar_accion({"tipo": "respuesta", "texto": texto_respuesta})
        texto_acumulado = ""
        notificar_y_activar_follow_up(texto_respuesta)
        return

    if interpret_tipo == "escribir_texto":
        contenido = str((interpretacion.get("contenido") if interpretacion else "") or "").strip()
        if contenido:
            try:
                texto_formateado = _preparar_texto_escribir(contenido)
                pyautogui.write(texto_formateado)
                registrar_accion({"tipo": "texto", "texto": texto_formateado})
                _reiniciar_memoria_redaccion(texto_formateado, mensaje)
                mensaje_escritura = f"Escribiendo: {texto_formateado}"
                texto_acumulado = ""
                notificar_y_activar_follow_up(mensaje_escritura)
                return
            except Exception as exc:
                hud.log(f"No pude escribir: {exc}")
        else:
            hud.log("Cohere no envió contenido para escribir.")
        texto_acumulado = ""
        threading.Timer(2, hud.ocultar).start()
        return

    if interpret_tipo == "atajo":
        atajo_interpretado = _obtener_atajo_por_id((interpretacion or {}).get("atajo_id"))
        if atajo_interpretado and ejecutar_atajo_teclado(atajo_interpretado):
            texto_acumulado = ""
            if not atajo_interpretado.get("manual_follow_up"):
                descripcion_atajo = atajo_interpretado.get("descripcion") or "Atajo ejecutado."
                registrar_accion({
                    "tipo": "atajo",
                    "combos": atajo_interpretado.get("combos", []),
                    "descripcion": descripcion_atajo,
                })
                notificar_y_activar_follow_up(descripcion_atajo)
            return
        if interpretacion:
            logger.warning(
                "Atajo indicado por Cohere no reconocido: %s",
                interpretacion.get("atajo_id"),
            )

    if interpret_tipo in {"abrir_app", "cerrar_app"}:
        accion_objetivo = "abrir" if interpret_tipo == "abrir_app" else "cerrar"
        nombre_objetivo = str((interpretacion or {}).get("objetivo") or "").strip()
        if nombre_objetivo:
            catalogo_ref = cargar_catalogo()
            contexto_app = _buscar_app(catalogo_ref, nombre_objetivo)

    mensaje_para_cohere = mensaje
    if interpret_tipo == "comandos" and interpretacion:
        nota = (interpretacion.get("contexto") or interpretacion.get("nota") or "").strip()
        if nota:
            mensaje_para_cohere = f"{mensaje}\nNota: {nota}"

    if interpret_tipo != "atajo":
        atajo = _detectar_atajo_teclado(mensaje)
        if atajo:
            logger.info("Atajo de teclado detectado: %s", atajo.get("id"))
            if ejecutar_atajo_teclado(atajo):
                texto_acumulado = ""
                if not atajo.get("manual_follow_up"):
                    registrar_accion({
                        "tipo": "atajo",
                        "combos": atajo.get("combos", []),
                        "descripcion": atajo.get("descripcion"),
                    })
                    notificar_y_activar_follow_up(atajo.get("descripcion") or "Atajo ejecutado.")
                return
            hud.log("No pude ejecutar el atajo.")
            texto_acumulado = ""
            threading.Timer(2, hud.ocultar).start()
            return

    if interpret_tipo != "escribir_texto":
        texto_a_escribir = _detectar_texto_a_escribir(mensaje)
        if texto_a_escribir:
            try:
                texto_formateado = _preparar_texto_escribir(texto_a_escribir)
                pyautogui.write(texto_formateado)
            except Exception as exc:
                hud.log(f"No pude escribir: {exc}")
                texto_acumulado = ""
                threading.Timer(2, hud.ocultar).start()
                return
            registrar_accion({"tipo": "texto", "texto": texto_formateado})
            _reiniciar_memoria_redaccion(texto_formateado, mensaje)
            texto_acumulado = ""
            notificar_y_activar_follow_up(f"Escribiendo: {texto_formateado}")
            return

    accion_ventana = _detectar_accion_ventana(mensaje)
    if accion_ventana:
        accion, objetivo = accion_ventana
        logger.info("Acción de ventana detectada: %s -> %s", accion, objetivo)
        try:
            ejecutar_accion_ventana(accion, objetivo)
        except RuntimeError as exc:
            hud.log(str(exc))
        else:
            registrar_accion({"tipo": "ventana", "accion": accion, "objetivo": objetivo})
            mensajes_ok = {
                "maximizar": f"Ventana maximizada: {objetivo}",
                "minimizar": f"Ventana minimizada: {objetivo}",
                "enfocar": f"Ventana enfocada: {objetivo}",
            }
            mensaje_ventana = mensajes_ok.get(accion, f"Accion sobre ventana completada: {objetivo}")
            texto_acumulado = ""
            notificar_y_activar_follow_up(mensaje_ventana)
            return
        texto_acumulado = ""
        threading.Timer(2, hud.ocultar).start()
        return

    if not nombre_objetivo:
        intencion = _detectar_intencion_catalogo(mensaje)
        if intencion:
            accion_objetivo, nombre_objetivo = intencion
            catalogo_ref = cargar_catalogo()
            if nombre_objetivo:
                contexto_app = _buscar_app(catalogo_ref, nombre_objetivo)

    logger.info("Consultando Cohere para la orden: %s", mensaje_para_cohere)

    sugerencia = generar_comandos_con_cohere(
        mensaje_para_cohere,
        contexto_app=contexto_app,
        catalogo_actual=catalogo_ref,
    )
    if sugerencia:
        descripcion = str(sugerencia.get("descripcion") or "").strip()
        comandos = list(sugerencia["comandos"])
        if accion_objetivo == "abrir" and contexto_app:
            comando_catalogo = comando_abrir_desde_app(contexto_app) or contexto_app.get("comando")
            if comando_catalogo:
                comandos[0] = comando_catalogo
        comandos_generados = ";".join(comandos)
        if ejecutar_comandos_en_cadena(comandos_generados):
            registrar_accion({"tipo": "comandos", "comandos": comandos_generados})
            texto_acumulado = ""
            mensaje_exitoso = descripcion or "Acción completada."
            notificar_y_activar_follow_up(mensaje_exitoso)
            return
        logger.warning("Los comandos sugeridos por Cohere fallaron: %s", comandos_generados)

    respuesta_conversacional = generar_respuesta_con_cohere(mensaje)
    if respuesta_conversacional:
        registrar_accion({"tipo": "respuesta", "texto": respuesta_conversacional})
        texto_acumulado = ""
        notificar_y_activar_follow_up(respuesta_conversacional)
        return
    hud.log("No pude interpretar la orden.")
    texto_acumulado = ""
    threading.Timer(2, hud.ocultar).start()
def enviar_mensaje_final_automatico() -> None:
    timeout = 60 if es_pregunta_larga(texto_acumulado) else 5
    enviar_mensaje_final(timeout=timeout)


def iniciar_escucha_segura() -> None:
    reinicios = 0
    while True:
        try:
            escuchar_hotword()
        except Exception as exc:
            reinicios += 1
            logger.error("Error en escucha_hotword: %s. Reinicio #%s en 3 segundos...", exc, reinicios)
            time.sleep(3)


def main() -> None:
    asegurar_catalogo()
    try:
        print("Iniciando thread de escucha hotword segura...")
        escucha_thread = threading.Thread(target=iniciar_escucha_segura, daemon=True)
        escucha_thread.start()
        print("Iniciando HUD (mainloop)...")
        hud.iniciar_hud()
    except Exception as exc:
        print(f"ERROR EN MAIN: {exc}")


if __name__ == "__main__":
    if not _inicializar_ipc_consola():  # Si la inicialización IPC devuelve False, somos una instancia auxiliar y debemos finalizar de inmediato.
        sys.exit(0)  # Cerramos el proceso actual para evitar duplicar lógica cuando solo se pretendía adjuntar la consola.
    main()  # Continuamos con la ejecución normal cuando actuamos como instancia principal.
