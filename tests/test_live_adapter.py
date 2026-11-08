"""The live Claude adapter, exercised end-to-end through the real SDK with a mocked HTTP transport
(no network, no key). Verifies request shape, tool-use parsing, and the agent loop with it."""
import json

import pytest

anthropic = pytest.importorskip("anthropic")
httpx = pytest.importorskip("httpx2")

from errand_agent.agent import ErrandAgent
from errand_agent.clock import FakeClock
from errand_agent.llm import AnthropicLLM
from errand_agent.mock_env import MockWorld, ScenarioConfig
from errand_agent.store import Store


def _client(responses, seen):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append({"url": str(req.url), "headers": dict(req.headers), "body": json.loads(req.content)})
        return httpx.Response(200, json=responses.pop(0))
    return anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)),
                               max_retries=0)


def _msg(content, stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 80}}


def test_live_adapter_drives_agent_loop():
    seen = []
    responses = [
        _msg([{"type": "text", "text": "Let me note that."},
              {"type": "tool_use", "id": "toolu_1", "name": "update_request",
               "input": {"service": "plumber", "area": "indiranagar", "date_start": "2026-11-10",
                         "date_end": "2026-11-12", "budget_max": 1500}}], "tool_use"),
        _msg([{"type": "tool_use", "id": "toolu_2", "name": "book", "input": {"hold_id": "H-9999"}}], "tool_use"),
        _msg([{"type": "text", "text": "I found options; shall I hold one?"}]),
    ]
    llm = AnthropicLLM(client=_client(responses, seen))
    clock = FakeClock()
    agent = ErrandAgent(llm=llm, world=MockWorld(ScenarioConfig(), clock), store=Store(), clock=clock)
    reply = agent.handle_user_message("plumber in Indiranagar 10-12 Nov under 1500")
    body = seen[0]["body"]
    assert seen[0]["url"].startswith("https://api.anthropic.com/v1/messages")
    assert "server-side-fallback-2026-07-01" in seen[0]["headers"]["anthropic-beta"]
    assert body["model"] == "claude-opus-5" and body["thinking"] == {"type": "adaptive"}
    assert body["fallbacks"] == "default"
    assert {t["name"] for t in body["tools"]} >= {"search_providers", "book", "request_approval"}
    assert body["messages"][0]["role"] == "user"
    # second request carries the tool_result for toolu_1
    tr = [b for m in seen[1]["body"]["messages"] if isinstance(m["content"], list) for b in m["content"]
          if b.get("type") == "tool_result"]
    assert tr and tr[0]["tool_use_id"] == "toolu_1"
    assert agent.state.request.service == "plumber"
    assert "I found options" in reply and not agent.world.side_effects()
    calls = agent.tracer.of_type("model_call")
    assert calls[0]["input_tokens"] == 1200 and calls[0]["cost_usd"] > 0
