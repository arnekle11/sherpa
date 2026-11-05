import pytest


@pytest.fixture(autouse=True)
def _no_real_endpoints(monkeypatch):
    """Tests must never ping a real dead-man URL or message a real Telegram chat from a developer's shell."""
    for var in ("SHERPA_DEADMAN_URL", "SHERPA_TELEGRAM_TOKEN", "SHERPA_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
