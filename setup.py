"""Script de instalación para RAI-MINI."""
import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List

try:
    import getpass  # Disponible en Windows
except ImportError:  # Respaldo improbable
    getpass = None

try:
    import win32com.client  # type: ignore
except ImportError:  # pywin32 es opcional
    win32com = None
else:
    win32com = win32com.client

CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

ROOT = Path(__file__).resolve().parent
APPS_JSON = ROOT / "apps.json"
BACKUP_JSON = ROOT / "apps.json.bak"
LOG_FILE = ROOT / "setup.log"

SCAN_PATHS = [
    Path(r"C:\\Program Files"),
    Path(r"C:\\Program Files (x86)"),
]

USER = getpass.getuser() if getpass else os.environ.get("USERNAME", "")
if USER:
    SCAN_PATHS.extend(
        [
            Path(fr"C:\\Users\\{USER}\\AppData\\Local\\Programs"),
            Path(fr"C:\\Users\\{USER}\\Start Menu\\Programs"),
        ]
    )
SCAN_PATHS.append(Path(r"C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs"))

WINDOWS_DIR = Path(r"C:\\Windows")

warnings: List[str] = []

def registrar_advertencia(msg: str) -> None:
    warnings.append(msg)
    print(f"[ADVERTENCIA] {msg}")


def normalizar_id(nombre: str) -> str:
    texto = unicodedata.normalize("NFKD", nombre)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.lower().replace(" ", "")
    permitido = "abcdefghijklmnopqrstuvwxyz0123456789_-"
    return "".join(c for c in texto if c in permitido)


def generar_aliases(nombre: str) -> List[str]:
    base = nombre.strip()
    if not base:
        return []
    normal = base.lower()
    simple = normal.replace(" ", "")
    aliases = {normal, simple}
    if "," in base:
        for parte in base.split(","):
            alias = parte.strip().lower()
            if alias:
                aliases.add(alias)
    return [a for a in aliases if a]


def resolver_lnk(ruta: Path) -> str:
    if win32com is None:
        return str(ruta)
    try:
        shell = win32com.Dispatch("WScript.Shell")
        acceso = shell.CreateShortCut(str(ruta))
        destino = acceso.Targetpath
        if destino:
            return str(destino)
    except Exception as exc:  # noqa: BLE001
        registrar_advertencia(f"No se pudo resolver acceso directo {ruta}: {exc}")
    return str(ruta)


def escanear_clasicas() -> List[Dict[str, object]]:
    aplicaciones: List[Dict[str, object]] = []
    vistos: Dict[str, Dict[str, object]] = {}
    for base in SCAN_PATHS:
        if not base.exists():
            continue
        try:
            for root_dir, dirnames, filenames in os.walk(base):
                actual = Path(root_dir)
                if WINDOWS_DIR in actual.parents:
                    dirnames[:] = []
                    continue
                for nombre in filenames:
                    ruta = actual / nombre
                    sufijo = ruta.suffix.lower()
                    if sufijo not in {".exe", ".lnk"}:
                        continue
                    nombre_app = ruta.stem
                    app_id = normalizar_id(nombre_app)
                    if not app_id:
                        continue
                    if sufijo == ".lnk":
                        destino = resolver_lnk(ruta)
                        tipo = "exe"
                        lanzamiento = destino
                    else:
                        tipo = "exe"
                        lanzamiento = str(ruta)
                    registro = {
                        "id": app_id,
                        "aliases": generar_aliases(nombre_app),
                        "type": tipo,
                        "launch": lanzamiento,
                        "window_hints": [nombre_app],
                    }
                    if app_id in vistos:
                        anterior = vistos[app_id]
                        if anterior.get("launch") != lanzamiento:
                            registrar_advertencia(
                                f"ID duplicado {app_id}, se conserva la primera ruta."
                            )
                        continue
                    vistos[app_id] = registro
                    aplicaciones.append(registro)
        except PermissionError as exc:
            registrar_advertencia(f"Permiso denegado en {base}: {exc}")
        except Exception as exc:  # noqa: BLE001
            registrar_advertencia(f"Error al escanear {base}: {exc}")
    return aplicaciones


def escanear_uwp() -> List[Dict[str, object]]:
    comando = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-StartApps | ConvertTo-Json -Depth 2",
    ]
    try:
        resultado = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        registrar_advertencia(f"No se pudo ejecutar PowerShell: {exc}")
        return []

    if resultado.returncode != 0:
        registrar_advertencia(
            f"PowerShell devolvió código {resultado.returncode}: {resultado.stderr.strip()}"
        )
        return []

    salida = resultado.stdout.strip()
    if not salida:
        return []

    try:
        datos = json.loads(salida)
    except json.JSONDecodeError as exc:
        registrar_advertencia(f"No se pudo interpretar la salida de PowerShell: {exc}")
        return []

    if isinstance(datos, dict):
        datos = [datos]

    aplicaciones: List[Dict[str, object]] = []
    for app in datos:
        nombre = str(app.get("Name", "")).strip()
        app_id_real = str(app.get("AppId", "")).strip()
        if not nombre or not app_id_real:
            continue
        app_id = normalizar_id(nombre)
        if not app_id:
            continue
        registro = {
            "id": app_id,
            "aliases": generar_aliases(nombre),
            "type": "uwp",
            "launch": f"explorer.exe shell:appsFolder\\{app_id_real}",
            "window_hints": [nombre],
        }
        aplicaciones.append(registro)
    return aplicaciones


def solicitar_confirmacion(mensaje: str) -> bool:
    while True:
        respuesta = input(f"{mensaje} ").strip().lower()
        if respuesta in {"s", "si", "sí"}:
            return True
        if respuesta in {"n", "no"}:
            return False
        print("Por favor, respondé con S o N.")


def guardar_catalogo(apps: List[Dict[str, object]]) -> None:
    if APPS_JSON.exists():
        try:
            if BACKUP_JSON.exists():
                BACKUP_JSON.unlink()
            APPS_JSON.rename(BACKUP_JSON)
            print("Se creó respaldo apps.json.bak")
        except Exception as exc:  # noqa: BLE001
            registrar_advertencia(f"No se pudo crear el backup: {exc}")
    try:
        with APPS_JSON.open("w", encoding="utf-8") as fh:
            json.dump(apps, fh, indent=2, ensure_ascii=False)
        print(f"Se guardó el catálogo en {APPS_JSON.name}.")
    except Exception as exc:  # noqa: BLE001
        registrar_advertencia(f"Error al guardar apps.json: {exc}")


def ejecutar_cliente() -> None:
    try:
        subprocess.Popen(
            [sys.executable, "client.py"],
            cwd=str(ROOT),
            creationflags=CREATE_NEW_CONSOLE,
        )
        print("client.py iniciado en una nueva consola.")
    except Exception as exc:  # noqa: BLE001
        registrar_advertencia(f"No se pudo lanzar RAI: {exc}")


def escribir_log_si_corresponde() -> None:
    if not warnings:
        if LOG_FILE.exists():
            try:
                LOG_FILE.unlink()
            except OSError:
                pass
        return
    try:
        with LOG_FILE.open("w", encoding="utf-8") as fh:
            for advertencia in warnings:
                fh.write(advertencia + "\n")
        print(f"Se registraron advertencias en {LOG_FILE.name}.")
    except Exception as exc:  # noqa: BLE001
        print(f"No se pudo escribir el log: {exc}")


def main() -> None:
    print("Este programa escaneará tus aplicaciones para crear el catálogo de RAI.")
    if not solicitar_confirmacion("¿Querés continuar? (S/N)"):
        print("Operación cancelada por el usuario.")
        return

    print("Escaneando aplicaciones clásicas...")
    clasicas = escanear_clasicas()
    print(f"Se detectaron {len(clasicas)} aplicaciones clásicas.")

    print("Escaneando aplicaciones de la Tienda...")
    uwp = escanear_uwp()
    print(f"Se detectaron {len(uwp)} aplicaciones UWP.")

    catalogo = {app["id"]: app for app in clasicas}
    for app in uwp:
        if app["id"] in catalogo:
            registrar_advertencia(
                f"Aplicación UWP con ID existente {app['id']}, se mantiene la versión clásica."
            )
            continue
        catalogo[app["id"]] = app

    apps_finales = sorted(catalogo.values(), key=lambda x: x["id"])  # type: ignore[arg-type]

    print(f"Se generará un catálogo con {len(apps_finales)} aplicaciones.")
    if not solicitar_confirmacion("¿Querés guardar los cambios? (S/N)"):
        print("Catálogo no guardado por decisión del usuario.")
        escribir_log_si_corresponde()
        return

    guardar_catalogo(apps_finales)
    escribir_log_si_corresponde()

    if solicitar_confirmacion("¿Querés ejecutar RAI ahora? (S/N)"):
        ejecutar_cliente()
    else:
        print("Podés iniciar RAI más tarde ejecutando client.py.")


if __name__ == "__main__":
    main()
