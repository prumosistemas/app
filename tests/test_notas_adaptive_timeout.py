import sys
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from flow_notas import adaptive_timeout_ms  # noqa: E402


def test_adaptive_timeout_grows_by_retry(monkeypatch) -> None:
    monkeypatch.delenv("TEST_NOTAS_TIMEOUT_MS", raising=False)
    first = adaptive_timeout_ms(
        "TEST_NOTAS_TIMEOUT_MS",
        100_000,
        retry_level=0,
        configured_max_ms=200_000,
        hard_max_ms=250_000,
    )
    third = adaptive_timeout_ms(
        "TEST_NOTAS_TIMEOUT_MS",
        100_000,
        retry_level=2,
        configured_max_ms=200_000,
        hard_max_ms=250_000,
    )
    assert first == 100_000
    assert third == 150_000


def test_adaptive_timeout_is_capped(monkeypatch) -> None:
    monkeypatch.setenv("TEST_NOTAS_TIMEOUT_MS", "200000")
    value = adaptive_timeout_ms(
        "TEST_NOTAS_TIMEOUT_MS",
        100_000,
        retry_level=5,
        configured_max_ms=200_000,
        hard_max_ms=250_000,
    )
    assert value == 250_000
