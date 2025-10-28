"""Programación de recordatorios y tareas temporizadas para RAI-MINI."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOGS_DIR = Path("logs")
_TASKS_FILE = _LOGS_DIR / "scheduled_tasks.json"

_tasks_lock = threading.Lock()


@dataclass
class ScheduledTask:
    id: str
    tipo: str
    comando: str
    eta: float
    creado_en: float
    meta: Optional[Dict[str, str]] = None


_tasks: Dict[str, ScheduledTask] = {}
_timers: Dict[str, threading.Timer] = {}
_counters = {
    "recordatorio": 0,
    "shutdown": 0,
    "restart": 0,
}


def _ensure_logs_dir() -> None:
    try:
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("No pude crear la carpeta de logs: %s", exc)


def _persist_tasks() -> None:
    _ensure_logs_dir()
    try:
        data = [asdict(task) for task in _tasks.values()]
        for entry in data:
            if entry.get("meta") is None:
                entry.pop("meta")
        _TASKS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("No pude guardar las tareas: %s", exc)


def _generate_id(kind: str) -> str:
    if kind not in _counters:
        _counters[kind] = 0
    _counters[kind] += 1
    prefix = {
        "recordatorio": "r",
        "shutdown": "a",
        "restart": "re",
    }.get(kind, "t")
    return f"{prefix}{_counters[kind]}"


def _register_task(task: ScheduledTask, timer: Optional[threading.Timer]) -> ScheduledTask:
    with _tasks_lock:
        _tasks[task.id] = task
        if timer:
            _timers[task.id] = timer
        else:
            _timers.pop(task.id, None)
        _persist_tasks()
    return task


def _pop_task(task_id: str) -> Optional[ScheduledTask]:
    with _tasks_lock:
        task = _tasks.pop(task_id, None)
        timer = _timers.pop(task_id, None)
        if timer:
            timer.cancel()
        _persist_tasks()
        return task


def parse_duration(texto: str) -> int:
    """Convierte expresiones como '10 minutos' en segundos."""
    if not texto:
        raise ValueError("No se recibió duración.")
    limpio = texto.strip().lower()
    if not limpio:
        raise ValueError("No se recibió duración.")
    limpio = limpio.replace(",", ".")
    limpio = unicodedata.normalize("NFD", limpio)
    limpio = "".join(ch for ch in limpio if unicodedata.category(ch) != "Mn")

    match = re.search(r"(\d+(?:\.\d+)?)", limpio)
    if not match:
        raise ValueError("No pude entender la cantidad de tiempo.")
    numero = float(match.group(1))
    if numero <= 0:
        raise ValueError("La duración debe ser positiva.")

    unidades = {
        "segundo": 1,
        "segundos": 1,
        "seg": 1,
        "s": 1,
        "minuto": 60,
        "minutos": 60,
        "min": 60,
        "m": 60,
        "hora": 3600,
        "horas": 3600,
        "h": 3600,
    }
    unidad = "minutos"
    for nombre in unidades:
        if re.search(rf"\b{nombre}\b", limpio):
            unidad = nombre
            break
    factor = unidades.get(unidad, 60)
    segundos = int(round(numero * factor))
    return max(segundos, 1)


def program_reminder(
    texto: str,
    delay_s: int,
    on_trigger: Optional[Callable[[ScheduledTask], None]] = None,
) -> ScheduledTask:
    if delay_s <= 0:
        raise ValueError("El recordatorio debe tener un tiempo positivo.")
    task_id = _generate_id("recordatorio")
    eta = time.time() + delay_s
    task = ScheduledTask(
        id=task_id,
        tipo="recordatorio",
        comando=texto,
        eta=eta,
        creado_en=time.time(),
    )

    def _callback() -> None:
        _pop_task(task_id)
        logger.info("Recordatorio (%s) disparado: %s", task_id, texto)
        if on_trigger:
            try:
                on_trigger(task)
            except Exception as exc:
                logger.exception("Error en el callback del recordatorio: %s", exc)

    timer = threading.Timer(delay_s, _callback)
    timer.daemon = True
    timer.start()
    return _register_task(task, timer)


def program_shutdown(delay_s: int, kind: str = "shutdown") -> ScheduledTask:
    if delay_s < 0:
        raise ValueError("El apagado debe tener un tiempo positivo o cero.")
    if kind not in {"shutdown", "restart"}:
        raise ValueError("Tipo de apagado desconocido.")
    flag = "-s" if kind == "shutdown" else "-r"
    comando = ["shutdown", flag, "-t", str(delay_s)]
    resultado = subprocess.run(
        comando,
        capture_output=True,
        text=True,
        check=False,
    )
    if resultado.returncode != 0:
        mensaje = resultado.stderr.strip() or "No pude programar el apagado."
        raise RuntimeError(mensaje)
    task_id = _generate_id(kind)
    task = ScheduledTask(
        id=task_id,
        tipo=kind,
        comando=" ".join(comando),
        eta=time.time() + delay_s,
        creado_en=time.time(),
    )
    logger.info("Programado %s (%s) en %s segundos.", kind, task_id, delay_s)
    return _register_task(task, None)


def cancel_shutdown() -> None:
    resultado = subprocess.run(["shutdown", "-a"], capture_output=True, text=True, check=False)
    if resultado.returncode != 0:
        mensaje = resultado.stderr.strip()
        if "No se puede abortar" in mensaje:
            raise RuntimeError("No hay un apagado en curso.")
        raise RuntimeError(mensaje or "No pude cancelar el apagado.")
    logger.info("Apagado cancelado.")


def cancel_task(task_id: str) -> ScheduledTask:
    if not task_id:
        raise ValueError("Debes indicar un ID de tarea.")
    task = _pop_task(task_id)
    if not task:
        raise KeyError(f"No encontré la tarea {task_id}.")
    if task.tipo in {"shutdown", "restart"}:
        try:
            cancel_shutdown()
        except RuntimeError as exc:
            logger.warning("No pude cancelar el apagado para %s: %s", task_id, exc)
    logger.info("Tarea %s cancelada.", task_id)
    return task


def list_tasks() -> List[ScheduledTask]:
    with _tasks_lock:
        return list(_tasks.values())


def clear_all() -> None:
    """Pensado para pruebas: limpia las tareas sin ejecutar callbacks."""
    with _tasks_lock:
        for timer in _timers.values():
            timer.cancel()
        _tasks.clear()
        _timers.clear()
        _persist_tasks()


__all__ = [
    "ScheduledTask",
    "parse_duration",
    "program_reminder",
    "program_shutdown",
    "cancel_task",
    "cancel_shutdown",
    "list_tasks",
    "clear_all",
]
