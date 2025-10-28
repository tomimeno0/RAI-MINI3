import pytest

from scheduler import parse_duration
from system_controls import normalize_percent


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("10 minutos", 600),
        ("45", 2700),
        ("15 segundos", 15),
        ("2 horas", 7200),
        ("1.5 horas", 5400),
    ],
)
def test_parse_duration(texto, esperado):
    assert parse_duration(texto) == esperado


def test_parse_duration_error():
    with pytest.raises(ValueError):
        parse_duration("cualquier momento")


@pytest.mark.parametrize(
    "valor, esperado",
    [
        (50, 50),
        (150, 100),
        (-10, 0),
        ("80", 80),
        (33.6, 34),
    ],
)
def test_normalize_percent(valor, esperado):
    assert normalize_percent(valor) == esperado


def test_normalize_percent_error():
    with pytest.raises(ValueError):
        normalize_percent("no-numero")
