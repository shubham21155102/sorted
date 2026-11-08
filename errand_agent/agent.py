"""The orchestrator.

One `ErrandAgent` per (user, session). Each user message (or simulated world
event) is a *run*: a bounded model<->tool loop whose every step is traced.

Separation of concerns:
  * the model interprets language and chooses read-only tools / proposals;
  * code owns the workflow phase, approvals, idempotency keys, side-effect
    execution & verification, memory gating, limits, and the final status lines
    the user sees for anything consequential.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .approval import ApprovalGate, ApprovalRequired
from .clock import Clock, SystemClock
from .context import ContextBuilder, ContextOverflow
from .executor import Executor, SimulatedCrash
from .llm import ModelResponse, estimate_tokens
from .memory import MemoryError_, MemoryManager
from .mock_env import MockWorld
from .prompts import SYSTEM_RULES
from .sanitize import sanitize_tool_result
from .store import Store
from .tools import TOOLS
from .tracing import Tracer, estimate_cost, sanitize
from .workflow import ErrandRequest, InvalidTransition, WorkflowState, apply_request_update, transition

CLAIM_RE = re.compile(
    r"(\b(is|are|has been|have been|now|successfully|all)\s+(booked|confirmed|cancell?ed|rescheduled|moved)\b"
    r"|\bbooking (is )?confirmed\b|\byou'?re (all )?booked\b|\bi(?:'ve| have) (booked|cancell?ed|rescheduled|moved)\b)",
    re.IGNORECASE)
FORGET_INTENT_RE = re.compile(r"\b(forget|delete|remove|erase|clear|wipe)\b", re.IGNORECASE)


@dataclass
class Limits:
    max_model_calls: int = 8
    max_tool_calls: int = 20
    max_wall_seconds: float = 120.0
    max_consecutive_tool_errors: int = 4


class LimitExceeded(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def human_slot(slot: str) -> str:
    try:
        dt = datetime.fromisoformat(slot)
    except (TypeError, ValueError):
        return str(slot)
    return dt.strftime("%a %d %b, %H:%M")


class ErrandAgent:
    def __init__(self, *, llm: Any, world: MockWorld, store: Store, user_id: str = "u1", session_id: str = "s1",
                 clock: Clock | None = None, trace_path: str | None = None, limits: Limits | None = None,
                 context_budget: int = 8000, fault_hook: Callable[[str], None] | None = None,
                 model_name: str | None = None):
        self.llm = llm
        self.world = world
        self.store = store
        self.user_id = user_id
        self.session_id = session_id
        self.clock = clock or SystemClock()
        self.limits = limits or Limits()
        self.model_name = model_name or getattr(llm, "model", "scripted")
        self.tracer = Tracer(trace_path, self.clock)
        self.memory = MemoryManager(store, self.clock, user_id)
        self.gate = ApprovalGate(store, self.clock, session_id, user_id, self._emit)
        self.executor = Executor(self._provider_call, store, self.clock, self._emit, fault_hook=fault_hook,
                                 deadline_check=self._deadline_hit)
        self.ctx = ContextBuilder(SYSTEM_RULES, TOOLS, context_budget)
        saved = store.load_workflow(session_id)
        self.state = WorkflowState.from_dict(saved) if saved else WorkflowState()
        self.outbox: list[str] = []  # messages produced without a user prompt (recovery, proactive)
        self.manifests: list[dict[str, Any]] = []
        self._reset_run_counters()
        self._startup()

    # ================================================================ infra
    def _emit(self, event: str, **kw: Any) -> None:
        self.tracer.emit(event, **kw)

    def _reset_run_counters(self) -> None:
        self._model_calls = 0
        self._tool_calls = 0
        self._consecutive_errors = 0
        self._run_t0 = self.clock.monotonic()
        self._wall_t0 = time.monotonic()
        self._run_executions: list[dict[str, Any]] = []
        self._run_proposals: list[str] = []
        self._current_user_text = ""

    def _elapsed(self) -> float:
        # Fake clocks advance only through simulated latency; real clocks advance on their own.
        return max(self.clock.monotonic() - self._run_t0, 0.0) if not isinstance(self.clock, SystemClock) \
            else time.monotonic() - self._wall_t0

    def _deadline_hit(self) -> bool:
        return self._elapsed() > self.limits.max_wall_seconds

    def _check_limits(self, about_to: str) -> None:
        if self._deadline_hit():
            raise LimitExceeded("wall_clock")
        if about_to == "model" and self._model_calls >= self.limits.max_model_calls:
            raise LimitExceeded("model_calls")
        if about_to == "tool" and self._tool_calls >= self.limits.max_tool_calls:
            raise LimitExceeded("tool_calls")
        if self._consecutive_errors >= self.limits.max_consecutive_tool_errors:
            raise LimitExceeded("consecutive_tool_errors")

    def _save(self) -> None:
        self.store.save_workflow(self.session_id, self.state.to_dict(), self.clock.now().isoformat())

    def _transition(self, to: str, reason: str) -> None:
        try:
            transition(self.state, to, reason, self._emit)
        except InvalidTransition:
            pass  # rejected transitions are traced; state is left unchanged
        self._save()

    def _provider_call(self, name: str, args: dict[str, Any]) -> Any:
        """Call the mock provider service; traced with latency and a sanitized result."""
        t0 = time.perf_counter()
        m0 = self.clock.monotonic()
        try:
            result = self.world.call(name, args)
        except SimulatedCrash:
            raise
        except Exception as e:
            self._emit("tool_call", tool=name, args=args, origin="provider", error=f"{type(e).__name__}: {e}",
                       latency_ms=round((time.perf_counter() - t0) * 1000, 2))
            raise
        lat = (time.perf_counter() - t0) * 1000 + (self.clock.monotonic() - m0) * 1000
        self._emit("tool_call", tool=name, args=args, origin="provider", result=sanitize(result),
                   latency_ms=round(lat, 2))
        return result

    # ============================================================== startup
    def _startup(self) -> None:
        self.tracer.start_run("startup", session_id=self.session_id)
        # 1. Recover side effects that were in flight when a previous process died.
        for p in self.store.proposals(user_id=self.user_id, status="executing"):
            out = self.executor.resolve_in_flight(p)
            if out["status"] == "executed":
                msg = (f"✅ Recovered after a restart: your {p['action']} [{p['id']}] did go through — "
                       f"verified with the provider (booking {out.get('booking_id')}).")
            elif out["status"] == "failed":
                msg = (f"⚠️ After a restart I checked [{p['id']}]: it was NOT applied by the provider, "
                       f"so nothing was booked. Say the word and I'll set it up again.")
            else:
                msg = f"⚠️ I couldn't reach the provider to confirm [{p['id']}] yet; I'll keep checking."
            self.outbox.append(msg)
            self.store.add_message(self.session_id, "assistant", msg, self.clock.now().isoformat(), self.user_id)
            if out["status"] in ("executed", "failed"):
                self.state.last_outcome = out
                self._transition("completed" if out["status"] == "executed" else "failed", "recovery")
        # 2. Code-side inference of unconfirmed preferences from booking history.
        try:
            hist = self.world.call("list_bookings", {"user_id": self.user_id})
            if isinstance(hist, dict) and "bookings" in hist:
                for m in self.memory.infer_from_bookings(hist["bookings"]):
                    self._emit("memory", action="inferred", id=m["id"], key=m["key"], scope=m["scope"])
        except Exception as e:
            self._emit("memory", action="inference_skipped", error=str(e))
        self._emit("run_end", outcome="startup_complete", reply=None)

    # ============================================================ user turn
    def handle_user_message(self, text: str) -> str:
        self._reset_run_counters()
        self._current_user_text = text
        self.tracer.start_run("user_turn", user_text=text, phase=self.state.phase)
        current_turn = self.store.turn_count(self.session_id)
        self.store.add_message(self.session_id, "user", text, self.clock.now().isoformat(), self.user_id)

        notes: list[str] = []
        d = self.gate.on_user_message(text, current_turn)
        p = d["proposal"]
        if d["decision"] == "approved":
            call = {"book": f"book(hold_id=\"{p['display'].get('hold_id')}\")",
                    "cancel": f"cancel(booking_id=\"{p['material'].get('booking_id')}\")",
                    "reschedule": f"reschedule(booking_id=\"{p['material'].get('booking_id')}\", "
                                  f"slot=\"{p['material'].get('new_slot')}\")"}[p["action"]]
            notes.append(f"The user APPROVED proposal {p['id']} ({p['action']}). Execute exactly it now: {call}.")
        elif d["decision"] == "rejected":
            notes.append(f"The user DECLINED proposal {p['id']}. Do not execute it; ask what they'd like instead.")
            self._transition("choosing", "user declined proposal")
        elif d["decision"] == "stale":
            notes.append(f"The user said yes, but proposal {p['id']} was not the last thing they were shown. "
                         f"Do not execute; re-confirm by calling request_approval again.")
        elif d["decision"] == "no_pending":
            prev = f" Previous proposal {p['id']} is already '{p['status']}'." if p else ""
            notes.append("The user said yes, but no proposal is awaiting approval, so nothing was approved." + prev +
                         " Never repeat a booking/cancellation that already happened. If they are agreeing to a "
                         "suggestion you just made, continue it; any booking/cancel/reschedule still needs "
                         "request_approval first.")
        elif p is not None:
            notes.append(f"Proposal {p['id']} is still awaiting approval; this message did NOT approve it. "
                         f"If the user changed a detail, update the request and make a new proposal.")
        reply = self._run_loop([{"role": "user", "content": text}], notes, text)
        self.store.add_message(self.session_id, "assistant", reply, self.clock.now().isoformat(), self.user_id)
        self._emit("run_end", outcome="replied", reply=reply, phase=self.state.phase,
                   model_calls=self._model_calls, tool_calls=self._tool_calls)
        self._save()
        return reply

    # ============================================================ proactive
    def process_events(self) -> list[str]:
        """Poll provider-side events. Called by a scheduler/poller — not by the user."""
        out = []
        for ev in self.world.poll_events(self.user_id):
            if self.store.event_processed(ev["event_id"]):
                continue
            self._reset_run_counters()
            self.tracer.start_run("proactive_event", trigger=ev)
            msg = self._handle_provider_cancel(ev)
            self.store.mark_event(ev["event_id"], self.user_id, "notified" if msg else "ignored",
                                  self.clock.now().isoformat())
            self._emit("run_end", outcome="notified" if msg else "ignored", reply=msg)
            if msg:
                out.append(msg)
                self.outbox.append(msg)
        return out

    def _handle_provider_cancel(self, ev: dict[str, Any]) -> str | None:
        bookings = self.world.call("list_bookings", {"user_id": self.user_id}).get("bookings", [])
        b = next((x for x in bookings if x["booking_id"] == ev["booking_id"]), None)
        if not b:
            self._emit("proactive", decision="ignore", reason="booking not found for user")
            return None
        if datetime.fromisoformat(b["slot"]) <= self.clock.now():
            self._emit("proactive", decision="ignore", reason="booking already in the past")
            return None
        if any(x["service"] == b["service"] and x["status"] == "confirmed" and x["slot"] > self.clock.now().isoformat()
               and x["booking_id"] != b["booking_id"] for x in bookings):
            self._emit("proactive", decision="notify_only", reason="user already has another upcoming booking for this service")
            msg = (f"Heads-up: {b['provider_name']} cancelled your {b['service']} visit on {human_slot(b['slot'])}. "
                   f"You still have another upcoming {b['service']} booking, so I haven't looked for a replacement.")
            self.store.add_message(self.session_id, "assistant", msg, self.clock.now().isoformat(), self.user_id)
            return msg
        self._emit("proactive", decision="notify_and_find_replacement", booking=b)
        slot_day = datetime.fromisoformat(b["slot"]).date()
        start = max(slot_day, self.clock.now().date())
        req = ErrandRequest(intent="rebook_after_provider_cancel", service=b["service"],
                            area=self.memory.value("preferred_area") or b.get("area") or "indiranagar",
                            date_start=start.isoformat(), date_end=(start + timedelta(days=3)).isoformat(),
                            earliest_time=self.memory.value("earliest_time", b["service"]) or self.memory.value("earliest_time"),
                            latest_time=self.memory.value("latest_time", b["service"]) or self.memory.value("latest_time"),
                            budget_max=int(self.memory.value("budget_max", b["service"]) or round(b["price"] * 1.25)),
                            provider_preference=f"not {b['provider_name']} ({b['provider_id']})",
                            notes=f"replacement for {b['booking_id']} cancelled by provider")
        req.provenance = {k: "event" for k in ("intent", "service", "date_start", "date_end", "notes")}
        req.provenance.update({k: "memory" for k in ("area", "earliest_time", "latest_time", "budget_max")})
        self.state.request = req
        self.state.last_search, self.state.active_proposal_id = [], None
        self._transition("searching", f"provider cancelled {b['booking_id']}")
        notes = [f"AUTOMATED EVENT (not a user message): {b['provider_name']} cancelled booking {b['booking_id']} "
                 f"({b['service']}, {human_slot(b['slot'])}). The user doesn't know yet. Tell them briefly, find a "
                 f"replacement within the errand request (it reflects their saved preferences; exclude "
                 f"{b['provider_id']}), hold the best slot and call request_approval. Never book without approval."]
        trigger = f"[automated event] provider_cancelled booking {b['booking_id']}"
        reply = self._run_loop([{"role": "user", "content": trigger}], notes, trigger)
        self.store.add_message(self.session_id, "assistant", reply, self.clock.now().isoformat(), self.user_id)
        self._save()
        return reply

    # ============================================================ main loop
    def _state_view(self) -> dict[str, Any]:
        req = {k: v for k, v in asdict(self.state.request).items() if v not in (None, "", {}, False)}
        req["missing"] = self.state.request.missing() if self.state.request.intent or self.state.request.service else []
        now = self.clock.now()
        holds = [{"hold_id": h["hold_id"], "provider_id": h["provider_id"], "provider_name": h.get("provider_name"),
                  "slot": h["slot"], "quoted_price": h["quoted_price"], "expires_at": h["expires_at"]}
                 for h in self.state.holds.values() if datetime.fromisoformat(h["expires_at"]) > now
                 and h.get("status", "active") == "active"]
        pending = None
        if self.state.active_proposal_id:
            pp = self.store.get_proposal(self.state.active_proposal_id)
            if pp:
                pending = {"id": pp["id"], "action": pp["action"], "status": pp["status"], "material": pp["material"]}
        return {"now": now.strftime("%A %Y-%m-%d %H:%M"), "phase": self.state.phase, "request": req,
                "candidates": self.state.last_search[:5], "active_holds": holds[-3:], "proposal": pending,
                "last_outcome": self.state.last_outcome}

    def _history(self, exclude_last: int) -> list[dict[str, str]]:
        msgs = self.store.messages(self.session_id)
        return msgs[:-exclude_last] if exclude_last else msgs

    def _run_loop(self, turn_messages: list[dict[str, Any]], notes: list[str], user_text: str) -> str:
        turn_tool_results: list[dict[str, Any]] = []
        exclude = 1 if turn_messages and not turn_messages[0]["content"].startswith("[automated event]") else 0
        history = self._history(exclude)
        final_text = ""
        try:
            while True:
                self._check_limits("model")
                built = self.ctx.build(state_view=self._state_view(), notes=notes,
                                       memories_all=self.memory.all(include_inactive=True), history=history,
                                       turn_messages=turn_messages, turn_tool_results=turn_tool_results,
                                       user_text=user_text)
                self.manifests.append({"run_id": self.tracer.run_id, **built.manifest,
                                       "rendered": built.request.system + json.dumps(built.request.messages, default=str)})
                self._emit("context", **built.manifest)
                t0 = time.perf_counter()
                try:
                    resp: ModelResponse = self.llm.complete(built.request)
                except SimulatedCrash:
                    raise
                except Exception as e:
                    self._emit("model_error", error=f"{type(e).__name__}: {e}")
                    return self._finalize(None, exit_reason="model_unavailable")
                self._model_calls += 1
                in_tok = resp.input_tokens or estimate_tokens(built.request.system + json.dumps(built.request.messages, default=str))
                out_tok = resp.output_tokens or estimate_tokens(resp.text + json.dumps([c.__dict__ for c in resp.tool_calls]))
                self._emit("model_call", model=resp.model, latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                           input_tokens=in_tok, output_tokens=out_tok,
                           cost_usd=resp.cost_usd if resp.cost_usd is not None else estimate_cost(resp.model, in_tok, out_tok),
                           stop_reason=resp.stop_reason, text=sanitize(resp.text, 200),
                           tool_calls=[{"name": c.name, "input": c.input} for c in resp.tool_calls])
                if not resp.tool_calls:
                    final_text = resp.text
                    break
                turn_messages.append({"role": "assistant", "content": resp.raw_content})
                blocks = []
                limit_hit: LimitExceeded | None = None
                for call in resp.tool_calls:
                    try:
                        if limit_hit:
                            raise limit_hit
                        self._check_limits("tool")
                    except LimitExceeded as e:
                        limit_hit = e
                        blocks.append({"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                                       "content": json.dumps({"error": f"{e.kind}_limit_reached"})})
                        continue
                    self._tool_calls += 1
                    result = self._dispatch(call.name, dict(call.input or {}))
                    safe, flags = sanitize_tool_result(result)
                    is_err = isinstance(result, dict) and "error" in result
                    self._consecutive_errors = self._consecutive_errors + 1 if is_err else 0
                    turn_tool_results.append({"name": call.name, "input": call.input, "result": safe})
                    blocks.append({"type": "tool_result", "tool_use_id": call.id, "is_error": is_err,
                                   "content": json.dumps(safe, default=str)})
                    if flags:
                        self._emit("untrusted_content", tool=call.name, flags=sorted(set(flags)))
                turn_messages.append({"role": "user", "content": blocks})
                self._save()
                if limit_hit:
                    raise limit_hit
        except LimitExceeded as e:
            self._emit("limit", kind=e.kind, model_calls=self._model_calls, tool_calls=self._tool_calls,
                       elapsed_s=round(self._elapsed(), 2))
            return self._finalize(None, exit_reason=f"limit:{e.kind}")
        except ContextOverflow as e:
            self._emit("limit", kind="context_budget", detail=str(e))
            return self._finalize(None, exit_reason="limit:context_budget")
        return self._finalize(final_text)

    # ============================================================= finalize
    def _status_lines(self) -> list[str]:
        lines = []
        for ex in self._run_executions:
            p = self.store.get_proposal(ex["proposal_id"])
            act = p["action"] if p else "action"
            if ex["status"] == "executed":
                if act == "book":
                    lines.append(f"✅ Verified: booking **{ex.get('booking_id')}** confirmed — {p['display']['provider_name']}, "
                                 f"{human_slot(ex.get('slot'))}, ₹{ex.get('price')} (checked via {ex.get('verified_by')}).")
                elif act == "cancel":
                    lines.append(f"✅ Verified: booking **{ex.get('booking_id')}** is cancelled.")
                else:
                    lines.append(f"✅ Verified: booking **{ex.get('booking_id')}** moved to {human_slot(ex.get('slot'))}.")
            elif ex["status"] == "unknown":
                lines.append(f"⚠️ I could not yet confirm whether [{ex['proposal_id']}] went through. I will not retry "
                             f"blindly; I'll verify with the provider before doing anything else.")
            else:
                lines.append(f"❌ Not completed [{ex['proposal_id']}]: {ex.get('reason') or ex.get('error')}. Nothing was charged.")
        return lines

    def _finalize(self, text: str | None, exit_reason: str | None = None) -> str:
        parts: list[str] = []
        executed_now = any(e["status"] == "executed" for e in self._run_executions)
        if text and CLAIM_RE.search(text) and not executed_now:
            self._emit("guard", kind="unverified_claim_blocked", text=sanitize(text, 200))
            text = "I haven't completed any booking, cancellation or change in this step."
        if exit_reason:
            reasons = {
                "model_unavailable": "I couldn't reach the language model just now.",
                "limit:model_calls": "I hit my step limit for this request before finishing.",
                "limit:tool_calls": "I hit my tool-call limit for this request before finishing.",
                "limit:wall_clock": "This took longer than my time limit, so I stopped.",
                "limit:consecutive_tool_errors": "The provider service kept returning errors, so I stopped retrying.",
                "limit:context_budget": "This conversation got too large for my working memory.",
            }
            parts.append(reasons.get(exit_reason, f"I had to stop ({exit_reason})."))
            if not executed_now and not any(e["status"] == "unknown" for e in self._run_executions):
                parts.append("Nothing was booked, cancelled or changed.")
            if self.state.request.service:
                parts.append(f"Saved progress: {self.state.request.service} in {self.state.request.area or '?'}, "
                             f"phase '{self.state.phase}'. Send another message to continue.")
        elif text:
            parts.append(text.strip())
        parts.extend(self._status_lines())
        for pid in self._run_proposals:
            p = self.store.get_proposal(pid)
            if p and p["status"] == "pending":
                parts.append(self.gate.render(p))
        if not parts:
            parts.append("Done.")
        return "\n\n".join(parts)

    # ============================================================= dispatch
    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            handler = getattr(self, f"_tool_{name}", None)
            result = handler(**args) if handler else {"error": "unknown_tool", "tool": name}
        except SimulatedCrash:
            raise
        except TypeError as e:
            result = {"error": "invalid_arguments", "detail": str(e)}
        except Exception as e:  # unexpected failure: surface as data, never crash the run
            result = {"error": "tool_exception", "detail": f"{type(e).__name__}: {e}"}
        if not isinstance(result, dict):
            result = {"error": "malformed_response", "detail": str(result)[:200]}
        self._emit("tool_call", tool=name, args=args, origin="model", result=sanitize(result),
                   latency_ms=round((time.perf_counter() - t0) * 1000, 2), blocked=result.get("error") == "approval_required")
        return result

    # --- request / state
    def _tool_update_request(self, **fields: Any) -> dict[str, Any]:
        req = self.state.request
        changed = apply_request_update(req, fields, "user")
        if "*reset*" in changed:
            self.state.last_search, self.state.active_proposal_id = [], None
        filled = {}
        if (req.intent or "book") in ("book",):
            for key, attr in (("preferred_area", "area"), ("earliest_time", "earliest_time"), ("latest_time", "latest_time")):
                if getattr(req, attr) is None:
                    v = (self.memory.value(key, req.service) if req.service and key != "preferred_area" else None) \
                        or self.memory.value(key)
                    if v:
                        setattr(req, attr, v.lower() if attr == "area" else v)
                        req.provenance[attr] = "memory"
                        filled[attr] = v
            if req.budget_max is None and not req.budget_declined and req.service:
                v = self.memory.value("budget_max", req.service) or self.memory.value("budget_max")
                if v and str(v).isdigit():
                    req.budget_max, req.provenance["budget_max"] = int(v), "memory"
                    filled["budget_max"] = v
        if changed and self.state.active_proposal_id:
            # A changed requirement makes any open proposal stale.
            p = self.store.get_proposal(self.state.active_proposal_id)
            if p and p["status"] in ("pending", "approved") and set(changed) & {"service", "area", "date_start",
                                                                                 "date_end", "earliest_time",
                                                                                 "latest_time", "budget_max",
                                                                                 "target_slot", "provider_preference"}:
                self.store.update_proposal(p["id"], status="superseded", status_reason=f"request changed: {changed}")
                self._emit("approval", action="superseded", proposal_id=p["id"], reason=f"request changed {changed}")
                self.state.active_proposal_id = None
        missing = req.missing()
        self._transition("gathering" if missing else "searching", f"request updated: {changed or 'no change'}")
        return {"request": {k: v for k, v in asdict(req).items() if v not in (None, "", {}, False)},
                "changed": changed, "filled_from_memory": filled, "missing": missing}

    # --- provider read tools
    def _window(self) -> tuple[str | None, str | None]:
        return self.state.request.earliest_time, self.state.request.latest_time

    def _slot_ok(self, slot: str) -> bool:
        e, l = self._window()
        t = slot[11:16]
        req = self.state.request
        if e and t < e:
            return False
        if l and t > l:
            return False
        if req.date_start and slot[:10] < req.date_start:
            return False
        if req.date_end and slot[:10] > req.date_end:
            return False
        return True

    def _tool_search_providers(self, service: str, area: str, date_range: Any) -> dict[str, Any]:
        r = self._provider_call("search_providers", {"service": service, "area": area, "date_range": date_range})
        if not isinstance(r, dict) or "error" in r:
            return r if isinstance(r, dict) else {"error": "malformed_response"}
        budget = self.state.request.budget_max
        avoid = (self.state.request.provider_preference or "")
        cands = []
        for p in r["providers"]:
            slots = [s for s in p["available_slots"] if self._slot_ok(s)]
            cands.append({"provider_id": p["provider_id"], "name": p["name"], "price": p["price"],
                          "rating": p["rating"], "verified": p["attributes"].get("verified"),
                          "warranty_days": p["attributes"].get("warranty_days"),
                          "within_budget": budget is None or p["price"] <= budget,
                          "excluded_by_preference": avoid.startswith("not ") and p["provider_id"] in avoid,
                          "matching_slots": slots[:6], "matching_slot_count": len(slots)})
        cands.sort(key=lambda c: (c["excluded_by_preference"], not c["within_budget"], c["matching_slot_count"] == 0,
                                  -c["rating"], c["price"]))
        self.state.last_search = cands
        self._transition("choosing", f"search returned {len(cands)} providers")
        return {"service": r["service"], "area": r["area"], "date_range": r["date_range"],
                "filters_applied": {"time_window": self._window(), "budget_max": budget},
                "providers": cands}

    def _tool_get_provider(self, provider_id: str) -> dict[str, Any]:
        r = self._provider_call("get_provider", {"provider_id": provider_id})
        if isinstance(r, dict) and "available_slots" in r:
            r = {**r, "available_slots": [s for s in r["available_slots"] if self._slot_ok(s)][:8]}
        return r

    def _tool_hold_slot(self, provider_id: str, slot: str) -> dict[str, Any]:
        r = self._provider_call("hold_slot", {"provider_id": provider_id, "slot": slot})
        if isinstance(r, dict) and "hold_id" in r:
            prov = self.world.state["providers"].get(provider_id, {})
            self.state.holds[r["hold_id"]] = {**r, "provider_name": prov.get("name"), "service": prov.get("service")}
            if r["quoted_price"] != r.get("search_price"):
                r = {**r, "price_changed_from_search": True}
        return r

    def _tool_get_booking_status(self, hold_id: str | None = None, **_: Any) -> dict[str, Any]:
        return self._provider_call("get_booking_status", {"hold_id": hold_id})

    def _tool_list_bookings(self) -> dict[str, Any]:
        r = self._provider_call("list_bookings", {"user_id": self.user_id})
        if isinstance(r, dict) and "bookings" in r:
            now = self.clock.now().isoformat()
            r = {**r, "bookings": [{**b, "when": "upcoming" if b["slot"] > now else "past"} for b in r["bookings"]]}
        return r

    # --- approvals and side effects
    def _booking(self, booking_id: str) -> dict[str, Any] | None:
        r = self._provider_call("list_bookings", {"user_id": self.user_id})
        return next((b for b in r.get("bookings", []) if b["booking_id"] == booking_id), None) if isinstance(r, dict) else None

    def _tool_request_approval(self, action: str, hold_id: str | None = None, booking_id: str | None = None,
                               new_slot: str | None = None) -> dict[str, Any]:
        presented_turn = self.store.turn_count(self.session_id) + 1
        if action == "book":
            hold = self.state.holds.get(hold_id or "")
            if not hold:
                return {"error": "unknown_hold", "detail": "call hold_slot first and pass its hold_id"}
            if datetime.fromisoformat(hold["expires_at"]) <= self.clock.now():
                return {"error": "hold_expired", "detail": "hold a slot again before asking for approval"}
            facts = {"provider_id": hold["provider_id"], "slot": hold["slot"], "price": hold["quoted_price"]}
            display = {"provider_name": hold.get("provider_name"), "service": hold.get("service"),
                       "slot_human": human_slot(hold["slot"]), "search_price": hold.get("search_price"),
                       "hold_id": hold["hold_id"],
                       "hold_expires_human": datetime.fromisoformat(hold["expires_at"]).strftime("%H:%M")}
        elif action in ("cancel", "reschedule"):
            b = self._booking(booking_id or "")
            if not b:
                return {"error": "booking_not_found", "booking_id": booking_id}
            if b["status"] != "confirmed":
                return {"error": "booking_not_active", "booking_status": b["status"]}
            facts = {"booking_id": b["booking_id"], "new_slot": new_slot}
            display = {"provider_name": b["provider_name"], "service": b["service"], "slot_human": human_slot(b["slot"]),
                       "old_slot_human": human_slot(b["slot"]), "new_slot_human": human_slot(new_slot or "")}
            if action == "reschedule" and not new_slot:
                return {"error": "new_slot_required"}
        else:
            return {"error": "unknown_action", "action": action}
        p = self.gate.create(action, facts, display, presented_turn)
        self.state.active_proposal_id = p["id"]
        self._run_proposals.append(p["id"])
        self._transition("awaiting_approval", f"proposal {p['id']} created")
        return {"proposal_id": p["id"], "status": "awaiting_user_approval",
                "shown_to_user": self.gate.render(p),
                "instruction": "The application shows this to the user verbatim. End your turn and wait for their reply."}

    def _authorized(self, action: str, facts: dict[str, Any]) -> dict[str, Any] | dict[str, str]:
        try:
            return self.gate.authorize(action, facts)
        except ApprovalRequired as e:
            return {"error": "approval_required", "reason": e.reason, **e.detail,
                    "instruction": "Not executed. Create a proposal with request_approval and wait for the user."}

    def _record_execution(self, out: dict[str, Any]) -> dict[str, Any]:
        self._run_executions.append(out)
        self.state.last_outcome = {k: out.get(k) for k in ("proposal_id", "status", "booking_id", "slot", "price",
                                                           "reason", "error") if out.get(k) is not None}
        if out["status"] == "executed":
            self._transition("completed", f"{out['proposal_id']} executed and verified")
            self.state.active_proposal_id = None
        elif out["status"] == "failed":
            self._transition("failed", f"{out['proposal_id']} failed: {out.get('reason')}")
            self.state.active_proposal_id = None
        return out

    def _tool_book(self, hold_id: str) -> dict[str, Any]:
        hold = self.state.holds.get(hold_id)
        if not hold:
            return {"error": "unknown_hold", "hold_id": hold_id}
        if datetime.fromisoformat(hold["expires_at"]) <= self.clock.now() and hold.get("status", "active") == "active":
            approved = [p for p in self.gate.open_proposals() if p["action"] == "book" and p["status"] == "approved"]
            if approved:
                # Approval is for provider+slot+price, not for a hold id: re-holding is allowed, but if the
                # re-quote differs the gate will invalidate the approval when book() is called again.
                hold["status"] = "expired"
                self._emit("approval", action="hold_expired_before_execution", proposal_id=approved[-1]["id"])
                return {"error": "hold_expired_before_booking", "approved": approved[-1]["material"],
                        "instruction": "Hold the same provider+slot again, then call book with the new hold_id. "
                                       "If the quoted price changed, approval is void and must be requested again."}
        p = self._authorized("book", {"provider_id": hold["provider_id"], "slot": hold["slot"],
                                      "price": hold["quoted_price"]})
        if "error" in p:
            return p
        if p["status"] in ("executed", "executing"):
            self._emit("approval", action="duplicate_execution_suppressed", proposal_id=p["id"])
            out = self.executor.book(p, hold_id)  # returns ledger result / resolves in-flight; never re-sends
            return {**out, "note": "already executed earlier; not booked again"}
        self._transition("executing", f"executing {p['id']}")
        out = self.executor.book(p, hold_id)
        if out["status"] in ("executed", "failed"):
            hold["status"] = "consumed" if out["status"] == "executed" else "invalid"
        return self._record_execution(out)

    def _tool_cancel(self, booking_id: str) -> dict[str, Any]:
        p = self._authorized("cancel", {"booking_id": booking_id, "new_slot": None})
        if "error" in p:
            return p
        if p["status"] == "executed":
            return {**self.executor.cancel(p), "note": "already cancelled earlier"}
        self._transition("executing", f"executing {p['id']}")
        return self._record_execution(self.executor.cancel(p))

    def _tool_reschedule(self, booking_id: str, slot: str) -> dict[str, Any]:
        p = self._authorized("reschedule", {"booking_id": booking_id, "new_slot": slot})
        if "error" in p:
            return p
        if p["status"] == "executed":
            return {**self.executor.reschedule(p), "note": "already rescheduled earlier"}
        self._transition("executing", f"executing {p['id']}")
        return self._record_execution(self.executor.reschedule(p))

    # --- memory
    def _user_messages(self) -> list[str]:
        return [m["content"] for m in self.store.messages(self.session_id) if m["role"] == "user"]

    def _tool_remember(self, key: str, value: str, evidence: str, scope: str = "global") -> dict[str, Any]:
        try:
            m = self.memory.remember_explicit(key, value, scope, evidence, self._user_messages())
        except MemoryError_ as e:
            self._emit("memory", action="write_rejected", key=key, reason=str(e))
            return {"error": "memory_write_rejected", "reason": str(e)}
        self._emit("memory", action="remembered", id=m["id"], key=m["key"], scope=m["scope"], source="explicit")
        return {"saved": {k: m[k] for k in ("id", "key", "value", "scope", "source")}}

    def _tool_list_memories(self) -> dict[str, Any]:
        items = self.memory.describe()
        return {"explicit": [m for m in items if m["source"] == "explicit"],
                "inferred": [m for m in items if m["source"] == "inferred"],
                "note": "inferred facts are only used after the user confirms them"}

    def _tool_forget_memory(self, memory_id: str | None = None, key: str | None = None,
                            scope: str | None = None) -> dict[str, Any]:
        if not FORGET_INTENT_RE.search(self._current_user_text):
            self._emit("memory", action="forget_rejected", reason="user did not ask to forget anything")
            return {"error": "forget_requires_user_request"}
        gone = self.memory.forget(mid=memory_id, key=key, scope=scope)
        self._emit("memory", action="forgotten", ids=[m["id"] for m in gone])
        return {"forgotten": [{"id": m["id"], "key": m["key"], "scope": m["scope"]} for m in gone]}

    def _tool_confirm_memory(self, memory_id: str, user_quote: str) -> dict[str, Any]:
        try:
            m = self.memory.confirm(memory_id, user_quote, self._current_user_text)
        except MemoryError_ as e:
            return {"error": "confirm_rejected", "reason": str(e)}
        self._emit("memory", action="confirmed", id=m["id"], key=m["key"])
        return {"confirmed": {k: m[k] for k in ("id", "key", "value", "scope")}}

    # ============================================================ inspection
    def snapshot(self) -> dict[str, Any]:
        return {"state": self._state_view(), "memories": self.memory.describe(),
                "proposals": self.store.proposals(session_id=self.session_id)}
