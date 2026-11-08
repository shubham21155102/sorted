"""LiteLLM adapter: message/tool conversion and a full agent turn with a stubbed completion()."""
import json
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("litellm")

from errand_agent.agent import ErrandAgent
from errand_agent.clock import FakeClock
from errand_agent.llm import LiteLLM, to_openai_messages, to_openai_tools
from errand_agent.mock_env import MockWorld, ScenarioConfig
from errand_agent.store import Store
from errand_agent.tools import TOOLS


def _resp(content=None, tool_calls=(), finish="stop"):
    calls = [NS(id=i, type="function", function=NS(name=n, arguments=json.dumps(a))) for i, n, a in tool_calls]
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls or None), finish_reason=finish)],
              usage=NS(prompt_tokens=900, completion_tokens=40), model="openai/gpt-4.1")


def test_conversion_round_trip():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "checking"},
                                              {"type": "tool_use", "id": "c1", "name": "list_bookings", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "{\"ok\": 1}"}]}]
    out = to_openai_messages("SYS", msgs)
    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[2]["tool_calls"][0]["function"] == {"name": "list_bookings", "arguments": "{}"}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "{\"ok\": 1}"}
    tools = to_openai_tools(TOOLS)
    assert tools[0]["type"] == "function" and "parameters" in tools[0]["function"]


def test_agent_turn_through_litellm():
    seen = []
    script = [
        _resp(tool_calls=[("c1", "update_request", {"service": "plumber", "area": "indiranagar",
                                                     "date_start": "2026-11-10", "date_end": "2026-11-12",
                                                     "budget_max": 1500})], finish="tool_calls"),
        _resp(tool_calls=[("c2", "book", {"hold_id": "H-9999"})], finish="tool_calls"),  # must be blocked
        _resp(content="Noted — shall I search?"),
    ]

    def fake_completion(**kw):
        seen.append(kw)
        return script.pop(0)

    clock = FakeClock()
    agent = ErrandAgent(llm=LiteLLM(model="openai/gpt-4.1", completion_fn=fake_completion),
                        world=MockWorld(ScenarioConfig(), clock), store=Store(), clock=clock)
    reply = agent.handle_user_message("plumber in Indiranagar 10-12 Nov under 1500")
    assert seen[0]["model"] == "openai/gpt-4.1" and seen[0]["messages"][0]["role"] == "system"
    assert any(m["role"] == "tool" and m["tool_call_id"] == "c1" for m in seen[1]["messages"])
    assert agent.state.request.service == "plumber"
    assert "Noted" in reply and not agent.world.side_effects()
    assert agent.tracer.of_type("model_call")[0]["input_tokens"] == 900
