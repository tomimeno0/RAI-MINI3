"""Herramientas para controlar brillo, volumen y micrófono en Windows.

El módulo intenta utilizar NirCmd cuando está disponible porque ofrece comandos
directos. Si no se encuentra NirCmd, se recurre a APIs nativas mediante COM para
audio y a WMI/PowerShell para brillo. Todas las operaciones exponen funciones
de alto nivel que validan y normalizan porcentajes y aseguran accesos thread-safe.
"""

from __future__ import annotations

import ctypes
import logging
import shutil
import subprocess
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

_lock = threading.Lock()

_CLSCTX_INPROC_SERVER = 0x1
_COINIT_APARTMENTTHREADED = 0x2


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, value: str) -> "_GUID":
        u = UUID(value)
        data4 = (ctypes.c_ubyte * 8)(*u.bytes[8:])
        return cls(u.time_low, u.time_mid, u.time_hi_version, data4)


class _IUnknown(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(ctypes.c_void_p))]


_CLSID_MMDeviceEnumerator = _GUID.from_string("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_IID_IMMDeviceEnumerator = _GUID.from_string("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
_IID_IAudioEndpointVolume = _GUID.from_string("{5CDF2C82-841E-4546-9722-0CF74078229A}")

_EDATAFLOW_RENDER = 0
_EDATAFLOW_CAPTURE = 1
_ERole_console = 0

_ole32 = ctypes.OleDLL("ole32")
_ole32.CoInitializeEx.restype = ctypes.HRESULT
_ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
_ole32.CoUninitialize.argtypes = ()
_ole32.CoCreateInstance.restype = ctypes.HRESULT
_ole32.CoCreateInstance.argtypes = (
    ctypes.POINTER(_GUID),
    ctypes.c_void_p,
    ctypes.c_ulong,
    ctypes.POINTER(_GUID),
    ctypes.POINTER(ctypes.c_void_p),
)


def _succeeded(hr: int) -> bool:
    return hr >= 0


def _com_call(
    obj: ctypes.POINTER(_IUnknown),
    index: int,
    restype: type,
    argtypes: list[type],
    *args,
):
    vtable = ctypes.cast(obj.contents.lpVtbl, ctypes.POINTER(ctypes.c_void_p))
    func_ptr = vtable[index]
    func_type = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    func = func_type(func_ptr)
    return func(obj, *args)


def _release(obj: Optional[ctypes.POINTER(_IUnknown)]) -> None:
    if obj:
        try:
            _com_call(obj, 2, ctypes.c_ulong, [])
        except Exception:
            pass


def _with_audio_endpoint(
    data_flow: int,
    callback: Callable[[ctypes.POINTER(_IUnknown)], float | None],
) -> float | None:
    hr = _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    need_uninit = hr in (0, 1)
    if hr not in (0, 1):
        if hr == -2147417850:  # RPC_E_CHANGED_MODE
            need_uninit = False
        else:
            raise RuntimeError(f"CoInitializeEx falló con HRESULT {hr:#x}")
    enumerator_raw = ctypes.c_void_p()
    hr = _ole32.CoCreateInstance(
        ctypes.byref(_CLSID_MMDeviceEnumerator),
        None,
        _CLSCTX_INPROC_SERVER,
        ctypes.byref(_IID_IMMDeviceEnumerator),
        ctypes.byref(enumerator_raw),
    )
    if not _succeeded(hr):
        if need_uninit:
            _ole32.CoUninitialize()
        raise RuntimeError(f"No pude crear IMMDeviceEnumerator (HRESULT {hr:#x})")
    enumerator = ctypes.cast(enumerator_raw, ctypes.POINTER(_IUnknown))
    device_raw = ctypes.c_void_p()
    try:
        hr = _com_call(
            enumerator,
            4,
            ctypes.HRESULT,
            [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
            data_flow,
            _ERole_console,
            ctypes.byref(device_raw),
        )
        if not _succeeded(hr):
            raise RuntimeError(f"No pude obtener el dispositivo de audio (HRESULT {hr:#x})")
        device = ctypes.cast(device_raw, ctypes.POINTER(_IUnknown))
        endpoint_raw = ctypes.c_void_p()
        try:
            hr = _com_call(
                device,
                3,
                ctypes.HRESULT,
                [
                    ctypes.POINTER(_GUID),
                    ctypes.c_ulong,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p),
                ],
                ctypes.byref(_IID_IAudioEndpointVolume),
                _CLSCTX_INPROC_SERVER,
                None,
                ctypes.byref(endpoint_raw),
            )
            if not _succeeded(hr):
                raise RuntimeError(f"No pude activar IAudioEndpointVolume (HRESULT {hr:#x})")
            endpoint = ctypes.cast(endpoint_raw, ctypes.POINTER(_IUnknown))
            try:
                return callback(endpoint)
            finally:
                _release(endpoint)
        finally:
            _release(device)
    finally:
        _release(enumerator)
        if need_uninit:
            _ole32.CoUninitialize()
    return None


def _get_volume_scalar(data_flow: int) -> float:
    valor = ctypes.c_float()

    def _getter(endpoint: ctypes.POINTER(_IUnknown)) -> float:
        hr = _com_call(
            endpoint,
            9,
            ctypes.HRESULT,
            [ctypes.POINTER(ctypes.c_float)],
            ctypes.byref(valor),
        )
        if not _succeeded(hr):
            raise RuntimeError(f"No pude leer el volumen (HRESULT {hr:#x})")
        return float(valor.value)

    resultado = _with_audio_endpoint(data_flow, _getter)
    if resultado is None:
        raise RuntimeError("No se obtuvo volumen.")
    return resultado


def _set_volume_scalar(data_flow: int, value: float) -> None:
    value = max(0.0, min(1.0, value))

    def _setter(endpoint: ctypes.POINTER(_IUnknown)) -> float:
        hr = _com_call(
            endpoint,
            7,
            ctypes.HRESULT,
            [ctypes.c_float, ctypes.c_void_p],
            ctypes.c_float(value),
            None,
        )
        if not _succeeded(hr):
            raise RuntimeError(f"No pude asignar el volumen (HRESULT {hr:#x})")
        return value

    _with_audio_endpoint(data_flow, _setter)


def _set_mute(data_flow: int, mute: bool) -> None:

    def _setter(endpoint: ctypes.POINTER(_IUnknown)) -> float:
        hr = _com_call(
            endpoint,
            14,
            ctypes.HRESULT,
            [wintypes.BOOL, ctypes.c_void_p],
            wintypes.BOOL(1 if mute else 0),
            None,
        )
        if not _succeeded(hr):
            raise RuntimeError(f"No pude cambiar el mute (HRESULT {hr:#x})")
        return 0.0

    _with_audio_endpoint(data_flow, _setter)


def _run_subprocess(cmd: list[str]) -> None:
    resultado = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if resultado.returncode != 0:
        raise RuntimeError(resultado.stderr.strip() or "El comando externo falló.")


def _find_nircmd() -> Optional[str]:
    path = shutil.which("nircmd.exe")
    if path:
        return path
    local = Path(__file__).with_name("nircmd.exe")
    if local.exists():
        return str(local)
    parent = Path(__file__).resolve().parent
    sibling = parent.parent / "nircmd.exe"
    if sibling.exists():
        return str(sibling)
    return None


def _normalize_percent(percent: float | int) -> int:
    try:
        val = float(percent)
    except (TypeError, ValueError):
        raise ValueError("El porcentaje debe ser un número.")
    if not val == val:  # NaN check
        raise ValueError("El porcentaje no puede ser NaN.")
    return max(0, min(100, int(round(val))))


def _normalize_delta(delta: float | int, default: int = 5) -> int:
    if delta is None:
        return default
    try:
        val = float(delta)
    except (TypeError, ValueError):
        raise ValueError("El ajuste debe ser numérico.")
    if not val == val:
        raise ValueError("El ajuste no puede ser NaN.")
    return max(-100, min(100, int(round(val))))


def _powershell_brightness(percent: int) -> None:
    comando = (
        "Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods "
        f"| ForEach-Object {{ $_.WmiSetBrightness(1,{percent}) }} | Out-Null"
    )
    _run_subprocess(["powershell.exe", "-NoProfile", "-Command", comando])


def get_brightness() -> Optional[int]:
    comando = (
        "Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
        "| Select-Object -First 1 -ExpandProperty CurrentBrightness"
    )
    resultado = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", comando],
        capture_output=True,
        text=True,
        check=False,
    )
    if resultado.returncode != 0:
        logger.debug("No pude leer el brillo: %s", resultado.stderr.strip())
        return None
    texto = resultado.stdout.strip()
    try:
        return int(texto)
    except ValueError:
        logger.debug("Valor de brillo inesperado: %s", texto)
        return None


def set_brightness(percent: float | int) -> int:
    """Ajusta el brillo al porcentaje solicitado. Retorna el valor aplicado."""
    valor = _normalize_percent(percent)
    with _lock:
        nircmd = _find_nircmd()
        try:
            if nircmd:
                _run_subprocess([nircmd, "setbrightness", str(valor)])
            else:
                _powershell_brightness(valor)
        except Exception as exc:
            logger.error("Fallo al cambiar brillo: %s", exc)
            raise
    return valor


def _run_nircmd_volume(command: str, value: int) -> None:
    nircmd = _find_nircmd()
    if not nircmd:
        raise RuntimeError("NirCmd no está disponible.")
    _run_subprocess([nircmd, command, str(value)])


def _volume_percent_to_step(percent: int) -> int:
    return int(percent / 100 * 65535)


def set_volume(percent: float | int) -> int:
    """Define el volumen maestro (altavoces) al porcentaje dado."""
    valor = _normalize_percent(percent)
    with _lock:
        nircmd = _find_nircmd()
        try:
            if nircmd:
                _run_nircmd_volume("setsysvolume", _volume_percent_to_step(valor))
            else:
                _set_volume_scalar(_EDATAFLOW_RENDER, valor / 100.0)
        except Exception as exc:
            logger.error("Fallo al ajustar volumen: %s", exc)
            raise
    return valor


def _adjust_volume(delta: int) -> int:
    current = _get_volume_scalar(_EDATAFLOW_RENDER)
    nuevo = max(0.0, min(1.0, current + (delta / 100.0)))
    _set_volume_scalar(_EDATAFLOW_RENDER, nuevo)
    return int(round(nuevo * 100))


def get_volume() -> int:
    return int(round(_get_volume_scalar(_EDATAFLOW_RENDER) * 100))


def volume_up(delta: float | int) -> int:
    """Sube el volumen en delta %. Devuelve el valor final estimado."""
    ajuste = _normalize_delta(delta, default=5)
    if ajuste <= 0:
        raise ValueError("El incremento debe ser positivo.")
    with _lock:
        nircmd = _find_nircmd()
        try:
            if nircmd:
                step = _volume_percent_to_step(ajuste)
                _run_nircmd_volume("changesysvolume", step)
                actual = int(round(_get_volume_scalar(_EDATAFLOW_RENDER) * 100))
                return actual
            return _adjust_volume(ajuste)
        except Exception as exc:
            logger.error("Fallo al subir volumen: %s", exc)
            raise


def volume_down(delta: float | int) -> int:
    """Baja el volumen en delta %. Devuelve el valor final estimado."""
    ajuste = _normalize_delta(delta, default=5)
    if ajuste <= 0:
        raise ValueError("El decremento debe ser positivo.")
    with _lock:
        nircmd = _find_nircmd()
        try:
            if nircmd:
                step = -_volume_percent_to_step(ajuste)
                _run_nircmd_volume("changesysvolume", step)
                actual = int(round(_get_volume_scalar(_EDATAFLOW_RENDER) * 100))
                return actual
            return _adjust_volume(-ajuste)
        except Exception as exc:
            logger.error("Fallo al bajar volumen: %s", exc)
            raise


def volume_mute() -> None:
    with _lock:
        nircmd = _find_nircmd()
        try:
            if nircmd:
                _run_subprocess([nircmd, "mutesysvolume", "1"])
            else:
                _set_mute(_EDATAFLOW_RENDER, True)
        except Exception as exc:
            logger.error("Fallo al mutear el volumen: %s", exc)
            raise


def volume_unmute() -> None:
    with _lock:
        nircmd = _find_nircmd()
        try:
            if nircmd:
                _run_subprocess([nircmd, "mutesysvolume", "0"])
            else:
                _set_mute(_EDATAFLOW_RENDER, False)
        except Exception as exc:
            logger.error("Fallo al desmutear el volumen: %s", exc)
            raise


def mic_mute() -> None:
    """Silencia el micrófono predeterminado."""
    with _lock:
        nircmd = _find_nircmd()
        try:
            if nircmd:
                _run_subprocess([nircmd, "mutesysvolume", "1", "microphone"])
            else:
                _set_mute(_EDATAFLOW_CAPTURE, True)
        except Exception as exc:
            logger.error("Fallo al mutear micrófono: %s", exc)
            raise


def mic_unmute() -> None:
    """Activa el micrófono predeterminado."""
    with _lock:
        nircmd = _find_nircmd()
        try:
            if nircmd:
                _run_subprocess([nircmd, "mutesysvolume", "0", "microphone"])
            else:
                _set_mute(_EDATAFLOW_CAPTURE, False)
        except Exception as exc:
            logger.error("Fallo al desmutear micrófono: %s", exc)
            raise


__all__ = [
    "set_brightness",
    "set_volume",
    "volume_up",
    "volume_down",
    "volume_mute",
    "volume_unmute",
    "mic_mute",
    "mic_unmute",
    "get_brightness",
    "get_volume",
    "_normalize_percent",
    "_normalize_delta",
    "normalize_percent",
]


def normalize_percent(percent: float | int) -> int:
    """API pública para las pruebas sin exponer detalles internos."""
    return _normalize_percent(percent)
