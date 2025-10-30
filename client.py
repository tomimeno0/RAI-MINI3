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
            descripcion_atajo = atajo_interpretado.get("descripcion") or "Atajo ejecutado."
            registrar_accion({
                "tipo": "atajo",
                "combos": atajo_interpretado.get("combos", []),
                "descripcion": descripcion_atajo,
            })
            texto_acumulado = ""
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
                registrar_accion({"tipo": "atajo", "combos": atajo.get("combos", []), "descripcion": atajo.get("descripcion")})
                texto_acumulado = ""
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
    main()
