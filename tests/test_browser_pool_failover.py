from pathlib import Path
import sys


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import flow_core  # noqa: E402


def _reset_pool(monkeypatch) -> None:
    monkeypatch.setattr(flow_core, "_BROWSER_POOL_ENV_KEY", None)
    monkeypatch.setattr(flow_core, "_BROWSER_POOL", [])
    monkeypatch.setattr(flow_core, "_BROWSER_POOL_CURSOR", 0)
    flow_core._BROWSER_LABEL_COOLDOWN_UNTIL.clear()
    flow_core._BROWSER_LABEL_LAST_REASON.clear()


def test_browser_pool_skips_disabled_workspace_and_reprobes_later(monkeypatch) -> None:
    monkeypatch.setenv(
        "BROWSER_CDP_POOL",
        "modal-primary|3|wss://primary.example;;modal-fallback|1|wss://fallback.example",
    )
    _reset_pool(monkeypatch)

    label, _url = flow_core._next_browser_cdp_target(now=100.0)
    assert label == "modal-primary"
    cooldown = flow_core._mark_browser_target_failure(
        label, RuntimeError("404 workspace is disabled"), now=100.0
    )
    assert cooldown == 1800

    assert flow_core._next_browser_cdp_target(now=101.0)[0] == "modal-fallback"
    assert flow_core._next_browser_cdp_target(now=1901.0)[0] == "modal-primary"


def test_success_clears_browser_target_cooldown(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_CDP_POOL", "modal-fallback|1|wss://fallback.example")
    _reset_pool(monkeypatch)
    flow_core._mark_browser_target_failure("modal-fallback", RuntimeError("503"), now=20.0)
    assert "modal-fallback" in flow_core._BROWSER_LABEL_COOLDOWN_UNTIL

    flow_core._mark_browser_target_success("modal-fallback")

    assert "modal-fallback" not in flow_core._BROWSER_LABEL_COOLDOWN_UNTIL
