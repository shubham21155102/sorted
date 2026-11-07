"""Local demo server: chat on the left, live trace on the right.

    python -m errand_agent.server [--model live|scripted] [--port 8765] [--fresh]

A background poller calls agent.process_events() every few seconds, so provider
cancellations surface in the chat without any user message.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .app import build

STATIC = os.path.join(os.path.dirname(__file__), "static", "index.html")


class Demo:
    def __init__(self, data: str, model: str | None, fresh: bool):
        self.data, self.model = data, model
        self.lock = threading.Lock()
        self.agent = build(data, model, fresh=fresh)
        self.outbox: list[dict[str, Any]] = [{"role": "agent", "text": m, "proactive": True} for m in self.agent.outbox]

    def restart(self) -> None:
        """Simulate a process restart: rebuild everything from disk (recovery runs on startup)."""
        with self.lock:
            self.agent.store.close()
            self.agent = build(self.data, self.model)
            self.outbox += [{"role": "agent", "text": m, "proactive": True} for m in self.agent.outbox]

    def poll_events(self) -> None:
        while True:
            time.sleep(3)
            try:
                with self.lock:
                    for m in self.agent.process_events():
                        self.outbox.append({"role": "agent", "text": m, "proactive": True})
            except Exception as e:  # never let the poller die silently
                self.outbox.append({"role": "system", "text": f"event poller error: {e}"})


def make_handler(demo: Demo):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def _json(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                with open(STATIC, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/api/poll"):
                q = dict(p.split("=") for p in self.path.split("?", 1)[1].split("&")) if "?" in self.path else {}
                t, o = int(q.get("trace", 0)), int(q.get("outbox", 0))
                with demo.lock:
                    ev = demo.agent.tracer.events
                    snap = demo.agent.snapshot()
                    self._json({"trace": ev[t:], "trace_len": len(ev), "outbox": demo.outbox[o:],
                                "outbox_len": len(demo.outbox), "state": snap["state"],
                                "memories": snap["memories"], "model": demo.agent.model_name,
                                "bookings": demo.agent.world.list_bookings(demo.agent.user_id)["bookings"]})
                return
            self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            b = self._body()
            if self.path == "/api/chat":
                with demo.lock:
                    reply = demo.agent.handle_user_message(b.get("text", ""))
                self._json({"reply": reply})
            elif self.path == "/api/provider_cancel":
                with demo.lock:
                    up = [x for x in demo.agent.world.list_bookings(demo.agent.user_id)["bookings"]
                          if x["status"] == "confirmed" and x["slot"] > demo.agent.clock.now().isoformat()]
                    bid = b.get("booking_id") or (up[-1]["booking_id"] if up else None)
                    if bid:
                        demo.agent.world.simulate_provider_cancellation(bid)
                self._json({"cancelled": bid})
            elif self.path == "/api/force":
                with demo.lock:
                    st = demo.agent.world.state
                    if b.get("book_outcome"):
                        st["forced_book_outcomes"].append(b["book_outcome"])
                    if b.get("price_drift"):
                        st["forced_price_drift"].append(int(b["price_drift"]))
                    demo.agent.world._save()
                self._json({"ok": True})
            elif self.path == "/api/restart":
                demo.restart()
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)

    return H


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--data", default="run")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--fresh", action="store_true")
    a = ap.parse_args()
    demo = Demo(a.data, a.model, a.fresh)
    threading.Thread(target=demo.poll_events, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(demo))
    print(f"Errand demo ({demo.agent.model_name}) on http://127.0.0.1:{a.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
