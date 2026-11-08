"""Run tracing. One JSONL line per event; one `run_id` per user turn / system event.

Events: run_start, state_transition, context, model_call, tool_call, approval,
memory, limit, recovery, proactive, run_end. Tool results are sanitized before
being written (phone numbers redacted, long strings truncated)."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from typing import Any

from .clock import Clock

# International-prefixed numbers or bare 10-digit mobiles. (An earlier, looser pattern also
# matched ISO dates like 2026-11-09 and corrupted every slot in the trace — see docs/BUGS.md.)
PHONE_RE = re.compile(r"\+\d{1,3}[-\s]?\d{2}[\d\-\s]{6,}\d|\b\d{10}\b")

# Fallback USD per million tokens (input, output) when the adapter doesn't report a cost.
# LiteLLM reports real per-call cost via litellm.completion_cost, which takes precedence.
PRICING = {
    "scripted": (0.0, 0.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICING.get(model, (0.0, 0.0))
    return round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)


def sanitize(obj: Any, max_str: int = 300) -> Any:
    if isinstance(obj, dict):
        return {k: ("[redacted]" if k in ("phone", "idempotency_key_secret") else sanitize(v, max_str))
                for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) > 12:
            return [sanitize(v, max_str) for v in obj[:12]] + [f"... {len(obj) - 12} more"]
        return [sanitize(v, max_str) for v in obj]
    if isinstance(obj, str):
        s = PHONE_RE.sub("[phone]", obj)
        return s if len(s) <= max_str else s[:max_str] + f"...[+{len(s) - max_str} chars]"
    return obj


class Tracer:
    def __init__(self, path: str | None, clock: Clock):
        self.path = path
        self.clock = clock
        self.events: list[dict[str, Any]] = []
        self.run_id: str | None = None
        self._lock = threading.Lock()
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def start_run(self, kind: str, **kw: Any) -> str:
        self.run_id = f"run-{uuid.uuid4().hex[:8]}"
        self.emit("run_start", kind=kind, **kw)
        return self.run_id

    def emit(self, event: str, **kw: Any) -> dict[str, Any]:
        rec = {"ts": self.clock.now().isoformat(), "run_id": self.run_id, "event": event, **kw}
        with self._lock:
            self.events.append(rec)
            if self.path:
                with open(self.path, "a") as f:
                    f.write(json.dumps(rec, default=str) + "\n")
        return rec

    def run_events(self, run_id: str | None = None) -> list[dict[str, Any]]:
        rid = run_id or self.run_id
        return [e for e in self.events if e["run_id"] == rid]

    def of_type(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]
