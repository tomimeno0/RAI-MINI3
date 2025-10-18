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
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

try:
    import cohere
except Exception:  # pragma: no cover - dependencia opcional
    cohere = None


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rai.client")
usuario = os.getlogin()
texto_acumulado = ""
CATALOGO_PATH = Path(__file__).with_name("apps.json")
catalogo_lock = threading.Lock()
_catalogo_cache: Optional[Dict[str, Any]] = None
COHERE_MODEL = os.getenv("COHERE_MODEL", "command-r-plus")
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

    api_key = os.getenv("ul9qI4KYIpzGJXxM2hHHBTHdAdLjxBJZPXWc0YDm") # COHERE_API_KEY
    if not api_key:
        logger.warning("COHERE_API_KEY no está definido en el entorno.")
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
        "Genera solo un JSON con esta forma:\n"
        '{\n  "comandos": ["..."],\n  "descripcion": "explicacion breve en español"\n}\n'
        "- Cada comando debe ser válido para cmd.exe o PowerShell, o usar los prefijos soportados:\n"
        '  * "tecla:<combos>" para accesos rápidos (ejemplo: "tecla:win+d").\n'
        '  * "ventana:<accion>:<nombre>" para minimizar/maximizar/enfocar una ventana.\n'
        "- Para cerrar procesos usa taskkill adecuado.\n"
        "- Si necesitas varias acciones, pon cada comando en la lista en orden.\n"
        "- Responde únicamente el JSON, sin texto adicional.\n"
        "- Si no puedes ayudar, responde con \"comandos\": []."
    )
    prompt = (
        f"{instrucciones}\n"
        f"Aplicaciones conocidas:\n{contexto_catalogo}\n"
        f"Solicitud del usuario: \"{peticion.strip()}\""
    )

    respuesta_texto = ""
    try:
        respuesta = cliente.generate(
            model=COHERE_MODEL,
            prompt=prompt,
            max_tokens=200,
            temperature=0.1,
            stop_sequences=[],
        )
        if respuesta.generations:
            respuesta_texto = (respuesta.generations[0].text or "").strip()
    except AttributeError:
        try:
            respuesta_chat = cliente.chat(model=COHERE_MODEL, message=prompt, temperature=0.1)
            respuesta_texto = getattr(respuesta_chat, "text", "") or ""
        except Exception as exc:
            logger.error(f"Cohere chat falló: {exc}")
            return None
    except Exception as exc:
        logger.error(f"Cohere generate falló: {exc}")
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
        comando = comando.replace("TuUsuario", usuario).replace("%USERNAME%", usuario)

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


def enviar_mensaje_final(timeout: int = 5) -> None:  # timeout se mantiene por compatibilidad
    del timeout  # no se usa sin servidor
    global texto_acumulado
    if not texto_acumulado:
        logger.warning("No hay texto para enviar.")
        return

    mensaje = texto_acumulado.strip()
    logger.info("Procesando orden local: %s", mensaje)

    intencion = _detectar_intencion_catalogo(mensaje)
    if intencion:
        accion, nombre_app = intencion
        if accion == "abrir":
            resultado = buscar_comando_por_nombre(nombre_app)
            logger.debug("Resultado buscar_comando_por_nombre: %s", resultado)
            if not resultado or any(r is None for r in resultado):
                logger.warning("No encontré comando válido para '%s'. Intento con Cohere.", nombre_app)
            else:
                _, comando_db, tipo = resultado
                if tipo == "exe":
                    comando_final = f'start "" "{comando_db.replace("%USERNAME%", usuario)}"'
                elif tipo == "uwp":
                    comando_final = comando_db
                else:
                    comando_final = comando_db
                hud.log(f"Ejecutando [ {nombre_app} ]...")
                if ejecutar_comando_cmd(comando_final):
                    hud.log(f"Listo, {nombre_app} fue abierto.")
                    actualizar_ultima_vez(nombre_app)
                    texto_acumulado = ""
                    threading.Timer(2, hud.ocultar).start()
                    return
                hud.log(f"No se pudo abrir [ {nombre_app} ]")
                texto_acumulado = ""
                threading.Timer(2, hud.ocultar).start()
                return
        elif accion == "cerrar":
            if ejecutar_accion_desde_catalogo(nombre_app, "cerrar"):
                hud.log(f"Listo, {nombre_app} fue cerrado.")
            else:
                hud.log(f"No se pudo cerrar [ {nombre_app} ]")
            texto_acumulado = ""
            threading.Timer(2, hud.ocultar).start()
            return

    sugerencia = generar_comandos_con_cohere(mensaje)
    if sugerencia:
        descripcion = sugerencia.get("descripcion")
        if descripcion:
            hud.log(descripcion)
        comandos_generados = ";".join(sugerencia["comandos"])
        if ejecutar_comandos_en_cadena(comandos_generados):
            texto_acumulado = ""
            threading.Timer(2, hud.ocultar).start()
            return
        logger.warning("Los comandos sugeridos por Cohere fallaron: %s", comandos_generados)

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
