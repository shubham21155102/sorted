"""Voice layer: speech text conversion and the LiveKit token/dispatch endpoint (no network)."""
import json

import pytest

from errand_agent.voice_text import speakable


def test_speakable_approval_block():
    text = ("I compared 4 providers. Best fit: **QuickPipe** (★4.8, ₹990).\n\n"
            "🔒 Approval needed [PF02CC]: Book **QuickPipe** (PLU-IND-2) for plumber on **Mon 09 Nov, 09:00** "
            "at **₹1,990**. Slot held until 10:56.\nReply **yes** (or `confirm PF02CC`) to proceed, or tell me what to change.")
    s = speakable(text)
    assert "Monday 9 November at 9 AM for 1990 rupees" in s
    assert "Say yes to confirm" in s and "rated 4.8" in s
    for junk in ("**", "PLU-IND-2", "PF02CC", "₹", "🔒", "held until"):
        assert junk not in s


def test_speakable_status_lines():
    assert speakable("Done!\n\n✅ Verified: booking **BK-1004** confirmed — QuickPipe, Sun 08 Nov, 11:30, ₹990 "
                     "(checked via get_booking_status).") == "Done! Booking BK-1004 is confirmed with the provider."
    assert speakable("❌ Not completed [PAB12C]: slot was taken by someone else. Nothing was charged.") \
        .startswith("That didn't go through: slot was taken")
    assert speakable("Moved to Sat 14 Nov, 14:30.") == "Moved to Saturday 14 November at 2:30 PM."


def test_voice_token_dispatches_agent_with_session(monkeypatch, tmp_path):
    pytest.importorskip("livekit.api")
    import io
    import jwt  # PyJWT ships with livekit-api

    from errand_agent import server

    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APItest")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s" * 44)
    app = server.App(str(tmp_path), "scripted", fresh=True)
    H = server.make_handler(app)
    h = H.__new__(H)
    out = {}
    h._json = lambda obj, code=200: out.update(obj=obj, code=code)
    h.rfile, h.headers = io.BytesIO(b'{"session_id": "c-abc123"}'), {"Content-Length": "26"}
    h.voice_token()
    assert out["code"] == 200 and out["obj"]["room"].startswith("errand-c-abc123-")
    claims = jwt.decode(out["obj"]["token"], options={"verify_signature": False})
    assert claims["video"]["room"] == out["obj"]["room"] and claims["video"]["roomJoin"]
    dispatch = claims["roomConfig"]["agents"][0]
    assert dispatch["agentName"] == "errand-voice" and json.loads(dispatch["metadata"]) == {"session_id": "c-abc123"}
    assert app.reader.session("c-abc123") is not None  # chat exists so the transcript has a home


def _handler(app, body=None, query=None):
    """Call server handler methods directly (no sockets)."""
    import io
    from errand_agent import server
    H = server.make_handler(app)
    h = H.__new__(H)
    out = {}
    h._json = lambda obj, code=200: out.update(obj=obj, code=code)
    raw = json.dumps(body or {}).encode()
    h.rfile, h.headers, h.q = io.BytesIO(raw), {"Content-Length": str(len(raw))}, query or {}
    return h, out


@pytest.fixture
def app(tmp_path, monkeypatch):
    from errand_agent import server
    monkeypatch.delenv("ERRAND_SIP_OUTBOUND_TRUNK_ID", raising=False)
    return server.App(str(tmp_path), "scripted", fresh=True)


def test_profile_phone_validation(app):
    h, out = _handler(app, {"phone": "98765 43210"})
    h.put_profile()
    assert out["code"] == 400
    h, out = _handler(app, {"phone": "+91 98765 43210"})
    h.put_profile()
    assert out["obj"]["phone"] == "+919876543210" and app.reader.profile("u1")["phone"] == "+919876543210"


def test_inbound_phone_only_registered_number(app):
    h, out = _handler(app, {"from": "+919876543210"})
    h.phone_inbound()
    assert out["obj"] == {"allowed": False}  # no number registered yet
    _handler(app, {"phone": "+919876543210"})[0].put_profile()
    h, out = _handler(app, {"from": "+919000000001"})
    h.phone_inbound()
    assert out["obj"] == {"allowed": False}
    h, out = _handler(app, {"from": "+919876543210"})
    h.phone_inbound()
    sid = out["obj"]["session_id"]
    assert out["obj"]["allowed"] and app.reader.session(sid)["title"].startswith("Phone call")


def test_call_me_errors_are_explicit(app, monkeypatch):
    h, out = _handler(app, {})
    h.call_me("c-1")
    assert out["code"] == 400 and out["obj"]["error"] == "no_phone"
    _handler(app, {"phone": "+919876543210"})[0].put_profile()
    h, out = _handler(app, {})
    h.call_me("c-1")
    assert out["obj"]["error"] == "phone_not_configured"


def test_call_brief_and_markers(app):
    from errand_agent.scripted_model import ScriptedModel
    a = app.agent("c-2")
    a.set_model(ScriptedModel(), "scripted")
    a.handle_user_message("I need a plumber")
    h, out = _handler(app)
    h.brief("c-2")
    assert "plumber" in out["obj"]["text"] and "need" in out["obj"]["text"]  # asks for the missing details
    a.handle_user_message("Indiranagar, this week, under 1500")
    h, out = _handler(app)
    h.brief("c-2")
    assert "Approval needed" in out["obj"]["text"]  # outbound call would open by reading the pending approval
    h, out = _handler(app, {"text": "📞 Phone call ended · 1m 05s"})
    h.call_event("c-2")
    assert app.reader.messages("c-2")[-1]["kind"] == "call"
    h, out = _handler(app, query={})
    h.messages("c-2")
    assert out["obj"]["phase"] == "awaiting_approval" and out["obj"]["missing"] == []
