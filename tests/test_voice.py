"""Voice layer: speech text conversion and the LiveKit token/dispatch endpoint (no network)."""
import json

import pytest

from errand_agent.voice_text import speakable
from errand_agent.brand import SLUG


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
    assert out["code"] == 200 and out["obj"]["room"].startswith(f"{SLUG}-c-abc123-")
    claims = jwt.decode(out["obj"]["token"], options={"verify_signature": False})
    assert claims["video"]["room"] == out["obj"]["room"] and claims["video"]["roomJoin"]
    dispatch = claims["roomConfig"]["agents"][0]
    assert dispatch["agentName"] == f"{SLUG}-voice" and json.loads(dispatch["metadata"]) == {"session_id": "c-abc123"}
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
    a.handle_user_message("Indiranagar, next week, under 1500")
    h, out = _handler(app)
    h.brief("c-2")
    assert "Approval needed" in out["obj"]["text"]  # outbound call would open by reading the pending approval
    h, out = _handler(app, {"text": "📞 Phone call ended · 1m 05s"})
    h.call_event("c-2")
    assert app.reader.messages("c-2")[-1]["kind"] == "call"
    h, out = _handler(app, query={})
    h.messages("c-2")
    assert out["obj"]["phase"] == "awaiting_approval" and out["obj"]["missing"] == []


# ------------------------------------------------------------- Sarvam phone line
class _Headers(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def _sarvam(app, path_handler, body, token="tok", headers=None):
    import io
    from errand_agent import server
    H = server.make_handler(app)
    h = H.__new__(H)
    out = {}
    h._json = lambda obj, code=200: out.update(obj=obj, code=code)
    raw = json.dumps(body).encode()
    hdrs = _Headers({"Content-Length": str(len(raw)), **({"Authorization": f"Bearer {token}"} if token else {}),
                     **(headers or {})})
    h.rfile, h.headers, h.q = io.BytesIO(raw), hdrs, {}
    getattr(h, path_handler)()
    return out


@pytest.fixture
def sarvam_app(app, monkeypatch):
    monkeypatch.setenv("ERRAND_SARVAM_TOKEN", "tok")
    _handler(app, {"phone": "+919876543210"})[0].put_profile()
    from errand_agent.scripted_model import ScriptedModel
    real = app.agent

    def agent(sid, channel="chat"):  # every chat on the offline model
        a = real(sid)
        if a.model_name != "scripted":
            a.set_model(ScriptedModel(), "scripted")
        return a
    app.agent = agent
    return app


def test_sarvam_turn_requires_token(sarvam_app):
    out = _sarvam(sarvam_app, "sarvam_turn", {"text": "hi", "caller": "+919876543210"}, token="wrong")
    assert out["code"] == 401


def test_sarvam_inbound_call_flow_creates_chat_and_books(sarvam_app):
    app = sarvam_app
    app.world.clear_faults("book_outcome"); app.world.queue_fault("book_outcome", "success")  # this test is about the call flow, not provider failures
    call = {"caller": "+919876543210", "interaction_id": "20261108/abc-1", "session_id": "@session_id"}
    r = _sarvam(app, "sarvam_turn", {**call, "text": "Find a plumber in Indiranagar next week, mornings, under 1500"})
    sid = r["obj"]["session_id"]
    assert "To confirm" in r["obj"]["say"] and "**" not in r["obj"]["say"]  # spoken approval, no markdown
    r = _sarvam(app, "sarvam_turn", {**call, "text": "haan, book kar do"})
    assert r["obj"]["session_id"] == sid and "confirmed with the provider" in r["obj"]["say"]
    assert len(app.world.side_effects()) == 1
    hook = {"app_id": "a", "app_version": 1, "deployment_id": "d", "interaction_id": "20261108/abc-1",
            "user_phone_number": "+919876543210", "agent_phone_number": "+918000000000", "duration": 75.4,
            "metadata": {"token": "tok"}}
    assert _sarvam(app, "sarvam_webhook", hook, token=None)["obj"] == {"ok": True}
    assert _sarvam(app, "sarvam_webhook", hook, token=None)["obj"].get("duplicate")  # retried delivery
    msgs = app.reader.messages(sid)
    assert msgs[0]["kind"] == "call" and msgs[-1]["content"] == "📞 Phone call ended · 1m 15s"
    assert [m["kind"] for m in msgs[1:-1]] == ["phone"] * 4
    assert app.reader.session(sid)["title"].startswith("Phone call")


def test_sarvam_inbound_rejects_unregistered_caller(sarvam_app):
    r = _sarvam(sarvam_app, "sarvam_turn", {"caller": "+919000000001", "interaction_id": "x", "text": "book a plumber"})
    assert r["obj"]["end_call"] is True and "registered" in r["obj"]["say"]
    assert not sarvam_app.world.side_effects()


def test_sarvam_outbound_continues_existing_chat_and_reports_no_answer(sarvam_app):
    app = sarvam_app
    app.agent("c-out").handle_user_message("I need a plumber")
    r = _sarvam(app, "sarvam_turn", {"caller": "", "interaction_id": "int-9", "session_id": "c-out",
                                     "text": "Indiranagar, next week, under 1500"})
    assert r["obj"]["session_id"] == "c-out" and "To confirm" in r["obj"]["say"]
    assert any(m["content"] == "📞 Phone call connected" for m in app.reader.messages("c-out"))
    app.store.add_phone_call("att-2", "c-out", "sarvam", "outbound", "+919876543210", "t")
    nohook = {"attempt_id": "att-2", "status": "no_answer", "channel_info": {}, "duration": None,
              "interaction_id": None, "webhook_config": {"url": "u", "metadata": {"token": "tok", "session_id": "c-out"}}}
    _sarvam(app, "sarvam_webhook", nohook, token=None)
    assert app.reader.messages("c-out")[-1]["content"].startswith("📞 Couldn't reach you (no answer)")


def test_public_tunnel_only_reaches_sarvam_endpoints(sarvam_app):
    import io
    from errand_agent import server
    H = server.make_handler(sarvam_app)
    for path, ok in (("/api/sessions", False), ("/api/dev/restart", False), ("/", False), ("/api/sarvam/turn", True)):
        h = H.__new__(H)
        out = {}
        h._json = lambda obj, code=200, out=out: out.update(obj=obj, code=code)
        h.path, h.rfile = path, io.BytesIO(b"{}")
        h.headers = _Headers({"Host": "abc.trycloudflare.com", "Cf-Connecting-Ip": "1.2.3.4", "Content-Length": "2"})
        h.index = lambda: out.update(code=200)
        h._route("POST" if "sarvam" in path else "GET")
        assert (out.get("code") != 404) == ok, path
