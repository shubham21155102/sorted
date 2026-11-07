"""Side-effect execution with idempotency and recovery.

The idempotency key is minted when the proposal is created and persisted before
the first network call. The proposal row doubles as a write-ahead ledger:
  pending -> approved -> executing -> executed | failed
A crash while `executing` is resolved on the next start by asking
get_booking_status (the source of truth) — never by blindly re-booking.
"""
from __future__ import annotations

from typing import Any, Callable

from .clock import Clock
from .store import Store

TRANSIENT = {"timeout", "internal_error", "service_unavailable", "malformed_response", "exception"}


class SimulatedCrash(BaseException):
    """Raised by test fault hooks to simulate the process dying mid-operation.
    BaseException so ordinary `except Exception` handlers cannot swallow it."""


class Executor:
    def __init__(self, call_tool: Callable[[str, dict[str, Any]], Any], store: Store, clock: Clock, emit,
                 max_attempts: int = 3, backoff_s: tuple[float, ...] = (0.5, 1.0, 2.0),
                 fault_hook: Callable[[str], None] | None = None,
                 deadline_check: Callable[[], bool] | None = None):
        self.call_tool = call_tool
        self.store = store
        self.clock = clock
        self.emit = emit
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self.fault_hook = fault_hook or (lambda stage: None)
        self.deadline_check = deadline_check or (lambda: False)

    # ------------------------------------------------------------- helpers
    def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self.call_tool(name, args)
        except SimulatedCrash:
            raise
        except Exception as e:  # unexpected tool failure -> normalized error
            return {"error": "exception", "detail": f"{type(e).__name__}: {e}"}
        if not isinstance(r, dict):
            return {"error": "malformed_response", "detail": str(r)[:120]}
        return r

    def _status(self, key: str, hold_id: str | None = None) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for i in range(2):
            last = self._call("get_booking_status", {"idempotency_key": key} if not hold_id
                              else {"idempotency_key": key, "hold_id": hold_id})
            if last.get("status") in ("applied", "not_found"):
                return last
            self.clock.sleep(self.backoff_s[min(i, len(self.backoff_s) - 1)])
        return {"status": "unknown", "last": last}

    def _finish(self, p: dict[str, Any], status: str, result: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
        self.store.update_proposal(p["id"], status=status, result=result, status_reason=reason)
        self.emit("execution", proposal_id=p["id"], kind=p["action"], status=status, result=result, reason=reason)
        return {"proposal_id": p["id"], "status": status, **result, **({"reason": reason} if reason else {})}

    def _backoff(self, attempt: int) -> None:
        self.clock.sleep(self.backoff_s[min(attempt - 1, len(self.backoff_s) - 1)])

    # ---------------------------------------------------------------- book
    def book(self, p: dict[str, Any], hold_id: str) -> dict[str, Any]:
        if p["status"] == "executed":
            return {"proposal_id": p["id"], "status": "executed", **(p["result"] or {}), "replayed": True}
        if p["status"] == "executing":  # e.g. a second confirmation racing the first: resolve, don't re-send
            return self.resolve_in_flight(p)
        key = p["idempotency_key"]
        self.store.update_proposal(p["id"], status="executing", result={"hold_id": hold_id})
        self.emit("execution", proposal_id=p["id"], kind="book", status="executing", idempotency_key=key)
        for attempt in range(1, self.max_attempts + 1):
            r = self._call("book", {"hold_id": hold_id, "idempotency_key": key})
            self.fault_hook("after_book_call")
            err = r.get("error")
            if r.get("status") == "confirmed":
                st = self._status(key)
                verified_by = "get_booking_status" if st.get("status") == "applied" else "book_response"
                return self._finish(p, "executed", {"booking_id": r["booking_id"], "slot": r["slot"],
                                                    "price": r["price"], "provider_id": r["provider_id"],
                                                    "verified_by": verified_by, "attempts": attempt})
            if err in TRANSIENT:
                self.emit("recovery", kind="book", attempt=attempt, error=err,
                          action="query get_booking_status before deciding")
                st = self._status(key)
                if st.get("status") == "applied":
                    return self._finish(p, "executed", {"booking_id": st["booking_id"], "slot": st["slot"],
                                                        "price": st["price"], "provider_id": st["provider_id"],
                                                        "verified_by": "get_booking_status",
                                                        "attempts": attempt, "note": f"{err} after success"})
                if attempt < self.max_attempts and not self.deadline_check():
                    self.emit("recovery", kind="book", attempt=attempt,
                              action="not booked per source of truth; retrying with same idempotency key")
                    self._backoff(attempt)
                    continue
                break
            if err == "slot_taken":
                return self._finish(p, "failed", {"error": "slot_taken"}, "slot was taken by someone else")
            if err in ("hold_expired", "hold_not_active", "hold_not_found"):
                return self._finish(p, "failed", {"error": err}, "hold no longer valid; a new hold and fresh approval are needed")
            return self._finish(p, "failed", {"error": err or "unknown", "detail": r}, "unexpected provider response")
        st = self._status(key)
        if st.get("status") == "applied":
            return self._finish(p, "executed", {"booking_id": st["booking_id"], "slot": st["slot"],
                                                "price": st["price"], "provider_id": st["provider_id"],
                                                "verified_by": "get_booking_status"})
        if st.get("status") == "unknown":
            # Cannot tell. Leave the ledger in `executing` so recovery resolves it later; tell the user honestly.
            self.emit("execution", proposal_id=p["id"], kind="book", status="unknown")
            return {"proposal_id": p["id"], "status": "unknown",
                    "reason": "provider unreachable; booking outcome not yet confirmed"}
        return self._finish(p, "failed", {"error": "retries_exhausted"},
                            f"not booked after {self.max_attempts} attempts (verified with get_booking_status)")

    # -------------------------------------------------------------- cancel
    def cancel(self, p: dict[str, Any]) -> dict[str, Any]:
        if p["status"] == "executed":
            return {"proposal_id": p["id"], "status": "executed", **(p["result"] or {}), "replayed": True}
        key, bid = p["idempotency_key"], p["material"]["booking_id"]
        self.store.update_proposal(p["id"], status="executing")
        for attempt in range(1, self.max_attempts + 1):
            r = self._call("cancel", {"booking_id": bid, "idempotency_key": key})
            if r.get("status") == "cancelled":
                return self._finish(p, "executed", {"booking_id": bid, "booking_status": "cancelled",
                                                    "verified_by": "cancel_response", "attempts": attempt})
            err = r.get("error")
            if err in TRANSIENT or r.get("retryable"):
                self.emit("recovery", kind="cancel", attempt=attempt, error=err, action="retry with same key")
                st = self._status(key)
                if st.get("status") == "applied" and st.get("booking_status") == "cancelled":
                    return self._finish(p, "executed", {"booking_id": bid, "booking_status": "cancelled",
                                                        "verified_by": "get_booking_status"})
                if attempt < self.max_attempts and not self.deadline_check():
                    self._backoff(attempt)
                    continue
                break
            return self._finish(p, "failed", {"error": err, "detail": r}, "provider refused cancellation")
        return self._finish(p, "failed", {"error": "retries_exhausted"}, "cancellation could not be completed")

    # ---------------------------------------------------------- reschedule
    def reschedule(self, p: dict[str, Any]) -> dict[str, Any]:
        if p["status"] == "executed":
            return {"proposal_id": p["id"], "status": "executed", **(p["result"] or {}), "replayed": True}
        key, bid, slot = p["idempotency_key"], p["material"]["booking_id"], p["material"]["new_slot"]
        self.store.update_proposal(p["id"], status="executing")
        for attempt in range(1, self.max_attempts + 1):
            r = self._call("reschedule", {"booking_id": bid, "slot": slot, "idempotency_key": key})
            if r.get("status") == "rescheduled":
                return self._finish(p, "executed", {"booking_id": bid, "slot": r["slot"],
                                                    "verified_by": "reschedule_response", "attempts": attempt})
            err = r.get("error")
            if err == "slot_conflict":
                return self._finish(p, "failed", {"error": "slot_conflict", "slot": slot},
                                    "requested slot conflicts; choose another slot (needs fresh approval)")
            if err in TRANSIENT:
                st = self._status(key)
                if st.get("status") == "applied":
                    return self._finish(p, "executed", {"booking_id": bid, "slot": st["slot"],
                                                        "verified_by": "get_booking_status"})
                if attempt < self.max_attempts and not self.deadline_check():
                    self._backoff(attempt)
                    continue
                break
            return self._finish(p, "failed", {"error": err, "detail": r}, "unexpected provider response")
        return self._finish(p, "failed", {"error": "retries_exhausted"}, "reschedule could not be completed")

    # ------------------------------------------------------------ recovery
    def resolve_in_flight(self, p: dict[str, Any]) -> dict[str, Any]:
        """Resolve a proposal left in `executing` (crash / unknown outcome) from the source of truth."""
        hold_id = (p.get("result") or {}).get("hold_id")
        st = self._status(p["idempotency_key"], hold_id if p["action"] == "book" else None)
        self.emit("recovery", kind="in_flight", proposal_id=p["id"], status=st.get("status"))
        if st.get("status") == "applied":
            res = {"booking_id": st["booking_id"], "slot": st.get("slot"), "price": st.get("price"),
                   "provider_id": st.get("provider_id"), "booking_status": st.get("booking_status"),
                   "verified_by": "get_booking_status", "recovered": True}
            return self._finish(p, "executed", res)
        if st.get("status") == "not_found":
            return self._finish(p, "failed", {"error": "not_applied", "recovered": True},
                                "interrupted before the provider applied it; nothing was booked")
        return {"proposal_id": p["id"], "status": "unknown", "reason": "source of truth unreachable"}
