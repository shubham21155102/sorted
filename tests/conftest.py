import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _no_live_phone_calls(monkeypatch):
    """Tests must never reach Sarvam (or dial anyone), whatever credentials are in .env."""
    from errand_agent import sarvam

    def blocked(*a, **k):
        raise RuntimeError("network access to Sarvam is disabled in tests")
    monkeypatch.setattr(sarvam, "_request", blocked)
    monkeypatch.setattr(sarvam, "place_call", blocked)
    monkeypatch.setattr(sarvam, "configured", lambda for_outbound=True: ["(disabled in tests)"])
