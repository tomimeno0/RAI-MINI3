"""Translate natural-language desktop orders into structured actions.

This module prepares the prompt stack for the LLM and exposes :func:`interpret`
which orchestrates intent classification, application resolution and response
formatting. The LLM call is intentionally abstracted behind :func:`call_llm` so
integrators can plug Cohere (u otra opción) without leaking credentials.

Notas para desarrolladores:
- Agregá o ajustá ejemplos few-shot editando ``server/prompts/fewshot.jsonl``;
  cada línea es un JSON con claves ``input`` y ``output``.
- Alias adicionales se suman en ``APP_ALIASES`` debajo.
- Para limitar la búsqueda a un host concreto pasá ``interpret(..., host="PC")``.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

_LOGGER = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
SYSTEM_PROMPT_FILE = PROMPTS_DIR / "system.txt"
FEWSHOT_FILE = PROMPTS_DIR / "fewshot.jsonl"
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "apps.sqlite"

VALID_ACTIONS = {"abrir", "cerrar", "minimizar", "maximizar", "foco"}
FALLBACK_ACTION = "buscar_app"

ACTION_SYNONYMS: Dict[str, str] = {
    "abrime": "abrir",
    "abre": "abrir",
    "abri": "abrir",
    "abrí": "abrir",
    "lanza": "abrir",
    "lanzar": "abrir",
    "ejecuta": "abrir",
    "ejecutar": "abrir",
    "abrelo": "abrir",
    "abrilo": "abrir",
    "abrila": "abrir",
    "arranca": "abrir",
    "arrancá": "abrir",
    "arrancar": "abrir",
    "cerrame": "cerrar",
    "cerrá": "cerrar",
    "cerra": "cerrar",
    "cierra": "cerrar",
    "cierra": "cerrar",
    "mata": "cerrar",
    "matá": "cerrar",
    "matame": "cerrar",
    "termina": "cerrar",
    "terminá": "cerrar",
    "terminar": "cerrar",
    "finalizá": "cerrar",
    "finaliza": "cerrar",
    "finalizar": "cerrar",
    "minimizá": "minimizar",
    "minimiza": "minimizar",
    "minimizar": "minimizar",
    "minimizame": "minimizar",
    "maximizá": "maximizar",
    "maximiza": "maximizar",
    "maximizame": "maximizar",
    "pone": "foco",
    "poné": "foco",
    "ponele": "foco",
    "poneme": "foco",
    "foco": "foco",
    "enfoca": "foco",
    "enfocá": "foco",
    "enfocar": "foco",
}

STOPWORDS = {
    "el",
    "la",
    "los",
    "las",
    "lo",
    "al",
    "del",
    "de",
    "un",
    "una",
    "unos",
    "unas",
    "porfa",
    "porfavor",
    "favor",
    "che",
    "dale",
    "dame",
    "pone",
    "ponele",
    "poneme",
    "ponele",
    "ponele",
    "poné",
    "pone",
    "en",
    "la",
    "el",
    "app",
    "aplicacion",
    "aplicación",
    "programa",
    "ventana",
    "por",
    "fi",
    "porfis",
    "toque",
    "porfa",
    "please",
    "una",
    "que",
    "tengo",
    "necesito",
    "podrias",
    "podrías",
    "podes",
    "podés",
    "podrias",
    "podrías",
    "me",
    "de",
    "y",
    "ya",
    "la",
    "lo",
    "las",
    "los",
    "ahi",
    "ahí",
    "porfa",
    "porfis",
    "dale",
    "un",
    "una",
}

APP_ALIASES: Dict[str, str] = {
    "wpp": "whatsapp",
    "guasap": "whatsapp",
    "wasap": "whatsapp",
    "whats": "whatsapp",
    "whatsapp": "whatsapp",
    "google chrome": "google chrome",
    "chrome": "google chrome",
    "edge": "microsoft edge",
    "microsoft edge": "microsoft edge",
    "discord": "discord",
    "taskmgr": "administrador de tareas",
    "task manager": "administrador de tareas",
    "administrador de tareas": "administrador de tareas",
    "admin de tareas": "administrador de tareas",
    "excel": "microsoft excel",
    "visual studio": "visual studio",
    "spotify": "spotify",
    "calculadora": "calculadora",
    "calc": "calculadora",
    "notas adhesivas": "notas adhesivas",
    "sticky notes": "notas adhesivas",
}


@dataclass
class IntentGuess:
    action: str
    target_name: Optional[str]
    raw_target: Optional[str]
    confidence: float
    notes: List[str] = field(default_factory=list)
    from_llm: bool = False


@dataclass
class AppCandidate:
    name: str
    display_name: str
    normalized_name: str
    source: str
    exe_path: Optional[str]
    uwp_package: Optional[str]
    aumid: Optional[str]
    last_seen: Optional[str]
    host: Optional[str]
    publisher: Optional[str]
    score: float = 0.0


def interpret(
    text: str,
    *,
    host: Optional[str] = None,
    db_path: Optional[Path | str] = None,
    llm: Optional[Callable[[Sequence[Dict[str, str]]], str]] = None,
) -> Dict[str, object]:
    """Interpret a free-form desktop order.

    Parameters
    ----------
    text:
        User utterance.
    host:
        Optional hostname filter to prioritise installs for a specific device.
    db_path:
        Override path for ``apps.sqlite`` (useful in tests).
    llm:
        Optional callable that receives the messages list and must return the
        assistant text (JSON string). When omitted the rule-based heuristics are
        used.
    """

    trace_id = str(uuid.uuid4())
    clean_text = (text or "").strip()
    if not clean_text:
        return _empty_response(
            action=FALLBACK_ACTION,
            confidence=0.1,
            notes="orden vacía",
            trace_id=trace_id,
        )

    normalized = _normalize_text(clean_text)
    intent = _llm_infer(clean_text, normalized, llm_callable=llm)
    if intent is None:
        intent = _rule_based_infer(clean_text, normalized)

    action = _normalise_action(intent.action if intent else None)
    target_hint = (intent.target_name or "") if intent else ""
    raw_target = intent.raw_target if intent else None
    base_confidence = intent.confidence if intent else 0.55
    collected_notes: List[str] = list(intent.notes if intent else [])

    if action == FALLBACK_ACTION:
        notes = _join_notes(collected_notes, "sin acción ejecutable")
        return _empty_response(
            action=FALLBACK_ACTION,
            confidence=min(base_confidence, 0.35),
            notes=notes,
            trace_id=trace_id,
        )

    alias_target = _resolve_alias(target_hint) if target_hint else ""
    alias_target = alias_target or target_hint

    candidate_info = _resolve_candidate(
        alias_target,
        host=host,
        db_path=db_path,
        raw_target=raw_target,
    )

    if candidate_info is None:
        collected_notes.append(
            "sin coincidencias en apps.sqlite; sugerir rescan o crear alias"
        )
        return _empty_response(
            action=FALLBACK_ACTION,
            confidence=min(base_confidence, 0.2),
            notes=_join_notes(collected_notes),
            trace_id=trace_id,
        )

    best, alternates, ambiguous = candidate_info
    candidate_notes = _format_candidate_notes(best, alternates, ambiguous)
    if candidate_notes:
        collected_notes.append(candidate_notes)

    confidence = base_confidence
    if ambiguous:
        confidence = min(confidence, 0.45)
    else:
        confidence = max(confidence, 0.6)
        confidence = min(1.0, confidence + min(best.score, 100) / 300)

    target_payload = {
        "name": best.display_name or best.name,
        "source": best.source,
        "exe_path": best.exe_path,
        "uwp_package": best.uwp_package,
        "aumid": best.aumid,
    }

    return {
        "action": action,
        "target": target_payload,
        "confidence": round(max(0.0, min(confidence, 1.0)), 2),
        "notes": _join_notes(collected_notes),
        "trace_id": trace_id,
    }


def call_llm(
    messages: Sequence[Dict[str, str]],
    *,
    client: Optional[Callable[[Sequence[Dict[str, str]]], str]] = None,
) -> str:
    """Wrapper around the LLM provider.

    ``client`` can be any callable taking the chat messages and returning the
    assistant text. Integrators can pass ``llm`` in :func:`interpret` to use
    their own implementation.
    """

    if client is not None:
        return client(messages)
    raise NotImplementedError("LLM client no configurado")


_SYSTEM_PROMPT: Optional[str] = None
_FEWSHOTS: List[Tuple[str, str]] = []


def _llm_infer(
    text: str,
    normalized: str,
    *,
    llm_callable: Optional[Callable[[Sequence[Dict[str, str]]], str]] = None,
) -> Optional[IntentGuess]:
    try:
        system_prompt = _load_system_prompt()
        fewshots = _load_fewshots()
        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        for user, assistant in fewshots:
            messages.append({"role": "user", "content": user})
            messages.append({"role": "assistant", "content": assistant})
        user_payload = _format_user_prompt(text, normalized)
        messages.append({"role": "user", "content": user_payload})

        raw = call_llm(messages, client=llm_callable)
    except NotImplementedError:
        return None
    except Exception as exc:  # pragma: no cover - logging defensivo
        _LOGGER.warning("Fallo consultando LLM: %%s", exc)
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _LOGGER.warning("Respuesta LLM inválida: %s", raw)
        return None

    if not isinstance(data, dict):
        return None

    action = _normalise_action(data.get("action"))
    target = data.get("target_name")
    if isinstance(target, str):
        target = _normalize_text(target)
    else:
        target = None
    raw_target = data.get("raw_target")
    if isinstance(raw_target, str):
        raw_target = raw_target
    else:
        raw_target = None
    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)):
        confidence = 0.6
    confidence = float(max(0.0, min(confidence, 1.0)))
    notes_value = data.get("notes")
    notes: List[str] = []
    if isinstance(notes_value, str) and notes_value.strip():
        notes.append(notes_value.strip())

    return IntentGuess(
        action=action,
        target_name=target,
        raw_target=raw_target,
        confidence=confidence,
        notes=notes,
        from_llm=True,
    )


def _rule_based_infer(text: str, normalized: str) -> IntentGuess:
    words = normalized.split()
    action = _detect_action(words)
    target_words = [
        w
        for w in words
        if w not in STOPWORDS
        and w not in ACTION_SYNONYMS
        and _normalize_text(w) not in ACTION_SYNONYMS
        and w not in VALID_ACTIONS
    ]
    raw_target = " ".join(target_words).strip() or None
    target = _resolve_alias(raw_target) if raw_target else None
    notes: List[str] = []
    if not action:
        notes.append("no identifiqué verbo; caigo a buscar_app")
        return IntentGuess(
            action=FALLBACK_ACTION,
            target_name=None,
            raw_target=None,
            confidence=0.4,
            notes=notes,
            from_llm=False,
        )

    if not target:
        notes.append("sin nombre de app claro")
        return IntentGuess(
            action=FALLBACK_ACTION,
            target_name=None,
            raw_target=None,
            confidence=0.35,
            notes=notes,
            from_llm=False,
        )

    return IntentGuess(
        action=action,
        target_name=target,
        raw_target=raw_target,
        confidence=0.65,
        notes=notes,
        from_llm=False,
    )


def _detect_action(words: List[str]) -> Optional[str]:
    for word in words:
        if word in VALID_ACTIONS:
            return word
        if word in ACTION_SYNONYMS:
            return ACTION_SYNONYMS[word]
    if "pone" in words or "poné" in words:
        if "foco" in words:
            return "foco"
    if "poner" in words and "foco" in words:
        return "foco"
    return None


def _resolve_candidate(
    target_hint: str,
    *,
    host: Optional[str],
    db_path: Optional[Path | str],
    raw_target: Optional[str],
) -> Optional[Tuple[AppCandidate, List[AppCandidate], bool]]:
    if not target_hint:
        return None

    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    if not path.exists():
        _LOGGER.info("apps.sqlite no encontrado en %s", path)
        return None

    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            candidates = _load_candidates(conn, host)
    except sqlite3.Error as exc:
        _LOGGER.warning("No pude leer apps.sqlite: %%s", exc)
        return None

    if not candidates:
        return None

    target_norm = _normalize_text(target_hint)
    raw_norm = _normalize_text(raw_target) if raw_target else None
    target_words = [w for w in target_norm.split() if w]

    scored: List[AppCandidate] = []
    for candidate in candidates:
        score = _score_candidate(candidate, target_norm, target_words, raw_norm, host)
        if score <= 0:
            continue
        item = AppCandidate(**{**candidate.__dict__})
        item.score = score
        scored.append(item)

    if not scored:
        return None

    scored.sort(key=lambda c: (c.score, _parse_last_seen(c.last_seen)), reverse=True)
    best = scored[0]
    alternates = scored[1:4]
    ambiguous = False
    if alternates:
        second = alternates[0]
        if best.score - second.score <= 20:
            ambiguous = True

    return best, alternates, ambiguous


def _load_candidates(conn: sqlite3.Connection, host: Optional[str]) -> List[AppCandidate]:
    tables = _list_tables(conn)
    if "apps" in tables:
        return _load_from_apps_table(conn, host)
    if {"installs", "apps_catalog"}.issubset(tables):
        return _load_from_catalog(conn, host)
    _LOGGER.debug("Esquema desconocido en apps.sqlite: %s", tables)
    return []


def _load_from_apps_table(conn: sqlite3.Connection, host: Optional[str]) -> List[AppCandidate]:
    columns = _get_columns(conn, "apps")
    rows = conn.execute("SELECT * FROM apps").fetchall()
    results: List[AppCandidate] = []
    for row in rows:
        data = dict(row)
        row_host = data.get("host") or data.get("hostname")
        if host and row_host and row_host.lower() != host.lower():
            # keep candidate but mark host mismatch with lower score later
            pass
        name = str(data.get("name") or data.get("display_name") or "").strip()
        display_name = str(data.get("display_name") or name)
        normalized_name = _normalize_text(
            data.get("normalized_name") or data.get("name") or display_name
        )
        source = _normalise_source(data.get("source") or data.get("type"))
        exe_path = data.get("exe_path") or data.get("path")
        uwp_package = (
            data.get("uwp_package")
            or data.get("uwp_package_fullname")
            or data.get("package_fullname")
        )
        aumid = data.get("aumid") or data.get("app_id") or data.get("app_user_model_id")
        last_seen = data.get("last_seen") or data.get("last_seen_at") or data.get("updated_at")
        publisher = data.get("publisher")
        results.append(
            AppCandidate(
                name=name or display_name,
                display_name=display_name,
                normalized_name=normalized_name,
                source=source,
                exe_path=exe_path,
                uwp_package=uwp_package,
                aumid=aumid,
                last_seen=last_seen,
                host=row_host,
                publisher=publisher,
            )
        )
    return results


def _load_from_catalog(conn: sqlite3.Connection, host: Optional[str]) -> List[AppCandidate]:
    installs_cols = _get_columns(conn, "installs")
    apps_cols = _get_columns(conn, "apps_catalog")
    packages_cols = _get_columns(conn, "packages") if "packages" in _list_tables(conn) else set()
    binaries_cols = _get_columns(conn, "binaries") if "binaries" in _list_tables(conn) else set()
    hosts_cols = _get_columns(conn, "hosts") if "hosts" in _list_tables(conn) else set()

    select_parts = [
        "ac.display_name AS display_name",
        "ac.normalized_name AS normalized_name",
    ]
    if "publisher" in apps_cols:
        select_parts.append("ac.publisher AS publisher")
    if "source" in installs_cols:
        select_parts.append("inst.source AS source")
    if "aumid" in installs_cols:
        select_parts.append("inst.aumid AS aumid")
    if "last_seen_at" in installs_cols:
        select_parts.append("inst.last_seen_at AS last_seen_at")
    if "host_id" in installs_cols:
        select_parts.append("inst.host_id AS host_id")
    if "package_id" in installs_cols and packages_cols:
        if "package_fullname" in packages_cols:
            select_parts.append("pk.package_fullname AS package_fullname")
        if "package_family_name" in packages_cols:
            select_parts.append("pk.package_family_name AS package_family_name")
    if "binary_id" in installs_cols and binaries_cols:
        if "exe_path" in binaries_cols:
            select_parts.append("bn.exe_path AS exe_path")
        if "target_path" in binaries_cols:
            select_parts.append("bn.target_path AS target_path")
    if hosts_cols:
        if "hostname" in hosts_cols:
            select_parts.append("h.hostname AS hostname")

    base_query = [
        f"SELECT {', '.join(select_parts)}",
        "FROM installs AS inst",
        "JOIN apps_catalog AS ac ON ac.id = inst.app_catalog_id",
    ]
    if "package_id" in installs_cols and packages_cols:
        base_query.append("LEFT JOIN packages AS pk ON pk.id = inst.package_id")
    if "binary_id" in installs_cols and binaries_cols:
        base_query.append("LEFT JOIN binaries AS bn ON bn.id = inst.binary_id")
    if hosts_cols:
        base_query.append("LEFT JOIN hosts AS h ON h.id = inst.host_id")

    where_clauses: List[str] = []
    params: List[object] = []
    if "is_active" in installs_cols:
        where_clauses.append("inst.is_active = 1")
    if host and hosts_cols and "hostname" in hosts_cols:
        where_clauses.append("h.hostname = ?")
        params.append(host)

    if where_clauses:
        base_query.append("WHERE " + " AND ".join(where_clauses))

    base_query.append("ORDER BY inst.last_seen_at DESC")

    query = "\n".join(base_query)
    rows = conn.execute(query, params).fetchall()
    results: List[AppCandidate] = []
    for row in rows:
        data = dict(row)
        display_name = str(data.get("display_name") or "").strip()
        normalized_name = _normalize_text(data.get("normalized_name") or display_name)
        source = _normalise_source(data.get("source"))
        exe_path = data.get("exe_path") or data.get("target_path")
        uwp_package = (
            data.get("package_fullname")
            or data.get("package_family_name")
        )
        aumid = data.get("aumid")
        last_seen = data.get("last_seen_at")
        host_name = data.get("hostname")
        publisher = data.get("publisher")
        results.append(
            AppCandidate(
                name=display_name or normalized_name,
                display_name=display_name or normalized_name,
                normalized_name=normalized_name,
                source=source,
                exe_path=exe_path,
                uwp_package=uwp_package,
                aumid=aumid,
                last_seen=last_seen,
                host=host_name,
                publisher=publisher,
            )
        )
    return results


def _score_candidate(
    candidate: AppCandidate,
    target_norm: str,
    target_words: List[str],
    raw_norm: Optional[str],
    host: Optional[str],
) -> float:
    score = 0.0
    name_norm = candidate.normalized_name
    display_norm = _normalize_text(candidate.display_name or candidate.name)

    if not target_norm:
        return 0.0

    if name_norm == target_norm or display_norm == target_norm:
        score += 90
    elif display_norm.startswith(target_norm):
        score += 70
    elif target_norm in display_norm:
        score += 55

    if raw_norm and raw_norm in (name_norm, display_norm):
        score += 12

    alias = _resolve_alias(target_norm)
    if alias and alias == name_norm:
        score = max(score, 80)

    match_words = sum(1 for word in target_words if word and word in display_norm.split())
    score += match_words * 8

    if candidate.source == "uwp" and ("tienda" in target_words or "uwp" in target_words):
        score += 5

    if candidate.exe_path:
        score += 5

    if candidate.last_seen:
        score += 3

    if host:
        if candidate.host and candidate.host.lower() == host.lower():
            score += 15
        elif candidate.host:
            score -= 5

    return max(score, 0.0)


def _format_candidate_notes(
    best: AppCandidate,
    alternates: List[AppCandidate],
    ambiguous: bool,
) -> str:
    items = [f"{best.display_name or best.name} ({best.source})"]
    for alt in alternates[:2]:
        items.append(f"{alt.display_name or alt.name} ({alt.source})")
    if ambiguous and items:
        return "candidatos: " + "; ".join(items)
    if items:
        return "coincidencia: " + items[0]
    return ""


def _empty_response(
    *,
    action: str,
    confidence: float,
    notes: str,
    trace_id: str,
) -> Dict[str, object]:
    return {
        "action": action,
        "target": {
            "name": None,
            "source": None,
            "exe_path": None,
            "uwp_package": None,
            "aumid": None,
        },
        "confidence": round(max(0.0, min(confidence, 1.0)), 2),
        "notes": notes,
        "trace_id": trace_id,
    }


def _join_notes(parts: Iterable[str], extra: Optional[str] = None) -> str:
    notes = [p for p in parts if p]
    if extra:
        notes.append(extra)
    return " | ".join(notes)


def _normalise_action(action: Optional[str]) -> str:
    if not action:
        return FALLBACK_ACTION
    lowered = _normalize_text(str(action))
    if lowered in VALID_ACTIONS:
        return lowered
    if lowered in ACTION_SYNONYMS:
        mapped = ACTION_SYNONYMS[lowered]
        return mapped if mapped in VALID_ACTIONS else FALLBACK_ACTION
    return FALLBACK_ACTION


def _resolve_alias(name: str) -> str:
    normalized = _normalize_text(name)
    return APP_ALIASES.get(normalized, normalized)


def _format_user_prompt(text: str, normalized: str) -> str:
    return f"Orden original: {text}\nFrase normalizada: {normalized}\nRespondé solo JSON"


def _load_system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        try:
            _SYSTEM_PROMPT = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            _LOGGER.warning("system.txt no encontrado en %s", SYSTEM_PROMPT_FILE)
            _SYSTEM_PROMPT = ""
    return _SYSTEM_PROMPT


def _load_fewshots() -> List[Tuple[str, str]]:
    global _FEWSHOTS
    if _FEWSHOTS:
        return _FEWSHOTS
    if not FEWSHOT_FILE.exists():
        _LOGGER.warning("fewshot.jsonl no encontrado en %s", FEWSHOT_FILE)
        _FEWSHOTS = []
        return _FEWSHOTS
    examples: List[Tuple[str, str]] = []
    for line in FEWSHOT_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            user = str(data["input"])
            assistant = str(data["output"])
            examples.append((user, assistant))
        except Exception as exc:  # pragma: no cover - defensivo
            _LOGGER.warning("Few-shot inválido: %s", exc)
    _FEWSHOTS = examples
    return _FEWSHOTS


def _list_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def _get_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {row[1] for row in rows}


def _normalise_source(raw: Optional[str]) -> str:
    if not raw:
        return "exe"
    value = str(raw).lower()
    if value in {"uwp", "store"}:
        return "uwp"
    return "exe"


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_last_seen(value: Optional[str]) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


__all__ = ["interpret", "call_llm"]
