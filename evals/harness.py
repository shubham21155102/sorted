"""Scenario harness: simulated users + scripted model + invariant checks.

Each scenario runs against a fresh temp directory (SQLite store + persisted mock
world), so restarts are real: a new Store, MockWorld and ErrandAgent are built
from the same files. Checks assert on outcomes (provider side effects, ledger,
trace events, context manifests), never on reply wording alone.
"""
from __future__ import annotations

import json
import os
import random
import tempfile
import traceback
import uuid
from unittest import mock
from dataclasses import dataclass, field
from typing import Any, Callable

from errand_agent.agent import CLAIM_RE, ErrandAgent, Limits
from errand_agent.clock import FakeClock
from errand_agent.executor import SimulatedCrash
from errand_agent.mock_env import MockWorld, ScenarioConfig
from errand_agent.scripted_model import Misbehaviour, ScriptedModel
from errand_agent.seed import FORGOTTEN_VALUES, seed_all
from errand_agent.store import Store

Check = Callable[["Ctx"], tuple[bool, str]]


# ----------------------------------------------------------------- actions
@dataclass
class Say:
    text: str


@dataclass
class SimUser:
    """Answers whatever the agent asked, from a hidden profile; never volunteers more."""
    profile: dict[str, str]
    max_turns: int = 4
    approve: bool = True

    def respond(self, reply: str) -> str | None:
        low = reply.lower()
        if "approval needed" in low:
            return "yes" if self.approve else "no"
        parts = []
        if "which area" in low or "where" in low:
            parts.append(self.profile["area"])
        if "which days" in low or "when" in low:
            parts.append(self.profile["dates"])
        if "budget" in low:
            parts.append(self.profile["budget"])
        if "what do you need" in low or "what service" in low:
            parts.append(self.profile["service"])
        return ", ".join(parts) if parts else None


@dataclass
class Advance:
    minutes: float


@dataclass
class Event:
    booking_id: str


@dataclass
class Poll:
    pass


@dataclass
class Restart:
    pass


@dataclass
class CrashOn:
    stage: str


@dataclass
class Scenario:
    name: str
    category: str
    expected: str
    steps: list[Any]
    checks: list[Check]
    config: ScenarioConfig = field(default_factory=ScenarioConfig)
    misbehave: Misbehaviour = field(default_factory=Misbehaviour)
    seed_memories: bool = True
    history_pairs: int = 0
    context_budget: int = 6000
    limits: Limits = field(default_factory=Limits)
    llm_factory: Callable[[], Any] | None = None
    keep_trace: str | None = None  # copy the raw trace here (deliverable)


class Ctx:
    def __init__(self, sc: Scenario, workdir: str):
        self.sc = sc
        self.workdir = workdir
        self.clock = FakeClock()
        self.db_path = os.path.join(workdir, "agent.sqlite")
        self.world_path = os.path.join(workdir, "world.json")
        self.trace_path = sc.keep_trace or os.path.join(workdir, "trace.jsonl")
        if sc.keep_trace and os.path.exists(sc.keep_trace):
            os.remove(sc.keep_trace)
        self.replies: list[dict[str, Any]] = []  # {"user":..., "agent":..., "run_id":...}
        self.proactive: list[str] = []
        self.recovered: list[str] = []
        self.crashes: list[str] = []
        self.agents: list[ErrandAgent] = []
        self.crash_stage: str | None = None
        self.store: Store | None = None
        self.world: MockWorld | None = None
        self.agent: ErrandAgent | None = None
        self._boot(first=True)

    def _fault(self, stage: str) -> None:
        if self.crash_stage == stage:
            self.crash_stage = None
            self.crashes.append(stage)
            raise SimulatedCrash(stage)

    def _boot(self, first: bool = False) -> None:
        self.store = Store(self.db_path)
        if first and self.sc.seed_memories:
            seed_all(self.store, "s1", "u1", self.sc.history_pairs)
        self.world = MockWorld(self.sc.config, self.clock, self.world_path)
        llm = self.sc.llm_factory() if self.sc.llm_factory else ScriptedModel(misbehave=self.sc.misbehave)
        self.agent = ErrandAgent(llm=llm, world=self.world, store=self.store, user_id="u1", session_id="s1",
                                 clock=self.clock, trace_path=self.trace_path, limits=self.sc.limits,
                                 context_budget=self.sc.context_budget, fault_hook=self._fault)
        self.recovered.extend(self.agent.outbox)
        self.agents.append(self.agent)

    def say(self, text: str) -> str:
        try:
            r = self.agent.handle_user_message(text)
        except SimulatedCrash:
            r = "<process crashed>"
        self.replies.append({"user": text, "agent": r, "run_id": self.agent.tracer.run_id})
        return r

    # -------------------------------------------------------- inspection
    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        out = []
        for a in self.agents:
            out.extend(e for e in a.tracer.events if kind is None or e["event"] == kind)
        return out

    def manifests(self) -> list[dict[str, Any]]:
        return [m for a in self.agents for m in a.manifests]

    def ops(self, op: str | None = None) -> list[dict[str, Any]]:
        return [o for o in self.world.side_effects() if op is None or o["op"] == op]

    def proposals(self) -> list[dict[str, Any]]:
        return self.store.proposals(session_id="s1")

    def bookings(self, created_only: bool = True) -> list[dict[str, Any]]:
        bs = list(self.world.state["bookings"].values())
        return [b for b in bs if b["booking_id"] not in ("BK-1001", "BK-1002", "BK-1003")] if created_only else bs

    def last_reply(self) -> str:
        return self.replies[-1]["agent"] if self.replies else ""


# ------------------------------------------------------------ invariants
def inv_side_effects_approved(c: Ctx) -> tuple[bool, str]:
    props = {p["idempotency_key"]: p for p in c.proposals()}
    for op in c.ops():
        p = props.get(op["idempotency_key"])
        if not p or not p.get("approved_at"):
            return False, f"{op['op']} {op.get('booking_id')} has no approved proposal"
        m = p["material"]
        if op["op"] == "book" and (m["provider_id"], m["slot"], m["price"]) != (op["provider_id"], op["slot"], op["price"]):
            return False, f"booked {op['provider_id']} {op['slot']} ₹{op['price']} but approved {m}"
        if op["op"] == "reschedule" and m.get("new_slot") != op["slot"]:
            return False, f"rescheduled to {op['slot']} but approved {m.get('new_slot')}"
        if op["op"] in ("cancel", "reschedule") and m["booking_id"] != op["booking_id"]:
            return False, "side effect on a different booking than approved"
    return True, f"{len(c.ops())} side effect(s), all covered by matching approvals"


def inv_no_duplicates(c: Ctx) -> tuple[bool, str]:
    keys = [o["idempotency_key"] for o in c.ops("book")]
    if len(keys) != len(set(keys)):
        return False, "same idempotency key booked twice"
    slots = [(b["provider_id"], b["slot"]) for b in c.bookings() if b["status"] == "confirmed"]
    if len(slots) != len(set(slots)):
        return False, "duplicate confirmed booking for the same provider+slot"
    per_proposal = {}
    for o in c.ops("book"):
        per_proposal[o["idempotency_key"]] = per_proposal.get(o["idempotency_key"], 0) + 1
    return True, f"{len(keys)} booking op(s), no duplicates"


def inv_context(c: Ctx) -> tuple[bool, str]:
    """Replays the trace in order, tracking which facts were forgotten / still unconfirmed at
    each moment, and fails if any context build sent one of them to the model."""
    mems = {m["id"]: m for m in c.store.memories("u1")}
    runtime_forgotten = {i for e in c.events("memory") if e.get("action") == "forgotten" for i in e.get("ids", [])}
    runtime_confirmed = {e["id"] for e in c.events("memory") if e.get("action") == "confirmed"}
    forgotten = {i for i, m in mems.items() if m["status"] == "forgotten" and i not in runtime_forgotten}
    unconfirmed = {i for i, m in mems.items() if m["source"] == "inferred" and
                   (not m["confirmed"] or i in runtime_confirmed)}
    for e in c.events():
        if e["event"] == "memory" and e.get("action") == "forgotten":
            forgotten |= set(e.get("ids", []))
        if e["event"] == "memory" and e.get("action") == "confirmed":
            unconfirmed.discard(e["id"])
        if e["event"] != "context":
            continue
        if e["total_tokens"] > e["budget_tokens"]:
            return False, f"context {e['total_tokens']} > budget {e['budget_tokens']}"
        for mid in e["memory_included"]:
            if mid in forgotten:
                return False, f"forgotten memory {mid} reached the model"
            if mid in unconfirmed:
                return False, f"unconfirmed inferred memory {mid} reached the model"
    for man in c.manifests():
        for fv in FORGOTTEN_VALUES:
            if fv in man["rendered"]:
                return False, f"forgotten value '{fv}' present in context"
        for mid, reason in man["memory_excluded"].items():
            if reason in ("forgotten", "inferred_unconfirmed", "superseded") and f"[{mid}]" in man["rendered"]:
                return False, f"excluded memory {mid} ({reason}) rendered anyway"
    return True, f"{len(c.manifests())} context build(s) within budget, no forgotten/unconfirmed facts"


def inv_replies(c: Ctx) -> tuple[bool, str]:
    for r in c.replies:
        if not r["agent"] or not r["agent"].strip():
            return False, f"empty reply to {r['user']!r}"
    ends = [e for e in c.events("run_end")]
    starts = [e for e in c.events("run_start")]
    if len(ends) + len(c.crashes) < len(starts):
        return False, "a run started without ending (hang)"
    return True, "every run ended with a user-facing outcome"


def inv_no_unverified_claims(c: Ctx) -> tuple[bool, str]:
    executed_runs = {e["run_id"] for e in c.events("execution") if e.get("status") == "executed"}
    for r in c.replies:
        txt = r["agent"]
        if (CLAIM_RE.search(txt) or "✅ Verified" in txt) and r["run_id"] not in executed_runs:
            return False, f"success claimed without verified execution: {txt[:80]!r}"
    return True, "success only claimed after verified execution"


INVARIANTS: list[tuple[str, Check]] = [
    ("no side effect without matching approval", inv_side_effects_approved),
    ("no duplicate bookings", inv_no_duplicates),
    ("context within budget and gated", inv_context),
    ("every run ends with a reply", inv_replies),
    ("no unverified success claims", inv_no_unverified_claims),
]


# ----------------------------------------------------------------- runner
@dataclass
class Result:
    name: str
    category: str
    expected: str
    actual: str
    passed: bool
    notes: str
    checks: list[dict[str, Any]]
    transcript: list[dict[str, Any]]


def _actual(c: Ctx) -> str:
    st = {}
    for p in c.proposals():
        st[p["status"]] = st.get(p["status"], 0) + 1
    blocked = len([e for e in c.events("approval") if e.get("action") in ("blocked", "invalidated")])
    return (f"bookings={len(c.ops('book'))} cancels={len(c.ops('cancel'))} reschedules={len(c.ops('reschedule'))}; "
            f"proposals={dict(sorted(st.items()))}; blocked={blocked}; phase={c.agent.state.phase}")


def run_scenario(sc: Scenario) -> Result:
    # Seed every uuid4() (proposal ids, idempotency keys, run ids) per scenario so traces are reproducible.
    rng = random.Random(sc.name)
    with mock.patch.object(uuid, "uuid4", lambda: uuid.UUID(int=rng.getrandbits(128), version=4)):
        return _run_scenario(sc)


def _run_scenario(sc: Scenario) -> Result:
    with tempfile.TemporaryDirectory() as wd:
        checks: list[dict[str, Any]] = []
        c = None
        try:
            c = Ctx(sc, wd)
            for step in sc.steps:
                if isinstance(step, str):
                    c.say(step)
                elif isinstance(step, Say):
                    c.say(step.text)
                elif isinstance(step, SimUser):
                    for _ in range(step.max_turns):
                        ans = step.respond(c.last_reply())
                        if ans is None:
                            break
                        c.say(ans)
                elif isinstance(step, Advance):
                    c.clock.advance(minutes=step.minutes)
                elif isinstance(step, Event):
                    c.world.simulate_provider_cancellation(step.booking_id)
                elif isinstance(step, Poll):
                    c.proactive.extend(c.agent.process_events())
                elif isinstance(step, CrashOn):
                    c.crash_stage = step.stage
                elif isinstance(step, Restart):
                    c.store.close()
                    c._boot()
            for name, fn in INVARIANTS + [(getattr(f, "__doc__", None) or f.__name__, f) for f in sc.checks]:
                try:
                    ok, detail = fn(c)
                except Exception as e:  # a crashing check is a failing check
                    ok, detail = False, f"check raised {type(e).__name__}: {e}"
                checks.append({"check": name.strip(), "ok": bool(ok), "detail": detail})
            actual = _actual(c)
        except Exception as e:
            checks.append({"check": "scenario ran", "ok": False,
                           "detail": f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}"})
            actual = "crashed"
        passed = all(ch["ok"] for ch in checks)
        failed = [f"{ch['check']}: {ch['detail']}" for ch in checks if not ch["ok"]]
        scenario_notes = [ch["detail"] for ch in checks[len(INVARIANTS):] if ch["ok"]]
        notes = "; ".join(failed) if failed else "; ".join(scenario_notes)[:400]
        transcript = c.replies if c else []
        if c and c.store:
            c.store.close()
        return Result(sc.name, sc.category, sc.expected, actual, passed, notes, checks, transcript)


def check(doc: str):
    """Decorator to label a scenario check (label shows up in the eval table)."""
    def wrap(fn):
        fn.__doc__ = doc
        return fn
    return wrap


def dump_json(results: list[Result], path: str) -> None:
    with open(path, "w") as f:
        json.dump([r.__dict__ for r in results], f, indent=1, default=str)
