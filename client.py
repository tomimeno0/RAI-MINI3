"""Cliente principal de RAI-MINI.

Escucha la hotword, envía las peticiones al servidor local y ejecuta acciones
usando un catálogo JSON. Si falta una acción en el catálogo, se apoya en Cohere
para generar comandos de PowerShell/teclado/ventanas y los persiste.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import keyboard  # type: ignore
import psutil
import pyautogui  # type: ignore
import pygetwindow as gw  # type: ignore
import speech_recognition as sr

import hud
from hud import log

try:
    import cohere
except Exception:  # pragma: no cover - dependencia opcional
    cohere = None


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rai.client")
usuario = os.getlogin()
texto_acumulado = ""
CATALOGO_PATH = Path(__file__).with_name("apps.json")
COHERE_LOG_PATH = Path(__file__).with_name("cohere.log")
catalogo_lock = threading.Lock()
_catalogo_cache: Optional[Dict[str, Any]] = None
COHERE_MODEL = os.getenv("COHERE_MODEL", "command-r-plus-08-2024")
COHERE_API_KEY = "ppBVjJhTQ1vCU7WVBKt1wYKpDUZW97LhZ1PrHsBJ"
_cohere_client: Optional["cohere.Client"] = None


def _normalizar(texto: str) -> str:
    return re.sub(r"\s+", " ", texto.strip().lower())


def _asegurar_catalogo_unlocked() -> Dict[str, Any]:
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


def _log_cohere_event(titulo: str, contenido: str) -> None:
    marca = datetime.datetime.now().isoformat(timespec="seconds")
    linea = f"[{marca}] {titulo}\n{contenido}\n{'-' * 60}\n"
    try:
        with COHERE_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(linea)
    except Exception:
        logger.debug("No pude escribir el log de Cohere.")


def cargar_catalogo() -> Dict[str, Any]:
    with catalogo_lock:
        return _asegurar_catalogo_unlocked()


def asegurar_catalogo() -> None:
    with catalogo_lock:
        catalogo = _asegurar_catalogo_unlocked()
        if not CATALOGO_PATH.exists():
            _guardar_catalogo_unlocked(catalogo)


def obtener_cliente_cohere() -> Optional["cohere.Client"]:
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
    nombres: List[str] = []
    for app in catalogo.get("aplicaciones", []):
        nombre = str(app.get("nombre") or app.get("id") or "").strip()
        if nombre:
            nombres.append(nombre)
        if len(nombres) >= 40:
            break
    bloques = [", ".join(nombres) if nombres else "(sin catálogo cargado)"]
    if app_obj:
        detalles = {
            "nombre": app_obj.get("nombre"),
            "tipo": app_obj.get("tipo"),
            "paths": app_obj.get("paths"),
            "acciones": list((app_obj.get("acciones") or {}).keys()),
        }
        bloques.append(json.dumps(detalles, ensure_ascii=False))
    return "\n".join(bloques)


def _extraer_json(texto: str) -> Optional[Dict[str, Any]]:
    match = re.search(r"\{.*\}", texto, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def generar_comandos_con_cohere(
    peticion: str,
    *,
    contexto_app: Optional[Dict[str, Any]] = None,
    catalogo_actual: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    cliente = obtener_cliente_cohere()
    if not cliente:
        return None

    catalogo = catalogo_actual or cargar_catalogo()
    contexto_catalogo = _componer_contexto_catalogo(catalogo, contexto_app)
    instrucciones = (
        "Eres un asistente que genera comandos exactos para Windows.\n"
        "Dispones de un catálogo JSON con aplicaciones instaladas. Cada entrada contiene:\n"
        "- nombre/id\n"
        '- tipo: "exe" o "uwp"\n'
        "- launch: ruta o comando EXACTO para abrir la app\n"
        "- paths: rutas de instalación\n"
        "- acciones: comandos conocidos (abrir, cerrar, etc.)\n"
        "Al generar comandos:\n"
        "1. Usa siempre la ruta de launch/paths cuando exista.\n"
        '   - Si lanzas un .exe, responde con `start "" \"RUTA\"` o directamente `"RUTA"`.\n'
        "2. No inventes rutas ni dependas de `start appname` genérico.\n"
        "3. Para apps UWP DEBES devolver exactamente `explorer.exe shell:appsFolder\\<AppUserModelID>` (incluye la barra invertida).\n"
        "4. Si necesitas varias acciones, colócalas en orden en la lista.\n"
        "5. Responde únicamente el JSON siguiente, sin texto adicional:\n"
        '{\n  "comandos": ["..."],\n  "descripcion": "explicacion breve en español"\n}\n'
        "Si no puedes ayudar, devuelve `\"comandos\": []`."
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

    datos = _extraer_json(respuesta_texto)
    if not datos:
        logger.warning(f"Cohere devolvió un formato inesperado: {respuesta_texto}")
        return None

    comandos = datos.get("comandos")
    if not isinstance(comandos, list):
        logger.warning("Cohere devolvió comandos inválidos.")
        return None

    comandos_filtrados = [cmd.strip() for cmd in comandos if isinstance(cmd, str) and cmd.strip()]
    if not comandos_filtrados:
        return None

    descripcion = datos.get("descripcion")
    if not isinstance(descripcion, str):
        descripcion = ""

    try:
        _log_cohere_event("COMANDOS PARSEADOS", json.dumps({"comandos": comandos_filtrados, "descripcion": descripcion}, ensure_ascii=False, indent=2))
    except Exception:
        logger.debug("No pude registrar el resultado parseado.")

    return {"comandos": comandos_filtrados, "descripcion": descripcion}


def _buscar_app(catalogo: Dict[str, Any], nombre_app: str) -> Optional[Dict[str, Any]]:
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


def ejecutar_accion_desde_catalogo(nombre_app: str, tipo_accion: str) -> bool:
    tipo_objetivo = tipo_accion.lower()
    catalogo = cargar_catalogo()
    app = _buscar_app(catalogo, nombre_app)
    if not app:
        logger.warning(f"No encontré la app '{nombre_app}' en el catálogo.")
        return False

    acciones = app.get("acciones") or {}
    if not isinstance(acciones, dict):
        acciones = {}
    comando = acciones.get(tipo_objetivo)
    descripcion_generada = ""

    if not comando:
        logger.info(f"No hay acción '{tipo_accion}' para '{nombre_app}'. Consulto a Cohere.")
        generado = generar_comandos_con_cohere(
            f"{tipo_objetivo} {nombre_app}",
            contexto_app=app,
            catalogo_actual=catalogo,
        )
        if not generado:
            logger.warning(f"No pude obtener un comando para '{tipo_accion}' de '{nombre_app}'.")
            return False
        comando = generado["comandos"][0]
        descripcion_generada = generado.get("descripcion", "")
        with catalogo_lock:
            catalogo_ref = _asegurar_catalogo_unlocked()
            app_ref = _buscar_app(catalogo_ref, nombre_app)
            if app_ref is not None:
                acciones_ref = app_ref.setdefault("acciones", {})  # type: ignore[assignment]
                if isinstance(acciones_ref, dict):
                    acciones_ref[tipo_objetivo] = comando
                    _guardar_catalogo_unlocked(catalogo_ref)

    if descripcion_generada:
        hud.log(descripcion_generada)
    if ejecutar_comando_cmd(comando):
        logger.info(f"Acción '{tipo_accion}' ejecutada sobre '{nombre_app}'.")
        return True

    logger.warning(f"Falló el comando de '{tipo_accion}' sobre '{nombre_app}'.")
    return False


def actualizar_ultima_vez(nombre_app: str) -> None:
    marca_temporal = datetime.datetime.now().isoformat(timespec="seconds")
    with catalogo_lock:
        catalogo = _asegurar_catalogo_unlocked()
        app = _buscar_app(catalogo, nombre_app)
        if not app:
            logger.debug(f"Catálogo sin coincidencia para '{nombre_app}'.")
            return
        app["ultima_vez"] = marca_temporal
        _guardar_catalogo_unlocked(catalogo)


def escaner_inteligente(tipo: str) -> None:
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
        ventana = next((w for w in gw.getWindowsWithTitle(nombre_ventana)), None)
        if ventana:
            if accion == "maximizar":
                ventana.maximize()
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
        "Hola, soy RAI. ¿En qué puedo ayudarte?",
        estado="procesando",
        after=despues_del_typing,
    )


def escuchar_fragmento() -> Optional[str]:
    recognizer = sr.Recognizer()
    with sr.Microphone() as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.5)
        audio = recognizer.listen(source, phrase_time_limit=5)
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


def _detectar_intencion_catalogo(texto: str) -> Optional[tuple[str, str]]:
    patrones = [
        (r"\b(abrir|iniciar)\s+([^\.,;]+)", "abrir"),
        (r"\b(cerrar)\s+([^\.,;]+)", "cerrar"),
    ]
    for patron, accion in patrones:
        match = re.search(patron, texto, re.IGNORECASE)
        if not match:
            continue
        fragmento = match.group(2).strip()
        if not fragmento:
            continue
        # Corto en conectores comunes
        for separador in [" y ", " luego ", " despues ", " entonces ", ",", ".", ";"]:
            pos = fragmento.lower().find(separador.strip())
            if pos > 0:
                fragmento = fragmento[:pos].strip()
                break
        if fragmento:
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
    del timeout  # no se usa sin servidor
    global texto_acumulado
    if not texto_acumulado:
        logger.warning("No hay texto para enviar.")
        return

    mensaje = texto_acumulado.strip()
    logger.info("Consultando Cohere para la orden: %s", mensaje)

    contexto_app = None
    catalogo_ref: Optional[Dict[str, Any]] = None
    accion_objetivo: Optional[str] = None
    nombre_objetivo: Optional[str] = None

    intencion = _detectar_intencion_catalogo(mensaje)
    if intencion:
        accion_objetivo, nombre_objetivo = intencion
        catalogo_ref = cargar_catalogo()
        if nombre_objetivo:
            contexto_app = _buscar_app(catalogo_ref, nombre_objetivo)

    sugerencia = generar_comandos_con_cohere(
        mensaje,
        contexto_app=contexto_app,
        catalogo_actual=catalogo_ref,
    )
    if sugerencia:
        descripcion = sugerencia.get("descripcion")
        if descripcion:
            hud.log(descripcion)
        comandos = list(sugerencia["comandos"])
        if accion_objetivo == "abrir" and contexto_app:
            comando_catalogo = comando_abrir_desde_app(contexto_app) or contexto_app.get("comando")
            if comando_catalogo:
                comandos[0] = comando_catalogo
        comandos_generados = ";".join(comandos)
        if ejecutar_comandos_en_cadena(comandos_generados):
            texto_acumulado = ""
            threading.Timer(2, hud.ocultar).start()
            return
        logger.warning("Los comandos sugeridos por Cohere fallaron: %s", comandos_generados)

    if accion_objetivo and nombre_objetivo:
        logger.info("Cohere falló, intento resolver '%s %s' desde el catálogo.", accion_objetivo, nombre_objetivo)
        if accion_objetivo == "abrir" and contexto_app:
            comando_final = comando_abrir_desde_app(contexto_app) or contexto_app.get("comando")
            if comando_final:
                hud.log(f"Ejecutando [ {nombre_objetivo} ]...")
                if ejecutar_comando_cmd(comando_final):
                    hud.log(f"Listo, {nombre_objetivo} fue abierto.")
                    actualizar_ultima_vez(nombre_objetivo)
                    texto_acumulado = ""
                    threading.Timer(2, hud.ocultar).start()
                    return
        elif accion_objetivo == "cerrar":
            if ejecutar_accion_desde_catalogo(nombre_objetivo, "cerrar"):
                hud.log(f"Listo, {nombre_objetivo} fue cerrado.")
                texto_acumulado = ""
                threading.Timer(2, hud.ocultar).start()
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
    main()
