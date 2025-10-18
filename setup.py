# -*- coding: utf-8 -*-
"""Genera el catálogo de aplicaciones y archivos usando JSON en lugar de SQLite."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

BASE_DIR = Path(__file__).parent
APPS_JSON_PATH = BASE_DIR / "apps.json"
ARCHIVOS_JSON_PATH = BASE_DIR / "archivos.json"
LOG_PATH = BASE_DIR / "setup.log"

RUTAS_EXE = [
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    os.path.expandvars(r"%LocalAppData%\Programs"),
    os.path.expandvars(r"%AppData%\Microsoft\Windows\Start Menu\Programs"),
    os.path.expandvars(r"%LocalAppData%"),
]

COMANDOS_GENERALES = [
    {"nombre": "Administrador de tareas", "abrir": "taskmgr", "cerrar": "taskkill /IM taskmgr.exe /F"},
    {"nombre": "Símbolo del sistema", "abrir": "cmd", "cerrar": "taskkill /IM cmd.exe /F"},
    {"nombre": "Calculadora", "abrir": "calc", "cerrar": "taskkill /IM Calculator.exe /F"},
    {"nombre": "Bloc de notas", "abrir": "notepad", "cerrar": "taskkill /IM notepad.exe /F"},
    {"nombre": "Explorador de archivos", "abrir": "explorer", "cerrar": "taskkill /IM explorer.exe /F"},
    {"nombre": "Centro de movilidad", "abrir": "mblctr", "cerrar": None},
    {"nombre": "Configuración", "abrir": "start ms-settings:", "cerrar": None},
    {"nombre": "Panel de control", "abrir": "control", "cerrar": None},
    {"nombre": "Desinstalar programas", "abrir": "appwiz.cpl", "cerrar": None},
    {"nombre": "Ejecutar", "abrir": r"explorer shell:AppsFolder\Microsoft.Windows.Run_8wekyb3d8bbwe!App", "cerrar": None},
    {"nombre": "Windows Update", "abrir": "control update", "cerrar": None},
    {"nombre": "Servicios", "abrir": "services.msc", "cerrar": None},
    {"nombre": "Visor de eventos", "abrir": "eventvwr", "cerrar": None},
    {"nombre": "Monitor de recursos", "abrir": "resmon", "cerrar": None},
    {"nombre": "Administrador de dispositivos", "abrir": "devmgmt.msc", "cerrar": None},
    {"nombre": "Windows Defender", "abrir": "start windowsdefender:", "cerrar": None},
    {"nombre": "Centro de seguridad de Windows Defender", "abrir": "windowsdefender:", "cerrar": None},
    {"nombre": "Windows PowerShell", "abrir": "powershell", "cerrar": "taskkill /IM powershell.exe /F"},
    {"nombre": "Bloc de notas++", "abrir": "notepad++", "cerrar": "taskkill /IM notepad++.exe /F"},
    {"nombre": "Reproductor de Windows Media", "abrir": "wmplayer", "cerrar": "taskkill /IM wmplayer.exe /F"},
    {"nombre": "Microsoft Edge", "abrir": "start msedge", "cerrar": None},
    {"nombre": "Google Chrome", "abrir": "start chrome", "cerrar": None},
    {"nombre": "Mozilla Firefox", "abrir": "start firefox", "cerrar": None},
    {"nombre": "Microsoft Word", "abrir": "start winword", "cerrar": None},
    {"nombre": "Microsoft Excel", "abrir": "start excel", "cerrar": None},
    {"nombre": "Microsoft PowerPoint", "abrir": "start powerpnt", "cerrar": None},
    {"nombre": "Spotify", "abrir": "start spotify", "cerrar": "taskkill /IM spotify.exe /F"},
    {"nombre": "Skype", "abrir": "start skype", "cerrar": "taskkill /IM skype.exe /F"},
    {"nombre": "Discord", "abrir": "start discord", "cerrar": "taskkill /IM discord.exe /F"},
    {"nombre": "Zoom", "abrir": "start zoom", "cerrar": "taskkill /IM zoom.exe /F"},
    {"nombre": "Teams", "abrir": "start teams", "cerrar": "taskkill /IM Teams.exe /F"},
]

ACCIONES_EXTRA = [
    {"nombre": "Discord", "accion": "abrir", "comando": "start discord"},
    {"nombre": "Discord", "accion": "cerrar", "comando": "taskkill /IM discord.exe /F"},
    {"nombre": "Discord", "accion": "reiniciar", "comando": "taskkill /IM discord.exe /F && start discord"},
    {"nombre": "Spotify", "accion": "abrir", "comando": "start spotify"},
    {"nombre": "Spotify", "accion": "cerrar", "comando": "taskkill /IM spotify.exe /F"},
    {"nombre": "Spotify", "accion": "reiniciar", "comando": "taskkill /IM spotify.exe /F && start spotify"},
    {"nombre": "Zoom", "accion": "abrir", "comando": "start zoom"},
    {"nombre": "Zoom", "accion": "cerrar", "comando": "taskkill /IM zoom.exe /F"},
    {"nombre": "Zoom", "accion": "desinstalar", "comando": 'powershell "Get-AppxPackage *zoom* | Remove-AppxPackage"'},
    {"nombre": "Calculadora", "accion": "cerrar", "comando": "taskkill /IM Calculator.exe /F"},
    {"nombre": "Calculadora", "accion": "reiniciar", "comando": "taskkill /IM Calculator.exe /F && start calc"},
    {"nombre": "WhatsApp", "accion": "abrir", "comando": "start whatsapp"},
    {"nombre": "WhatsApp", "accion": "cerrar", "comando": "taskkill /IM whatsapp.exe /F"},
    {"nombre": "WhatsApp", "accion": "reiniciar", "comando": "taskkill /IM whatsapp.exe /F && start whatsapp"},
    {"nombre": "Google Chrome", "accion": "cerrar", "comando": "taskkill /IM chrome.exe /F"},
    {"nombre": "Google Chrome", "accion": "reiniciar", "comando": "taskkill /IM chrome.exe /F && start chrome"},
    {"nombre": "Bloc de notas", "accion": "cerrar", "comando": "taskkill /IM notepad.exe /F"},
    {"nombre": "Bloc de notas", "accion": "reiniciar", "comando": "taskkill /IM notepad.exe /F && start notepad"},
    {"nombre": "Visual Studio Code", "accion": "cerrar", "comando": "taskkill /IM Code.exe /F"},
    {"nombre": "Visual Studio Code", "accion": "reiniciar", "comando": "taskkill /IM Code.exe /F && start code"},
    {"nombre": "Teams", "accion": "cerrar", "comando": "taskkill /IM Teams.exe /F"},
    {"nombre": "Teams", "accion": "reiniciar", "comando": "taskkill /IM Teams.exe /F && start teams"},
    {"nombre": "Skype", "accion": "cerrar", "comando": "taskkill /IM skype.exe /F"},
    {"nombre": "Skype", "accion": "reiniciar", "comando": "taskkill /IM skype.exe /F && start skype"},
]

CARPETAS_POR_DEFECTO = [
    os.path.expandvars(r"%UserProfile%\Desktop"),
    os.path.expandvars(r"%UserProfile%\Documents"),
    os.path.expandvars(r"%UserProfile%\Downloads"),
]


def escribir_log(mensaje: str) -> None:
    marca = datetime.now().isoformat(timespec="seconds")
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"[{marca}] {mensaje}\n")


def unique_strings(values: Iterable[Optional[str]]) -> List[str]:
    vistos = set()
    resultado: List[str] = []
    for valor in values:
        if not valor:
            continue
        limpio = str(valor).strip()
        if not limpio:
            continue
        clave = limpio.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        resultado.append(limpio)
    return resultado


def slugify(texto: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", texto.lower()).strip("_")
    return slug or "app"


def reservar_id(nombre: str, tipo: str, existentes: Dict[str, Dict[str, object]]) -> str:
    base = slugify(f"{nombre}_{tipo}")
    candidato = base
    contador = 2
    while candidato in existentes:
        candidato = f"{base}_{contador}"
        contador += 1
    return candidato


def escanear_apps_exe() -> List[Dict[str, str]]:
    resultados: List[Dict[str, str]] = []
    for ruta_base in RUTAS_EXE:
        if not ruta_base or not os.path.exists(ruta_base):
            continue
        for root, _, files in os.walk(ruta_base):
            for file in files:
                if not file.lower().endswith(".exe"):
                    continue
                ruta_completa = os.path.join(root, file)
                nombre_app = os.path.splitext(file)[0]
                resultados.append(
                    {"nombre": nombre_app, "ruta": ruta_completa, "proceso": file}
                )
    return resultados


def escanear_apps_uwp() -> List[Dict[str, str]]:
    comando_powershell = r"""
    $ErrorActionPreference = "SilentlyContinue"
    $apps = Get-StartApps
    $resultado = @()
    foreach ($app in $apps) {
        $shell = New-Object -ComObject Shell.Application
        $folder = $shell.Namespace("shell:AppsFolder")
        $items = $folder.Items() | Where-Object { $_.Name -eq $app.Name }
        foreach ($item in $items) {
            if ($item.Path) {
                $resultado += [PSCustomObject]@{
                    Name = $app.Name
                    AppUserModelID = $item.Path
                }
            }
        }
    }
    $resultado | ConvertTo-Json -Depth 3 -Compress
    """
    try:
        salida = subprocess.check_output(
            ["powershell", "-Command", comando_powershell],
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        escribir_log(f"Fallo listando apps UWP: {exc}")
        return []

    try:
        datos = json.loads(salida.decode("utf-8", errors="ignore") or "[]")
    except json.JSONDecodeError as exc:
        escribir_log(f"Respuesta UWP inválida: {exc}")
        return []

    if isinstance(datos, dict):
        datos = [datos]

    resultado: List[Dict[str, str]] = []
    for app in datos or []:
        nombre = str(app.get("Name") or "").strip()
        appid = str(app.get("AppUserModelID") or "").strip()
        if not nombre or not appid:
            continue
        resultado.append(
            {"nombre": nombre, "comando": f'explorer.exe shell:appsFolder\\{appid}'}
        )
    return resultado


def registrar_app(
    catalogo: Dict[str, Dict[str, object]],
    nombre: str,
    tipo: str,
    comando_abrir: str,
    *,
    launch: Optional[str] = None,
    paths: Optional[Iterable[str]] = None,
    aliases: Optional[Iterable[str]] = None,
    window_hints: Optional[Iterable[str]] = None,
) -> Dict[str, object]:
    app_id = reservar_id(nombre, tipo, catalogo)
    base_aliases = list(aliases) if aliases else []
    base_aliases.extend([nombre, app_id])

    app_datos: Dict[str, object] = {
        "id": app_id,
        "nombre": nombre,
        "tipo": tipo,
        "type": tipo,
        "launch": launch or comando_abrir,
        "comando": comando_abrir,
        "paths": list(paths) if paths else [],
        "window_hints": unique_strings(window_hints or [nombre]),
        "aliases": unique_strings(base_aliases),
        "acciones": {"abrir": comando_abrir},
        "ultima_vez": None,
    }
    catalogo[app_id] = app_datos
    return app_datos


def buscar_por_nombre(catalogo: Dict[str, Dict[str, object]], nombre: str) -> Optional[Dict[str, object]]:
    objetivo = nombre.lower()
    for app in catalogo.values():
        principal = str(app.get("nombre", "")).lower()
        if objetivo == principal:
            return app
        for alias in app.get("aliases", []):
            if objetivo == str(alias).lower():
                return app
    return None


def generar_catalogo() -> Dict[str, object]:
    catalogo: Dict[str, Dict[str, object]] = {}

    apps_exe = escanear_apps_exe()
    apps_uwp = escanear_apps_uwp()

    for exe in apps_exe:
        ruta = exe["ruta"]
        proceso = exe["proceso"]
        comando_abrir = f'start "" "{ruta}"'
        ventana_hint = Path(ruta).stem
        app = registrar_app(
            catalogo,
            exe["nombre"],
            "exe",
            comando_abrir,
            launch=ruta,
            paths=[ruta],
            aliases=[exe["nombre"], ventana_hint, proceso],
            window_hints=[exe["nombre"], ventana_hint],
        )
        acciones = app["acciones"]  # type: ignore[index]
        acciones["cerrar"] = f"taskkill /IM {proceso} /F"
        acciones["cerrar_suavemente"] = f"taskkill /IM {proceso}"
        acciones["reiniciar"] = f'taskkill /IM {proceso} /F && start "" "{ruta}"'
        acciones["terminar_proceso"] = f"taskkill /IM {proceso} /T /F"

    for uwp in apps_uwp:
        registrar_app(
            catalogo,
            uwp["nombre"],
            "uwp",
            uwp["comando"],
            launch=uwp["comando"],
            aliases=[uwp["nombre"]],
            window_hints=[uwp["nombre"]],
        )

    for comando in COMANDOS_GENERALES:
        app = registrar_app(
            catalogo,
            comando["nombre"],
            "cmd",
            comando["abrir"],
            launch=comando["abrir"],
            aliases=[comando["nombre"]],
            window_hints=[comando["nombre"]],
        )
        if comando.get("cerrar"):
            app["acciones"]["cerrar"] = comando["cerrar"]  # type: ignore[index]

    for extra in ACCIONES_EXTRA:
        app = buscar_por_nombre(catalogo, extra["nombre"])
        if not app and extra["accion"] == "abrir":
            app = registrar_app(
                catalogo,
                extra["nombre"],
                "cmd",
                extra["comando"],
                launch=extra["comando"],
                aliases=[extra["nombre"]],
                window_hints=[extra["nombre"]],
            )
        if not app:
            escribir_log(f"Acción extra sin app asociada: {extra['nombre']}")
            continue
        acciones = app.setdefault("acciones", {})  # type: ignore[assignment]
        if isinstance(acciones, dict):
            acciones[extra["accion"]] = extra["comando"]

    aplicaciones = sorted(catalogo.values(), key=lambda app: str(app.get("nombre", "")).lower())
    return {"aplicaciones": aplicaciones}


def calcular_hash_sha256(ruta: Path, bloque: int = 65536) -> Optional[str]:
    sha256 = hashlib.sha256()
    try:
        with ruta.open("rb") as fh:
            while True:
                data = fh.read(bloque)
                if not data:
                    break
                sha256.update(data)
        return sha256.hexdigest()
    except OSError:
        return None


def listar_archivos(base: Path, calcular_hash: bool = False) -> List[Dict[str, object]]:
    registros: List[Dict[str, object]] = []
    for root, _, files in os.walk(base, topdown=True):
        for nombre in files:
            ruta = Path(root) / nombre
            try:
                stat = ruta.stat()
            except OSError:
                continue
            info = {
                "nombre": nombre,
                "ruta": str(ruta),
                "extension": ruta.suffix.lower(),
                "tamano": stat.st_size,
                "ultima_modificacion": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "tipo_mime": mimetypes.guess_type(str(ruta))[0] or "desconocido",
            }
            registros.append(info)

    if calcular_hash and registros:
        with ThreadPoolExecutor(max_workers=8) as executor:
            hashes = executor.map(lambda item: calcular_hash_sha256(Path(item["ruta"])), registros)
            for registro, hash_value in zip(registros, hashes):
                registro["hash_sha256"] = hash_value
    return registros


def escanear_archivos(calcular_hash: bool = False) -> List[Dict[str, object]]:
    inventario: Dict[str, Dict[str, object]] = {}
    for carpeta in CARPETAS_POR_DEFECTO:
        ruta = Path(carpeta).expanduser()
        if not ruta.exists():
            continue
        print(f"Escaneando archivos en {ruta}...")
        for registro in listar_archivos(ruta, calcular_hash=calcular_hash):
            inventario[registro["ruta"]] = registro
    return list(inventario.values())


def guardar_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    LOG_PATH.write_text("", encoding="utf-8")
    print("Generando catálogo de aplicaciones...")
    catalogo = generar_catalogo()
    guardar_json(APPS_JSON_PATH, catalogo)
    print(f"Catálogo guardado en {APPS_JSON_PATH}")

    print("Compilando inventario de archivos...")
    archivos = escanear_archivos(calcular_hash=False)
    guardar_json(ARCHIVOS_JSON_PATH, archivos)
    print(f"Inventario guardado en {ARCHIVOS_JSON_PATH} ({len(archivos)} archivos)")

    escribir_log("Setup completado con JSON.")


if __name__ == "__main__":
    main()
