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
LOG_PATH = BASE_DIR / "setup.log"

RUTAS_EXE = [
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    os.path.expandvars(r"%LocalAppData%\Programs"),
    os.path.expandvars(r"%AppData%\Microsoft\Windows\Start Menu\Programs"),
    os.path.expandvars(r"%LocalAppData%"),
]

IGNORED_EXE_PATTERNS = (
    "setup",
    "instal",
    "unins",
    "updater",
    "update",
    "patch",
    "driver",
    "vc_redist",
    "helper",
    "support",
    "repair",
    "maint",
    "service pack",
)

IGNORED_PATH_SEGMENTS = {"temp", "installer", "$patchcache$"}

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


def es_ejecutable_valido(ruta_completa: str) -> bool:
    ruta = Path(ruta_completa)
    nombre = ruta.name.lower()
    if not nombre.endswith(".exe"):
        return False
    if any(pat in nombre for pat in IGNORED_EXE_PATTERNS):
        return False
    if any(seg.lower() in IGNORED_PATH_SEGMENTS for seg in ruta.parts):
        return False
    return True


def escanear_apps_exe() -> List[Dict[str, str]]:
    resultados: List[Dict[str, str]] = []
    for ruta_base in RUTAS_EXE:
        if not ruta_base or not os.path.exists(ruta_base):
            continue
        for root, _, files in os.walk(ruta_base):
            for file in files:
                ruta_completa = os.path.join(root, file)
                if not es_ejecutable_valido(ruta_completa):
                    continue
                nombre_app = os.path.splitext(file)[0]
                resultados.append(
                    {"nombre": nombre_app, "ruta": ruta_completa, "proceso": file}
                )
    return resultados


def escanear_paquetes_store() -> Dict[str, Dict[str, Optional[str]]]:
    comando_powershell = r"""
    $ErrorActionPreference = "SilentlyContinue"
    Get-AppxPackage | Select-Object Name, PackageFamilyName, InstallLocation, SignatureKind | ConvertTo-Json -Depth 3 -Compress
    """
    try:
        salida = subprocess.check_output(
            ["powershell", "-Command", comando_powershell],
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        escribir_log(f"Fallo consultando paquetes Store: {exc}")
        return {}

    try:
        datos = json.loads(salida.decode("utf-8", errors="ignore") or "[]")
    except json.JSONDecodeError as exc:
        escribir_log(f"Respuesta de paquetes Store inv�lida: {exc}")
        return {}

    if isinstance(datos, dict):
        datos = [datos]

    paquetes: Dict[str, Dict[str, Optional[str]]] = {}
    for pkg in datos or []:
        familia = str(pkg.get("PackageFamilyName") or "").strip()
        if not familia:
            continue
        paquetes[familia] = {
            "nombre": str(pkg.get("Name") or "").strip() or familia,
            "instalacion": str(pkg.get("InstallLocation") or "").strip() or None,
            "origen": str(pkg.get("SignatureKind") or "").strip() or None,
        }
    return paquetes


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

    paquetes_store = escanear_paquetes_store()

    resultado: List[Dict[str, str]] = []
    for app in datos or []:
        nombre = str(app.get("Name") or "").strip()
        appid = str(app.get("AppUserModelID") or "").strip()
        if not nombre or not appid:
            continue
        familia = appid.split("!")[0] if "!" in appid else ""
        info_store = paquetes_store.get(familia, {})
        resultado.append(
            {
                "nombre": nombre,
                "comando": f'explorer.exe shell:appsFolder\\{appid}',
                "familia": familia,
                "instalacion": info_store.get("instalacion"),
                "origen": info_store.get("origen"),
            }
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
        paths_uwp: List[str] = []
        if isinstance(uwp.get("instalacion"), str) and uwp["instalacion"]:
            paths_uwp.append(uwp["instalacion"])  # type: ignore[arg-type]
        aliases_extra: List[str] = [uwp["nombre"]]
        if isinstance(uwp.get("familia"), str) and uwp["familia"]:
            aliases_extra.append(uwp["familia"])  # type: ignore[arg-type]
        app_uwp = registrar_app(
            catalogo,
            uwp["nombre"],
            "uwp",
            uwp["comando"],
            launch=uwp["comando"],
            aliases=aliases_extra,
            window_hints=[uwp["nombre"]],
            paths=paths_uwp,
        )
        if isinstance(uwp.get("origen"), str) and uwp["origen"]:
            app_uwp["store_signature"] = uwp["origen"]  # type: ignore[index]

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

    paquetes_store = escanear_paquetes_store()
    for familia, datos in paquetes_store.items():
        ruta_instalacion = datos.get("instalacion") or ""
        clave = ruta_instalacion or f"store://{familia}"
        info: Dict[str, object] = {
            "nombre": datos.get("nombre") or familia,
            "ruta": clave,
            "extension": "",
            "tamano": 0,
            "ultima_modificacion": datetime.now().isoformat(),
            "tipo_mime": "appx-store",
            "familia": familia,
            "origen": datos.get("origen"),
        }
        inventario[clave] = info
    return list(inventario.values())


def guardar_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    LOG_PATH.write_text("", encoding="utf-8")
    print("Generando catálogo de aplicaciones...")
    catalogo = generar_catalogo()
    guardar_json(APPS_JSON_PATH, catalogo)
    print(f"Catálogo guardado en {APPS_JSON_PATH}")

    escribir_log("Setup completado con JSON.")


if __name__ == "__main__":
    main()
