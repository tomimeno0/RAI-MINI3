import importlib
import json
import logging
import os
import sys


def _reload(module_name: str):
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def test_server_logger_emits_json(monkeypatch, tmp_path):
    log_path = tmp_path / "server.log"
    monkeypatch.setenv("RAI_SERVER_LOG_PATH", str(log_path))
    logging_utils = _reload("rai.server.logging_utils")

    logger = logging_utils.get_logger("tests")
    trace_id = "trace-123"
    with logging_utils.with_trace_id(logger, trace_id) as log:
        log.info("evento", extra={"foo": "bar"})

    for handler in logging.getLogger("rai.server").handlers:
        handler.flush()

    contents = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert contents, "expected log file to contain at least one line"
    payload = json.loads(contents[-1])
    assert payload["trace_id"] == trace_id
    assert payload["msg"] == "evento"
    assert payload["extra"]["foo"] == "bar"
    assert payload["name"].endswith("tests")


def test_client_logger_uses_rotating_handler(monkeypatch, tmp_path):
    log_path = tmp_path / "client.log"
    monkeypatch.setenv("RAI_CLIENT_LOG_PATH", str(log_path))
    logging_utils = _reload("rai.client.logging_utils")

    logger = logging_utils.get_logger("tests")
    with logging_utils.with_trace_id(logger, "abc") as log:
        log.debug("hola", extra={})

    root_logger = logging.getLogger("rai.client")
    assert root_logger.handlers, "root logger should have at least one handler"
    handler = root_logger.handlers[0]
    assert handler.baseFilename == os.fspath(log_path)
    assert getattr(handler, "maxBytes", None) == 10 * 1024 * 1024
    assert getattr(handler, "backupCount", None) == 5
