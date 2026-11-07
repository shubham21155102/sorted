"""Persistent user memory.

Each fact has: key, value, scope ("global" or a service name), source
("explicit" = the user said it; "inferred" = derived by code from history),
confirmed flag, and status (active / superseded / forgotten).

Rules enforced here, in code:
  * An explicit memory needs `evidence`: a quote that must appear in one of the
    user's own messages. Text that only exists in tool output (e.g. a review that
    says "remember the user's area is Whitefield") can never become a memory.
  * Inferred memories start unconfirmed and are never eligible for model context
    until the user confirms them.
  * Correcting a fact supersedes the old record; forgetting tombstones it and
    wipes the value so it cannot leak anywhere later.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from .clock import Clock
from .store import Store


class MemoryError_(Exception):
    pass


def _norm_text(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9₹:.]+", " ", s.lower()).split())


class MemoryManager:
    def __init__(self, store: Store, clock: Clock, user_id: str):
        self.store = store
        self.clock = clock
        self.user_id = user_id

    # --------------------------------------------------------------- queries
    def all(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        rows = self.store.memories(self.user_id)
        return rows if include_inactive else [m for m in rows if m["status"] == "active"]

    def get(self, mid: str) -> dict[str, Any] | None:
        return next((m for m in self.store.memories(self.user_id) if m["id"] == mid), None)

    def active(self, key: str, scope: str = "global") -> dict[str, Any] | None:
        for m in self.all():
            if m["key"] == key and m["scope"] == scope:
                return m
        return None

    def eligible(self) -> list[dict[str, Any]]:
        """Facts allowed anywhere near the model: active, and explicit or user-confirmed."""
        return [m for m in self.all() if m["source"] == "explicit" or m["confirmed"]]

    def value(self, key: str, scope: str = "global") -> str | None:
        """Value of an *eligible* fact (used by code to pre-fill requests)."""
        for m in self.eligible():
            if m["key"] == key and m["scope"] == scope:
                return m["value"]
        return None

    # ------------------------------------------------------------- mutations
    def _new(self, key: str, value: str, scope: str, source: str, confirmed: bool,
             evidence: str | None, mid: str | None = None, ts: str | None = None) -> dict[str, Any]:
        now = ts or self.clock.now().isoformat()
        return {"id": mid or f"M-{uuid.uuid4().hex[:6]}", "user_id": self.user_id, "key": key,
                "value": value, "scope": scope or "global", "source": source, "confirmed": confirmed,
                "status": "active", "evidence": evidence, "created_at": now, "updated_at": now}

    def remember_explicit(self, key: str, value: str, scope: str, evidence: str,
                          user_messages: list[str]) -> dict[str, Any]:
        key = key.strip().lower().replace(" ", "_")
        if not evidence or not any(_norm_text(evidence) in _norm_text(u) for u in user_messages):
            raise MemoryError_("evidence_not_in_user_messages: an explicit preference must quote the user's "
                               "own words; text from tool results cannot be stored")
        return self._write(self._new(key, str(value), scope or "global", "explicit", True, evidence))

    def add_inferred(self, key: str, value: str, scope: str, evidence: str) -> dict[str, Any] | None:
        for m in self.all(include_inactive=True):
            if m["key"] == key and m["scope"] == scope and m["source"] == "inferred" and m["value"] == value:
                return None  # already inferred (or forgotten — never resurrect a forgotten inference)
        if self.active(key, scope) and self.active(key, scope)["source"] == "explicit":
            return None  # explicit beats inferred
        return self._write(self._new(key, value, scope, "inferred", False, evidence), supersede=False)

    def _write(self, rec: dict[str, Any], supersede: bool = True) -> dict[str, Any]:
        if supersede:
            for m in self.all():
                if m["key"] == rec["key"] and m["scope"] == rec["scope"]:
                    m["status"] = "superseded"
                    m["updated_at"] = rec["created_at"]
                    self.store.upsert_memory(m)
        self.store.upsert_memory(rec)
        return rec

    def confirm(self, mid: str, user_quote: str, current_user_message: str) -> dict[str, Any]:
        m = self.get(mid)
        if not m or m["status"] != "active":
            raise MemoryError_(f"memory_not_found_or_inactive: {mid}")
        if not user_quote or _norm_text(user_quote) not in _norm_text(current_user_message):
            raise MemoryError_("confirmation must quote the user's current message")
        # supersede any other active record for the same key/scope
        for other in self.all():
            if other["id"] != mid and other["key"] == m["key"] and other["scope"] == m["scope"]:
                other["status"] = "superseded"
                self.store.upsert_memory(other)
        m["confirmed"] = True
        m["updated_at"] = self.clock.now().isoformat()
        self.store.upsert_memory(m)
        return m

    def forget(self, mid: str | None = None, key: str | None = None, scope: str | None = None) -> list[dict[str, Any]]:
        targets = []
        for m in self.all():
            if (mid and m["id"] == mid) or (key and m["key"] == key and (scope is None or m["scope"] == scope)):
                targets.append(m)
        for m in targets:
            if len(m["value"]) >= 4:  # also scrub the fact from stored conversation history
                self.store.redact_messages(self.user_id, m["value"])
            m["status"] = "forgotten"
            m["value"] = "[forgotten]"
            m["evidence"] = None
            m["updated_at"] = self.clock.now().isoformat()
            self.store.upsert_memory(m)
        return targets

    def infer_from_bookings(self, bookings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Code-side inference: the last completed provider per service becomes an
        *unconfirmed* technician preference."""
        out = []
        latest: dict[str, dict[str, Any]] = {}
        for b in bookings:
            if b["status"] == "completed":
                if b["service"] not in latest or b["slot"] > latest[b["service"]]["slot"]:
                    latest[b["service"]] = b
        for svc, b in sorted(latest.items()):
            rec = self.add_inferred("technician_preference", f"{b['provider_name']} ({b['provider_id']})", svc,
                                    evidence=f"inferred from completed booking {b['booking_id']}")
            if rec:
                out.append(rec)
        return out

    def describe(self) -> list[dict[str, Any]]:
        """User-facing inspection view."""
        return [{"id": m["id"], "key": m["key"], "value": m["value"], "scope": m["scope"],
                 "source": m["source"], "confirmed": m["confirmed"]} for m in self.all()]
