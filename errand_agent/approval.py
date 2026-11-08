"""Approval enforcement — in application code.

Flow:
  1. The model calls `request_approval`. Code (not the model) reads the *material*
     facts from its own records (the hold returned by the provider, or the booking
     from list_bookings), stores a Proposal with a fresh idempotency key, and renders
     the confirmation text itself. The user always sees exactly what will happen.
  2. The next user message is classified by a deterministic parser. Only a bare,
     unambiguous affirmative ("yes", "confirm", "yes, book it", or the proposal code)
     sent in reply to the turn that presented the proposal counts. Anything with a
     negation, a modification ("yes but at 11"), numbers, or extra content does not.
     The model's output is never consulted.
  3. Side-effecting tools (book / cancel / reschedule) are wrapped: they run only if
     an approved proposal exists whose material fields equal the *current* facts.
     Any change of provider, slot or price invalidates the approval.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from .clock import Clock
from .store import Store

STRONG_AFFIRM = {"yes", "yeah", "yep", "yup", "ok", "okay", "sure", "confirm", "confirmed", "approve",
                 "approved", "proceed", "go",
                 # Hindi / Hinglish (romanised, as speech-to-text and typing produce it)
                 "haan", "haa", "han", "haanji", "hanji", "theek", "thik", "thick", "bilkul", "chalega"}
FILLER = {"ahead", "book", "it", "do", "please", "that", "sounds", "good", "great", "thanks", "thank", "you",
          "cancel", "reschedule", "move", "the", "booking", "this", "one", "perfect", "fine", "lets", "let's",
          "s", "go", "and", "is", "correct", "right",
          # common spoken confirmations (phone calls): "yes, finalize it", "yes lock it in", "yes do it now"
          "finalize", "finalise", "finalized", "finalised", "lock", "in", "now", "please", "go", "ahead", "done",
          "ji", "hai", "kar", "karo", "kardo", "karna", "dijiye", "kijiye", "do", "de", "dena", "shukriya", "dhanyavaad"}
NEGATION = {"no", "nope", "nah", "not", "dont", "don't", "never", "stop", "wait", "hold", "change",
            "instead", "but", "actually", "different", "other", "later", "cheaper", "rather",
            "nahi", "nahin", "mat", "ruko", "rukiye", "lekin", "magar", "badlo", "baad"}
REJECT_START = {"no", "nope", "nah", "dont", "don't", "stop", "never", "nahi", "nahin", "mat", "na"}

MATERIAL_FIELDS = {
    "book": ("provider_id", "slot", "price"),
    "cancel": ("booking_id",),
    "reschedule": ("booking_id", "new_slot"),
}


class ApprovalRequired(Exception):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def classify_confirmation(text: str, proposal_code: str | None = None) -> str:
    """Return 'approve', 'reject' or 'none'. Pure function, no model involved."""
    toks = _tokens(text)
    if not toks:
        return "none"
    code = (proposal_code or "").lower()
    if toks[0] in REJECT_START:
        return "reject"
    if any(t in NEGATION for t in toks):
        return "none"
    if len(toks) > 8:
        return "none"
    content = [t for t in toks if t != code]
    if any(t.isdigit() or re.search(r"\d", t) for t in content):
        return "none"
    if code and code in toks and all(t in STRONG_AFFIRM | FILLER for t in content):
        return "approve"
    if any(t in STRONG_AFFIRM for t in content) and all(t in STRONG_AFFIRM | FILLER for t in content):
        return "approve"
    return "none"


def _material_of(action: str, facts: dict[str, Any]) -> dict[str, Any]:
    return {k: facts.get(k) for k in MATERIAL_FIELDS[action]}


class ApprovalGate:
    def __init__(self, store: Store, clock: Clock, session_id: str, user_id: str, emit):
        self.store = store
        self.clock = clock
        self.session_id = session_id
        self.user_id = user_id
        self.emit = emit

    # ------------------------------------------------------------ proposals
    def open_proposals(self) -> list[dict[str, Any]]:
        return [p for p in self.store.proposals(session_id=self.session_id)
                if p["status"] in ("pending", "approved")]

    def create(self, action: str, facts: dict[str, Any], display: dict[str, Any],
               presented_turn: int) -> dict[str, Any]:
        for p in self.open_proposals():
            self.store.update_proposal(p["id"], status="superseded", status_reason="new proposal created")
            self.emit("approval", action="superseded", proposal_id=p["id"])
        pid = "P" + uuid.uuid4().hex[:5].upper()
        p = {"id": pid, "session_id": self.session_id, "user_id": self.user_id, "action": action,
             "material": _material_of(action, facts), "display": display, "status": "pending",
             "idempotency_key": f"idem-{action}-{uuid.uuid4().hex}", "presented_turn": presented_turn,
             "created_at": self.clock.now().isoformat()}
        self.store.insert_proposal(p)
        self.emit("approval", action="proposed", proposal_id=pid, kind=action, material=p["material"])
        return p

    @staticmethod
    def render(p: dict[str, Any]) -> str:
        d = p["display"]
        if p["action"] == "book":
            body = (f"Book **{d['provider_name']}** ({p['material']['provider_id']}) for {d['service']} "
                    f"on **{d['slot_human']}** at **₹{p['material']['price']}**"
                    + (f" (search price was ₹{d['search_price']})" if d.get("search_price") not in (None, p['material']['price']) else "")
                    + f". Slot held until {d['hold_expires_human']}."
                    + (" It will be added to your Google Calendar." if d.get("calendar_note") else ""))
        elif p["action"] == "cancel":
            body = (f"Cancel booking **{p['material']['booking_id']}** with {d['provider_name']} "
                    f"({d['service']}) on {d['slot_human']}.")
        else:
            body = (f"Move booking **{p['material']['booking_id']}** with {d['provider_name']} ({d['service']}) "
                    f"from {d['old_slot_human']} to **{d['new_slot_human']}**.")
        return (f"🔒 Approval needed [{p['id']}]: {body}\n"
                f"Reply **yes** (or `confirm {p['id']}`) to proceed, or tell me what to change.")

    # -------------------------------------------------------- user decisions
    def on_user_message(self, text: str, current_turn: int) -> dict[str, Any]:
        pending = [p for p in self.open_proposals() if p["status"] == "pending"]
        if not pending:
            verdict = classify_confirmation(text)
            if verdict == "approve":
                done = [p for p in self.store.proposals(session_id=self.session_id)
                        if p["status"] in ("executed", "approved", "executing")]
                self.emit("approval", action="confirmation_without_pending", already=[p["id"] for p in done])
                return {"decision": "no_pending", "proposal": done[-1] if done else None}
            return {"decision": "none", "proposal": None}
        p = pending[-1]
        verdict = classify_confirmation(text, p["id"])
        if verdict == "approve":
            if p["presented_turn"] != current_turn:
                self.emit("approval", action="stale_confirmation_ignored", proposal_id=p["id"],
                          presented_turn=p["presented_turn"], current_turn=current_turn)
                return {"decision": "stale", "proposal": p}
            self.store.update_proposal(p["id"], status="approved", approved_at=self.clock.now().isoformat())
            p = self.store.get_proposal(p["id"])
            self.emit("approval", action="approved", proposal_id=p["id"], user_text=text)
            return {"decision": "approved", "proposal": p}
        if verdict == "reject":
            self.store.update_proposal(p["id"], status="rejected", status_reason="user declined")
            self.emit("approval", action="rejected", proposal_id=p["id"], user_text=text)
            return {"decision": "rejected", "proposal": self.store.get_proposal(p["id"])}
        return {"decision": "none", "proposal": p}

    # ---------------------------------------------------------- enforcement
    def authorize(self, action: str, current_facts: dict[str, Any]) -> dict[str, Any]:
        """Return the approved proposal that covers this exact action, or raise."""
        current = _material_of(action, current_facts)
        props = self.store.proposals(session_id=self.session_id)
        # Replays of an already executed/in-flight action are answered from the ledger, never re-run.
        for p in reversed(props):
            if p["action"] == action and p["material"] == current and p["status"] in ("executed", "executing"):
                return p
        approved = [p for p in props if p["action"] == action and p["status"] == "approved"]
        if not approved:
            pending = [p for p in props if p["action"] == action and p["status"] == "pending"]
            reason = "approval_pending_user_has_not_confirmed" if pending else "no_approved_proposal"
            self.emit("approval", action="blocked", kind=action, reason=reason, facts=current)
            raise ApprovalRequired(reason, {"facts": current})
        p = approved[-1]
        if p["material"] != current:
            diff = {k: {"approved": p["material"][k], "now": current[k]}
                    for k in p["material"] if p["material"][k] != current[k]}
            self.store.update_proposal(p["id"], status="invalidated",
                                       status_reason=f"material change: {sorted(diff)}")
            self.emit("approval", action="invalidated", proposal_id=p["id"], diff=diff)
            raise ApprovalRequired("material_change_invalidated_approval", {"diff": diff})
        return p
