"""Explicit workflow state.

The errand is a small state machine. The model never sets the phase directly:
phases change only as a consequence of code-observed events (request updated,
search returned, proposal created, approval granted, side effect verified), and
every transition is checked against ALLOWED and traced.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PHASES = ["idle", "gathering", "searching", "choosing", "awaiting_approval", "executing", "completed", "failed"]

ALLOWED: dict[str, set[str]] = {
    "idle": {"gathering", "searching", "choosing", "awaiting_approval"},
    "gathering": {"gathering", "searching", "choosing", "awaiting_approval", "idle"},
    "searching": {"gathering", "choosing", "awaiting_approval", "failed", "idle"},
    "choosing": {"gathering", "searching", "choosing", "awaiting_approval", "idle"},
    "awaiting_approval": {"gathering", "searching", "choosing", "awaiting_approval", "executing", "idle"},
    "executing": {"completed", "failed", "choosing", "awaiting_approval"},
    "completed": {"idle", "gathering", "searching", "choosing", "awaiting_approval"},
    "failed": {"idle", "gathering", "searching", "choosing", "awaiting_approval"},
}

REQUEST_FIELDS = ["intent", "service", "area", "date_start", "date_end", "earliest_time", "latest_time",
                  "budget_max", "provider_preference", "booking_id", "target_slot", "notes"]

INTENTS = {"book", "reschedule", "cancel", "rebook_after_provider_cancel", "memory", "info"}


@dataclass
class ErrandRequest:
    intent: str | None = None
    service: str | None = None
    area: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    earliest_time: str | None = None
    latest_time: str | None = None
    budget_max: int | None = None
    budget_declined: bool = False
    provider_preference: str | None = None
    booking_id: str | None = None
    target_slot: str | None = None
    notes: str | None = None
    provenance: dict[str, str] = field(default_factory=dict)  # field -> "user" | "memory" | "event"

    def missing(self) -> list[str]:
        intent = self.intent or "book"
        if intent in ("book", "rebook_after_provider_cancel"):
            need = []
            if not self.service:
                need.append("service")
            if not self.area:
                need.append("area")
            if not self.date_start:
                need.append("date window")
            if self.budget_max is None and not self.budget_declined:
                need.append("budget")
            return need
        if intent in ("reschedule",):
            need = []
            if not (self.booking_id or self.service):
                need.append("which booking")
            if not (self.target_slot or self.date_start):
                need.append("new date/time")
            return need
        if intent == "cancel":
            return [] if (self.booking_id or self.service) else ["which booking"]
        return []


@dataclass
class WorkflowState:
    phase: str = "idle"
    request: ErrandRequest = field(default_factory=ErrandRequest)
    last_search: list[dict[str, Any]] = field(default_factory=list)  # compact candidates
    holds: dict[str, dict[str, Any]] = field(default_factory=dict)  # hold_id -> hold record (from tool)
    active_proposal_id: str | None = None
    last_outcome: dict[str, Any] | None = None
    history: list[dict[str, str]] = field(default_factory=list)  # transitions

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkflowState":
        d = dict(d)
        d["request"] = ErrandRequest(**d.get("request", {}))
        return cls(**d)


class InvalidTransition(Exception):
    pass


def transition(state: WorkflowState, to: str, reason: str, emit) -> None:
    frm = state.phase
    if frm == to:
        return
    if to not in ALLOWED.get(frm, set()):
        emit("state_transition_rejected", frm=frm, to=to, reason=reason)
        raise InvalidTransition(f"{frm} -> {to} ({reason})")
    state.phase = to
    state.history.append({"from": frm, "to": to, "reason": reason})
    emit("state_transition", frm=frm, to=to, reason=reason)


def apply_request_update(req: ErrandRequest, updates: dict[str, Any], source: str = "user") -> list[str]:
    """Merge only the provided fields; everything else is preserved. Returns changed fields."""
    changed = []
    if updates.get("new_errand"):
        fresh = ErrandRequest()
        for f in REQUEST_FIELDS:
            setattr(req, f, getattr(fresh, f))
        req.budget_declined = False
        req.provenance = {}
        changed.append("*reset*")
    for k, v in updates.items():
        if k in ("new_errand",):
            continue
        if k == "budget_max" and v in ("none", "no budget", 0, "0"):
            req.budget_max = None
            req.budget_declined = True
            changed.append(k)
            continue
        if k not in REQUEST_FIELDS or v in (None, ""):
            continue
        if k == "budget_max":
            try:
                v = int(str(v).replace(",", "").replace("₹", "").strip())
            except ValueError:
                continue
        if k == "service":
            v = str(v).strip().lower().replace(" ", "_")
        if k == "area":
            v = str(v).strip().lower()
        if getattr(req, k) != v:
            setattr(req, k, v)
            req.provenance[k] = source
            changed.append(k)
    return changed
