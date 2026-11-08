"""Local web app: chat history sidebar, conversation view, and a trace / memory / bookings panel.

    python -m errand_agent.server [--model litellm|scripted] [--port 8765] [--fresh]

One ErrandAgent per chat session; all chats share the same user memory, mock
provider world and model. A background poller calls process_events() so provider
cancellations show up in the chat without a user message.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .agent import ErrandAgent
from .app import ARCHIVE_SESSION, default_model_kind, make_agent, make_llm, seed_store
from .connectors import ConnectorHub
from .models import Catalog
from .clock import SystemClock
from .mock_env import MockWorld, ScenarioConfig
from .store import Store

STATIC = os.path.join(os.path.dirname(__file__), "static", "index.html")
USER_ID = "u1"
USER_NAME = os.environ.get("ERRAND_USER_NAME", "Shubham")


class App:
    def __init__(self, data: str, model: str | None, fresh: bool):
        self.data = data
        self.kind = model or default_model_kind()
        os.makedirs(data, exist_ok=True)
        self.db_path = os.path.join(data, "agent.sqlite")
        self.world_path = os.path.join(data, "world.json")
        self.trace_path = os.path.join(data, "trace.jsonl")
        if fresh:
            for p in (self.db_path, self.db_path + "-wal", self.db_path + "-shm", self.world_path, self.trace_path):
                if os.path.exists(p):
                    os.remove(p)
        self.lock = threading.RLock()  # serialises everything that mutates agent/world state
        self._boot()

    def _boot(self) -> None:
        new = not os.path.exists(self.db_path)
        self.store = Store(self.db_path)
        if new:
            seed_store(self.store, USER_ID)
        self.reader = Store(self.db_path)  # separate connection: reads never wait on a running agent turn
        self.world = MockWorld(ScenarioConfig(), SystemClock(), self.world_path)
        self.catalog = Catalog()
        self.hub = ConnectorHub(self.store, USER_ID, self.world.clock)  # Google Calendar / Gmail
        self.llms: dict[str, Any] = {}
        self.agents: dict[str, ErrandAgent] = {}

    def default_model(self) -> str:
        return self.catalog.default_model() if self.kind == "litellm" else "scripted"

    def llm_for(self, model_id: str) -> Any:
        """One adapter per model id; the scripted model is the offline option."""
        if model_id not in self.llms:
            if model_id == "scripted" or self.kind != "litellm":
                self.llms[model_id] = make_llm("scripted")
            else:
                from .llm import LiteLLM
                info = self.catalog.get(model_id) or {}
                price = (info.get("input_cost_per_token"), info.get("output_cost_per_token"))
                self.llms[model_id] = LiteLLM(model=model_id, pricing=price if price[0] is not None else None)
        return self.llms[model_id]

    def session_model(self, sid: str) -> str:
        row = self.reader.session(sid)
        return (row or {}).get("model") or self.default_model()

    def agent(self, sid: str) -> ErrandAgent:
        with self.lock:
            model = self.session_model(sid)
            if sid not in self.agents:
                self.agents[sid] = make_agent(self.store, self.world, self.llm_for(model), model, sid, USER_ID,
                                              self.trace_path, connectors=self.hub)
            elif self.agents[sid].model_name != model:
                self.agents[sid].set_model(self.llm_for(model), model)
            return self.agents[sid]

    def restart(self) -> None:
        """Simulate a process restart: drop all in-memory state and rebuild from disk."""
        with self.lock:
            self.store.close()
            self.reader.close()
            self._boot()

    def latest_session(self) -> str | None:
        rows = [s for s in self.reader.sessions(USER_ID) if s["id"] != ARCHIVE_SESSION]
        return rows[0]["id"] if rows else None

    def poll_events(self) -> None:
        while True:
            time.sleep(3)
            try:
                if not self.world.poll_events(USER_ID):
                    continue
                sid = self.latest_session()
                with self.lock:
                    if not sid:
                        sid = new_session_id()
                        self.store.create_session(sid, USER_ID, now(), "Updates")
                    self.agent(sid).process_events()
            except Exception as e:  # never let the poller die silently
                print(f"event poller error: {e}")

    def trace(self, sid: str, since: int) -> list[dict[str, Any]]:
        if not os.path.exists(self.trace_path):
            return []
        out = []
        with open(self.trace_path) as f:
            for line in f:
                if f'"session_id": "{sid}"' in line:
                    out.append(line)
        return [json.loads(x) for x in out[since:]]


def sarvam_missing() -> list[str]:
    from . import sarvam
    return sarvam.configured()


def now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def new_session_id() -> str:
    return "c-" + uuid.uuid4().hex[:10]


ROUTES: list[tuple[str, str, str]] = [
    ("GET", r"^/$", "index"),
    ("GET", r"^/api/bootstrap$", "bootstrap"),
    ("GET", r"^/api/models$", "models"),
    ("GET", r"^/api/voice/config$", "voice_config"),
    ("GET", r"^/api/profile$", "get_profile"),
    ("GET", r"^/static/(?P<folder>logos|brand)/(?P<name>[a-z0-9-]+\.(?:svg|png|ico))$", "asset"),
    ("GET", r"^/favicon\.ico$", "favicon"),
    ("GET", r"^/api/connectors$", "connectors"),
    ("GET", r"^/api/connectors/google/connect$", "google_connect"),
    ("GET", r"^/api/connectors/google/callback$", "google_callback"),
    ("POST", r"^/api/connectors/google/disconnect$", "google_disconnect"),
    ("PATCH", r"^/api/connectors/settings$", "connector_settings"),
    ("PUT", r"^/api/profile$", "put_profile"),
    ("POST", r"^/api/sessions/(?P<sid>[\w-]+)/call_me$", "call_me"),
    ("GET", r"^/api/sessions/(?P<sid>[\w-]+)/brief$", "brief"),
    ("POST", r"^/api/sessions/(?P<sid>[\w-]+)/call_event$", "call_event"),
    ("POST", r"^/api/phone/inbound$", "phone_inbound"),
    ("POST", r"^/api/sarvam/turn$", "sarvam_turn"),
    ("POST", r"^/api/sarvam/webhook$", "sarvam_webhook"),
    ("POST", r"^/api/voice/token$", "voice_token"),
    ("GET", r"^/api/sessions$", "list_sessions"),
    ("POST", r"^/api/sessions$", "create_session"),
    ("GET", r"^/api/sessions/(?P<sid>[\w-]+)/messages$", "messages"),
    ("POST", r"^/api/sessions/(?P<sid>[\w-]+)/chat$", "chat"),
    ("GET", r"^/api/sessions/(?P<sid>[\w-]+)/trace$", "trace"),
    ("GET", r"^/api/sessions/(?P<sid>[\w-]+)/state$", "state"),
    ("PATCH", r"^/api/sessions/(?P<sid>[\w-]+)$", "rename"),
    ("DELETE", r"^/api/sessions/(?P<sid>[\w-]+)$", "delete"),
    ("GET", r"^/api/memories$", "memories"),
    ("POST", r"^/api/memories/(?P<mid>[\w-]+)/forget$", "forget"),
    ("GET", r"^/api/bookings$", "bookings"),
    ("POST", r"^/api/dev/force$", "dev_force"),
    ("POST", r"^/api/dev/provider_cancel$", "dev_cancel"),
    ("POST", r"^/api/dev/restart$", "dev_restart"),
]


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        # ------------------------------------------------------------ io
        def _json(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _external(self) -> bool:
            """True when the request came through a public tunnel rather than from this machine."""
            host = (self.headers.get("Host") or "").split(":")[0]
            forwarded = any(self.headers.get(h) for h in ("X-Forwarded-For", "Cf-Connecting-Ip", "X-Real-Ip"))
            return forwarded or host not in ("127.0.0.1", "localhost", "")

        def _route(self, method: str) -> None:
            url = urlparse(self.path)
            self.q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if self._external() and not url.path.startswith("/api/sarvam/"):
                # Only the Sarvam endpoints (token-protected) are reachable through the public URL.
                return self._json({"error": "not found"}, 404)
            for m, pat, name in ROUTES:
                if m == method:
                    match = re.match(pat, url.path)
                    if match:
                        try:
                            return getattr(self, name)(**match.groupdict())
                        except Exception as e:
                            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            self._json({"error": "not found"}, 404)

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

        def do_PATCH(self) -> None:
            self._route("PATCH")

        def do_DELETE(self) -> None:
            self._route("DELETE")

        def do_PUT(self) -> None:
            self._route("PUT")

        # -------------------------------------------------------- handlers
        ASSET_TYPES = {"svg": "image/svg+xml", "png": "image/png", "ico": "image/x-icon"}

        def asset(self, folder: str, name: str) -> None:
            """Images next to index.html: model-company logos and the Errand logo/favicons.
            The route regex allows only [a-z0-9-] names in two fixed folders, so no path traversal."""
            path = os.path.join(os.path.dirname(STATIC), folder, name)
            if not os.path.isfile(path):
                return self._json({"error": "not found"}, 404)
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", self.ASSET_TYPES[name.rsplit(".", 1)[1]])
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        def favicon(self) -> None:
            self.asset("brand", "favicon.ico")

        def index(self) -> None:
            with open(STATIC, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def bootstrap(self) -> None:
            self._json({"user": {"id": USER_ID, "name": USER_NAME}, "backend": app.kind,
                        "default_model": app.default_model()})

        def voice_config(self) -> None:
            ok = all(os.environ.get(k) for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"))
            self._json({"enabled": ok, "url": os.environ.get("LIVEKIT_URL") if ok else None,
                        "provider": "sarvam" if os.environ.get("SARVAM_API_KEY") else "livekit-inference",
                        "phone": {"outbound": (ok and bool(os.environ.get("ERRAND_SIP_OUTBOUND_TRUNK_ID")))
                                  or not sarvam_missing(),
                                  "provider": "sarvam" if not sarvam_missing() else "livekit",
                                  "inbound_number": os.environ.get("SARVAM_AGENT_NUMBER")
                                  or os.environ.get("ERRAND_INBOUND_NUMBER") or None},
                        "user_phone": app.reader.profile(USER_ID).get("phone")})

        def _redirect(self, url: str) -> None:
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _google_redirect_uri(self) -> str:
            # Loopback redirect (Google "Desktop app" OAuth clients accept any 127.0.0.1 port/path).
            return f"http://{self.headers.get('Host') or '127.0.0.1:8765'}/api/connectors/google/callback"

        def connectors(self) -> None:
            phone = {"id": "phone", "name": "Phone line (Sarvam)", "connected": not sarvam_missing(),
                     "account": os.environ.get("SARVAM_AGENT_NUMBER"),
                     "description": "Inbound calls to your Errand number and 'Call my phone'."}
            voice = {"id": "voice", "name": "Voice in the browser (LiveKit)",
                     "connected": all(os.environ.get(k) for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")),
                     "account": "Sarvam speech" if os.environ.get("SARVAM_API_KEY") else "LiveKit Inference",
                     "description": "Talk to Errand from this page (mic button)."}
            gateway = {"id": "models", "name": "Model gateway (LiteLLM)", "connected": app.kind == "litellm",
                       "account": os.environ.get("LITELLM_API_BASE") and "LiteLLM proxy" or app.kind,
                       "description": "The language models Errand thinks with (model picker)."}
            self._json({"connectors": app.hub.status() + [phone, voice, gateway],
                        "google_configured": ConnectorHub.google_configured(),
                        "redirect_uri": self._google_redirect_uri()})

        def google_connect(self) -> None:
            try:
                url = app.hub.begin_google(self._google_redirect_uri())
            except ValueError:
                return self._redirect("/#connectors?error=google_not_configured")
            self._redirect(url)

        def google_callback(self) -> None:
            if self.q.get("error"):
                return self._redirect(f"/#connectors?error={self.q['error']}")
            try:
                with app.lock:
                    app.hub.finish_google(self.q.get("code", ""), self.q.get("state", ""))
            except Exception as e:
                return self._redirect("/#connectors?error=" + re.sub(r"[^\w-]", "_", type(e).__name__))
            self._redirect("/#connectors?connected=google")

        def google_disconnect(self) -> None:
            with app.lock:
                app.hub.disconnect_google()
            self._json({"ok": True})

        def connector_settings(self) -> None:
            try:
                with app.lock:
                    self._json({"settings": app.hub.update_settings(self._body())})
            except ValueError as e:
                self._json({"error": str(e)}, 400)

        def get_profile(self) -> None:
            self._json({"name": USER_NAME, **app.reader.profile(USER_ID)})

        def put_profile(self) -> None:
            phone = (self._body().get("phone") or "").strip().replace(" ", "").replace("-", "")
            if phone and not re.match(r"^\+[1-9]\d{7,14}$", phone):
                return self._json({"error": "Use international format, e.g. +919876543210"}, 400)
            with app.lock:
                app.store.set_phone(USER_ID, phone or None, now())
            self._json({"phone": phone or None})

        def call_me(self, sid: str) -> None:
            """Outbound phone call: dispatch the voice agent to a fresh room; it dials the user's phone."""
            from livekit import api
            phone = (self._body().get("phone") or app.reader.profile(USER_ID).get("phone") or "").strip()
            if not phone:
                return self._json({"error": "no_phone", "message": "Add your phone number first."}, 400)
            from . import sarvam
            if not sarvam.configured():
                with app.lock:
                    app.store.create_session(sid, USER_ID, now(), model=app.default_model())
                    brief = app.agent(sid).call_brief()
                    from .voice_text import speakable
                    first = f"Hi {USER_NAME}, this is Errand. " + speakable(brief)
                    attempt = sarvam.place_call(phone, sid, first)
                    app.store.add_phone_call(attempt, sid, "sarvam", "outbound", phone, now())
                    app.store.add_message(sid, "assistant", f"📞 Calling {phone[:3]}•••{phone[-3:]} (Sarvam)…",
                                          now(), USER_ID, kind="call")
                return self._json({"status": "calling", "provider": "sarvam", "attempt_id": attempt, "phone": phone})
            if not os.environ.get("ERRAND_SIP_OUTBOUND_TRUNK_ID"):
                return self._json({"error": "phone_not_configured",
                                   "message": "Phone calls aren't set up yet (run python -m errand_agent.telephony setup)."}, 400)
            with app.lock:
                app.store.create_session(sid, USER_ID, now(), model=app.default_model())
            room = f"errand-{sid}-out-{uuid.uuid4().hex[:6]}"

            async def dispatch() -> Any:
                lk = api.LiveKitAPI(os.environ["LIVEKIT_URL"].replace("wss://", "https://"),
                                    os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
                try:
                    return await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
                        agent_name="errand-voice", room=room,
                        metadata=json.dumps({"session_id": sid, "mode": "outbound_phone", "phone": phone})))
                finally:
                    await lk.aclose()
            import asyncio
            d = asyncio.run(dispatch())
            self._json({"status": "calling", "room": room, "dispatch_id": d.id, "phone": phone})

        def brief(self, sid: str) -> None:
            with app.lock:
                text = app.agent(sid).call_brief()
            self._json({"text": text, "user_name": USER_NAME})

        def call_event(self, sid: str) -> None:
            """Call start/end markers in the chat history (written by the voice worker)."""
            text = (self._body().get("text") or "").strip()[:300]
            if text:
                with app.lock:
                    app.store.add_message(sid, "assistant", text, now(), USER_ID, kind="call")
            self._json({"ok": True})

        def _sarvam_authorized(self, body: dict[str, Any]) -> bool:
            token = os.environ.get("ERRAND_SARVAM_TOKEN")
            meta = body.get("metadata") or (body.get("webhook_config") or {}).get("metadata") or {}
            got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip() \
                or str(meta.get("token") or "")
            import hmac
            return bool(token) and hmac.compare_digest(got, token)

        def sarvam_turn(self) -> None:
            """Sarvam agent's per-turn tool: run the caller's words through Errand, return what to say."""
            from .voice_text import speakable
            b = self._body()
            if not self._sarvam_authorized(b):
                return self._json({"error": "unauthorized"}, 401)
            text = (b.get("text") or "").strip()
            call_id = str(b.get("interaction_id") or "").strip()
            caller = str(b.get("caller") or "").replace(" ", "")
            sid = str(b.get("session_id") or "").strip()
            if sid.startswith("@") or sid.startswith("{{"):
                sid = ""  # unresolved agent variable (inbound calls don't set it)
            with app.lock:
                known = app.reader.phone_call(call_id) if call_id else None
                if known:
                    sid = known["session_id"]
                elif sid and app.reader.session(sid):
                    if call_id:
                        app.store.add_phone_call(call_id, sid, "sarvam", "outbound", caller, now())
                        app.store.add_message(sid, "assistant", "📞 Phone call connected", now(), USER_ID, kind="call")
                else:  # a new inbound call: only the user's registered number may use the line
                    mine = app.reader.profile(USER_ID).get("phone")
                    if not mine or caller != mine:
                        return self._json({"say": "Sorry, this line only serves registered Errand users. Goodbye.",
                                           "end_call": True})
                    sid = new_session_id()
                    app.store.create_session(sid, USER_ID, now(), title=f"Phone call · {datetime.now():%d %b %H:%M}",
                                             model=app.default_model())
                    app.store.add_phone_call(call_id or sid, sid, "sarvam", "inbound", caller, now())
                    app.store.add_message(sid, "assistant", "📞 Incoming phone call (Sarvam)", now(), USER_ID,
                                          kind="call")
                if not text:
                    return self._json({"say": "Sorry, I didn't catch that. Could you say it again?", "session_id": sid})
                reply = app.agent(sid).handle_user_message(text, channel="phone")
            self._json({"say": speakable(reply), "session_id": sid, "end_call": False})

        def sarvam_webhook(self) -> None:
            """Call-completion event from Sarvam (inbound deployment or instant outbound): write the
            'call ended' / 'not connected' marker into the call's chat, exactly once per call."""
            b = self._body()
            if not self._sarvam_authorized(b):
                return self._json({"error": "unauthorized"}, 401)
            meta = b.get("metadata") or (b.get("webhook_config") or {}).get("metadata") or {}
            ids = [i for i in (b.get("attempt_id"), b.get("interaction_id")) if i]
            with app.lock:
                known = next((c for c in (app.reader.phone_call(i) for i in ids) if c), None)
                sid = (known or {}).get("session_id") or meta.get("session_id")
                if not sid:
                    return self._json({"ok": True, "ignored": "unknown call"})
                if known:
                    fresh = [app.store.end_phone_call(i, now()) for i in ids if app.reader.phone_call(i)]
                    if not any(fresh):
                        return self._json({"ok": True, "duplicate": True})
                status = b.get("status") or "connected"
                secs = int(float(b.get("duration") or 0))
                if status == "connected" and secs:
                    text = f"📞 Phone call ended · {secs // 60}m {secs % 60:02d}s"
                else:
                    why = {"no_answer": "no answer", "busy": "line busy", "failed": "call failed"}.get(status, status)
                    reason = f" — {b['failure_reason']}" if b.get("failure_reason") else ""
                    text = f"📞 Couldn't reach you ({why}{reason}). You can keep chatting here or try the call again."
                app.store.add_message(sid, "assistant", text, now(), USER_ID, kind="call")
            self._json({"ok": True})

        def phone_inbound(self) -> None:
            """An inbound phone call arrived: accept only the user's registered number, open a chat for it."""
            b = self._body()
            caller = (b.get("from") or "").strip()
            mine = app.reader.profile(USER_ID).get("phone")
            if not mine or caller.replace(" ", "") != mine:
                return self._json({"allowed": False})
            sid = new_session_id()
            with app.lock:
                app.store.create_session(sid, USER_ID, now(), title=f"Phone call · {datetime.now():%d %b %H:%M}",
                                         model=app.default_model())
            self._json({"allowed": True, "session_id": sid, "user_name": USER_NAME})

        def voice_token(self) -> None:
            """Token for the browser to join a fresh room; it dispatches the voice worker for this chat."""
            from livekit import api
            sid = self._body().get("session_id")
            if not sid or not re.match(r"^[\w-]+$", sid):
                return self._json({"error": "session_id required"}, 400)
            if not os.environ.get("LIVEKIT_API_KEY"):
                return self._json({"error": "LiveKit is not configured (secrets/livekit.env)"}, 400)
            with app.lock:
                app.store.create_session(sid, USER_ID, now(), model=app.default_model())
            room = f"errand-{sid}-{uuid.uuid4().hex[:6]}"
            token = (api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
                     .with_identity(f"user-{USER_ID}-{uuid.uuid4().hex[:4]}").with_name(USER_NAME)
                     .with_grants(api.VideoGrants(room_join=True, room=room))
                     .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(
                         agent_name="errand-voice", metadata=json.dumps({"session_id": sid}))]))
                     .to_jwt())
            self._json({"url": os.environ["LIVEKIT_URL"], "token": token, "room": room})

        def models(self) -> None:
            ms = app.catalog.models(refresh=self.q.get("refresh") == "1") if app.kind == "litellm" \
                else [m for m in app.catalog.models() if m["id"] == "scripted"]
            self._json({"models": app.catalog.with_status(ms), "default": app.default_model(), "source": app.catalog.source})

        def list_sessions(self) -> None:
            self._json({"sessions": [s for s in app.reader.sessions(USER_ID) if s["n"] or s["title"]]})

        def create_session(self) -> None:
            sid = new_session_id()
            model = self._body().get("model") or app.default_model()
            with app.lock:
                app.store.create_session(sid, USER_ID, now(), model=model)
            self._json({"id": sid, "model": model})

        def messages(self, sid: str) -> None:
            msgs = app.reader.messages(sid)
            since = int(self.q.get("since", 0))
            wf = app.reader.load_workflow(sid)
            phase, missing = None, []
            if wf:
                from .workflow import WorkflowState
                state = WorkflowState.from_dict(wf)
                phase = state.phase
                missing = state.request.missing() if (state.request.intent or state.request.service) else []
            self._json({"messages": msgs[since:], "count": len(msgs), "model": app.session_model(sid),
                        "phase": phase, "missing": missing})

        def chat(self, sid: str) -> None:
            body = self._body()
            text = (body.get("text") or "").strip()
            channel = body.get("channel") if body.get("channel") in ("voice", "phone") else "chat"
            if not text:
                return self._json({"error": "empty message"}, 400)
            with app.lock:
                agent = app.agent(sid)
                agent.last_model_error = None
                reply = agent.handle_user_message(text, channel=channel)
                from .agent import model_error_kind
                if agent.last_model_error and model_error_kind(agent.last_model_error) in ("model_blocked", "model_not_found"):
                    app.catalog.mark_blocked(agent.model_name, "blocked for this key" if "blocked" in
                                             model_error_kind(agent.last_model_error) else "not found")
            self._json({"reply": reply, "count": len(app.reader.messages(sid)), "model": agent.model_name})

        def trace(self, sid: str) -> None:
            ev = app.trace(sid, int(self.q.get("since", 0)))
            self._json({"events": ev})

        def state(self, sid: str) -> None:
            a = app.agents.get(sid)
            self._json({"state": a._state_view() if a else None})

        def rename(self, sid: str) -> None:
            """PATCH a chat: {title} renames it, {model} switches the model for later turns."""
            b = self._body()
            title = (b.get("title") or "").strip()[:80]
            with app.lock:
                if title:
                    app.store.rename_session(sid, title)
                if b.get("model"):
                    app.store.create_session(sid, USER_ID, now())
                    app.store.set_session_model(sid, b["model"])
                    if sid in app.agents:
                        app.agent(sid)  # applies the switch and traces it
            self._json({"ok": True, "model": app.session_model(sid)})

        def delete(self, sid: str) -> None:
            with app.lock:
                app.agents.pop(sid, None)
                app.store.delete_session(sid)
            self._json({"ok": True})

        def memories(self) -> None:
            mems = [m for m in app.reader.memories(USER_ID) if m["status"] == "active"]
            self._json({"memories": [{k: m[k] for k in ("id", "key", "value", "scope", "source", "confirmed",
                                                        "updated_at")} for m in mems]})

        def forget(self, mid: str) -> None:
            from .memory import MemoryManager
            with app.lock:
                gone = MemoryManager(app.store, app.world.clock, USER_ID).forget(mid=mid)
            self._json({"forgotten": [m["id"] for m in gone]})

        def bookings(self) -> None:
            with app.lock:
                rows = app.world.list_bookings(USER_ID)["bookings"]
            t = app.world.clock.now().isoformat()
            self._json({"bookings": [{**b, "upcoming": b["slot"] > t,
                                      "on_calendar": bool(app.reader.calendar_event(b["booking_id"]))} for b in rows]})

        def dev_force(self) -> None:
            b = self._body()
            with app.lock:
                st = app.world.state
                if b.get("book_outcome"):
                    st["forced_book_outcomes"].append(b["book_outcome"])
                if b.get("price_drift"):
                    st["forced_price_drift"].append(int(b["price_drift"]))
                app.world._save()
            self._json({"ok": True})

        def dev_cancel(self) -> None:
            with app.lock:
                t = app.world.clock.now().isoformat()
                up = [x for x in app.world.list_bookings(USER_ID)["bookings"]
                      if x["status"] == "confirmed" and x["slot"] > t]
                bid = up[-1]["booking_id"] if up else None
                if bid:
                    app.world.simulate_provider_cancellation(bid)
            self._json({"cancelled": bid})

        def dev_restart(self) -> None:
            app.restart()
            self._json({"ok": True})

    return H


def start_public_tunnel(port: int) -> None:
    """Start ngrok for this port and set ERRAND_PUBLIC_URL. Logged-in ngrok accounts get a fixed
    domain, so the URL configured in the Sarvam agent's tool keeps working across restarts.
    The router only serves /api/sarvam/* to requests that arrive through the tunnel."""
    import atexit
    import subprocess
    import urllib.request
    if not os.environ.get("ERRAND_SARVAM_TOKEN"):
        raise SystemExit("--public needs ERRAND_SARVAM_TOKEN in .env (it protects the public endpoints)")
    proc = subprocess.Popen(["ngrok", "http", str(port), "--log", "stdout"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    atexit.register(proc.terminate)
    for _ in range(40):
        try:
            tunnels = json.loads(urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2).read())
            url = next(t["public_url"] for t in tunnels["tunnels"] if t["public_url"].startswith("https://"))
            os.environ["ERRAND_PUBLIC_URL"] = url
            print(f"public URL (Sarvam tool + webhooks only): {url}", flush=True)
            return
        except Exception:
            time.sleep(0.5)
    raise SystemExit("ngrok did not start; run `ngrok http %d` manually and set ERRAND_PUBLIC_URL" % port)


def start_voice_worker(port: int) -> None:
    """Run the voice worker as a child process that talks to this server."""
    import atexit
    import subprocess
    import sys
    if not os.environ.get("LIVEKIT_API_KEY"):
        print("voice: LiveKit is not configured (secrets/livekit.env); skipping the voice worker")
        return
    env = {**os.environ, "ERRAND_SERVER_URL": f"http://127.0.0.1:{port}"}
    proc = subprocess.Popen([sys.executable, "-m", "errand_agent.voice", "start"], env=env)
    atexit.register(proc.terminate)
    print(f"voice worker started (pid {proc.pid})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="litellm | scripted | replay:<cassette.jsonl>")
    ap.add_argument("--data", default="run")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--fresh", action="store_true", help="wipe local state and reseed")
    ap.add_argument("--voice", action="store_true", help="also start the LiveKit voice worker")
    ap.add_argument("--public", action="store_true",
                    help="expose ONLY the token-protected /api/sarvam/* endpoints via ngrok (for Sarvam phone calls)")
    a = ap.parse_args()
    if a.public:
        start_public_tunnel(a.port)
    app = App(a.data, a.model, a.fresh)
    if a.voice:
        start_voice_worker(a.port)
    threading.Thread(target=app.poll_events, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(app))
    print(f"Errand ({app.kind}) on http://127.0.0.1:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
