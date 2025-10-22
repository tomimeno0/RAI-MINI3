"""Cliente principal de RAI-MINI.

Escucha la hotword, envía las peticiones al servidor local y ejecuta acciones
usando un catálogo JSON. Si falta una acción en el catálogo, se apoya en Cohere
para generar comandos de PowerShell/teclado/ventanas y los persiste.
"""

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
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

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
historial_acciones: Deque[Dict[str, Any]] = deque(maxlen=5)
USER32 = ctypes.windll.user32
KERNEL32 = ctypes.windll.kernel32


def _normalizar(texto: str) -> str:
    return re.sub(r"\s+", " ", texto.strip().lower())


def _sin_acentos(texto: str) -> str:
    """Devuelve el texto en minusculas y sin acentos para comparaciones flexibles."""
    texto_lower = texto.lower()
    descompuesto = unicodedata.normalize("NFD", texto_lower)
    return "".join(ch for ch in descompuesto if not unicodedata.combining(ch))


ATAJOS_VOZ: List[Dict[str, Any]] = [
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
]


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


def registrar_accion(accion: Dict[str, Any]) -> None:
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
    match = re.search(r"\{.*\}", texto, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _extraer_comandos_desde_texto(texto: str) -> List[str]:
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
                user32 = ctypes.windll.user32 if hwnd else None
                if user32 and hwnd:
                    try:
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                        logger.debug("SW_RESTORE aplicado a %s.", hwnd)
                        time.sleep(0.15)
                    except Exception as exc:
                        logger.debug("SW_RESTORE falló: %s", exc)
                try:
                    ventana.restore()
                except Exception:
                    pass
                if user32 and hwnd:
                    try:
                        user32.SetForegroundWindow(hwnd)
                        time.sleep(0.05)
                    except Exception as exc:
                        logger.debug("SetForegroundWindow falló: %s", exc)
                try:
                    ventana.activate()
                except Exception as exc:
                    logger.debug("Actualizar foco falló: %s", exc)

                max_exitoso = False
                try:
                    ventana.maximize()
                    time.sleep(0.05)
                    if not user32 or (user32 and hwnd and user32.IsZoomed(hwnd)):
                        max_exitoso = True
                        logger.debug("Maximizacion via pygetwindow confirmada.")
                except Exception as exc:
                    logger.debug("Maximizar directo falló: %s", exc)

                if not max_exitoso and user32 and hwnd:
                    try:
                        user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
                        time.sleep(0.05)
                        if user32.IsZoomed(hwnd):
                            max_exitoso = True
                            logger.debug("Maximizacion via ShowWindow confirmada.")
                    except Exception as win_exc:
                        logger.debug("ShowWindow SW_MAXIMIZE falló: %s", win_exc)

                if not max_exitoso:
                    try:
                        ventana.activate()
                    except Exception:
                        pass
                    try:
                        time.sleep(0.1)
                        pyautogui.hotkey("win", "up")
                        time.sleep(0.05)
                        if user32 and hwnd and user32.IsZoomed(hwnd):
                            max_exitoso = True
                            logger.debug("Maximizacion via Win+Up confirmada.")
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
    combos_raw = atajo.get("combos") or []
    combos: List[Tuple[str, ...]] = []
    for combo in combos_raw:
        if isinstance(combo, (list, tuple)):
            combos.append(tuple(str(k) for k in combo))
        else:
            combos.append((str(combo),))
    return _ejecutar_combos_teclado(combos)



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
    del timeout  # no se usa sin servidor
    global texto_acumulado
    if not texto_acumulado:
        logger.warning("No hay texto para enviar.")
        return

    mensaje = texto_acumulado.strip()
    if _es_pedido_repeticion(mensaje):
        ok, mensaje_historial = _repetir_ultima_accion()
        if ok:
            hud.log(mensaje_historial)
        else:
            hud.log(mensaje_historial)
        texto_acumulado = ""
        threading.Timer(2, hud.ocultar).start()
        return

    atajo = _detectar_atajo_teclado(mensaje)
    if atajo:
        logger.info("Atajo de teclado detectado: %s", atajo.get('id'))
        if ejecutar_atajo_teclado(atajo):
            registrar_accion({"tipo": "atajo", "combos": [tuple(c) for c in atajo.get("combos", [])], "descripcion": atajo.get("descripcion")})
            hud.log(atajo.get("descripcion") or "Atajo ejecutado.")
        else:
            hud.log("No pude ejecutar el atajo.")
        texto_acumulado = ""
        threading.Timer(2, hud.ocultar).start()
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
            hud.log(mensajes_ok.get(accion, f"Accion sobre ventana completada: {objetivo}"))
        texto_acumulado = ""
        threading.Timer(2, hud.ocultar).start()
        return

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
            registrar_accion({"tipo": "comandos", "comandos": comandos_generados})
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
