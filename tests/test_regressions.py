"""Regression tests for real bugs hit while building (see docs/BUGS.md)."""
from errand_agent.agent import ErrandAgent, Limits
from errand_agent.clock import FakeClock
from errand_agent.context import ContextBuilder
from errand_agent.memory import MemoryManager
from errand_agent.mock_env import MockWorld, ScenarioConfig
from errand_agent.prompts import SYSTEM_RULES
from errand_agent.scripted_model import Misbehaviour, ScriptedModel
from errand_agent.seed import seed_all
from errand_agent.store import Store
from errand_agent.tools import TOOLS
from errand_agent.tracing import sanitize


def _agent(**kw):
    clock = FakeClock()
    world = MockWorld(kw.pop("config", ScenarioConfig()), clock)
    return ErrandAgent(llm=kw.pop("llm", ScriptedModel()), world=world, store=Store(), clock=clock, **kw), world


def test_bug1_proactive_event_does_not_crash_poller():
    """Bug 1: start_run(kind, event=...) collided with Tracer.emit's positional `event`
    parameter, so every provider-cancellation event raised TypeError in the poller."""
    agent, world = _agent()
    world.simulate_provider_cancellation("BK-1002")
    msgs = agent.process_events()
    assert len(msgs) == 1 and "Approval needed" in msgs[0]
    assert agent.tracer.of_type("run_start")[-1]["trigger"]["booking_id"] == "BK-1002"


def test_bug2_tool_call_limit_ends_the_run():
    """Bug 2: hitting max_tool_calls only returned error tool_results and kept looping until the
    model-call cap, burning model calls and reporting the wrong reason."""
    agent, _ = _agent(llm=ScriptedModel(misbehave=Misbehaviour(loop_search=True)),
                      limits=Limits(max_tool_calls=3, max_model_calls=50))
    reply = agent.handle_user_message("find a plumber")
    limits = agent.tracer.of_type("limit")
    assert [e["kind"] for e in limits] == ["tool_calls"]
    assert len(agent.tracer.of_type("model_call")) == 4  # 3 tool calls + the one whose call was refused
    assert "tool-call limit" in reply


def test_bug3_context_total_never_exceeds_budget_after_merge():
    """Bug 3: history was admitted using per-message estimates, but merging adjacent same-role
    messages changed the serialized size, so the final context overshot (4030 > 4000)."""
    store = Store()
    seed_all(store, "s1", "u1", history_pairs=150)
    mm = MemoryManager(store, FakeClock(), "u1")
    for budget in range(3000, 6000, 37):
        cb = ContextBuilder(SYSTEM_RULES, TOOLS, budget_tokens=budget)
        built = cb.build(state_view={"request": {"service": "plumber"}}, notes=["n"], memories_all=mm.all(True),
                         history=store.messages("s1"), turn_messages=[{"role": "user", "content": "find a plumber"}],
                         turn_tool_results=[], user_text="find a plumber")
        assert built.manifest["total_tokens"] <= budget, budget


def test_bug4_trace_sanitizer_keeps_dates():
    """Bug 4: the phone-number redactor matched ISO dates, so every slot in traces read
    '[phone]T09:00' — the trace could no longer prove which slot was approved/booked."""
    out = sanitize({"slot": "2026-11-09T09:00", "note": "call +91-9812345678", "phone": "+91-9812345678"})
    assert out["slot"] == "2026-11-09T09:00"
    assert "[phone]" in out["note"] and out["phone"] == "[redacted]"
