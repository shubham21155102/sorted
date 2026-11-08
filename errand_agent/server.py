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
                                              self.trace_path)
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


def now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def new_session_id() -> str:
    return "c-" + uuid.uuid4().hex[:10]


ROUTES: list[tuple[str, str, str]] = [
    ("GET", r"^/$", "index"),
    ("GET", r"^/api/bootstrap$", "bootstrap"),
    ("GET", r"^/api/models$", "models"),
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

        def _route(self, method: str) -> None:
            url = urlparse(self.path)
            self.q = {k: v[0] for k, v in parse_qs(url.query).items()}
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

        # -------------------------------------------------------- handlers
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
            self._json({"messages": msgs[since:], "count": len(msgs), "model": app.session_model(sid)})

        def chat(self, sid: str) -> None:
            text = (self._body().get("text") or "").strip()
            if not text:
                return self._json({"error": "empty message"}, 400)
            with app.lock:
                agent = app.agent(sid)
                agent.last_model_error = None
                reply = agent.handle_user_message(text)
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
            self._json({"bookings": [{**b, "upcoming": b["slot"] > t} for b in rows]})

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="litellm | scripted | replay:<cassette.jsonl>")
    ap.add_argument("--data", default="run")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--fresh", action="store_true", help="wipe local state and reseed")
    a = ap.parse_args()
    app = App(a.data, a.model, a.fresh)
    threading.Thread(target=app.poll_events, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(app))
    print(f"Errand ({app.kind}) on http://127.0.0.1:{a.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
