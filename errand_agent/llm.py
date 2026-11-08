"""Model adapters.

* LiteLLM       — any provider/model through LiteLLM (or a LiteLLM proxy).
* RecordingLLM / ReplayLLM — record a live session to JSONL and replay it offline.
* The scripted policy model used by evals lives in scripted_model.py.

All adapters take a ModelRequest and return a ModelResponse, so the orchestrator
is model-agnostic.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ModelResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    input_tokens: int
    output_tokens: int
    model: str
    raw_content: Any = None  # blocks to append to the in-turn transcript
    cost_usd: float | None = None  # provider-reported cost, when the adapter knows it (LiteLLM)


@dataclass
class ModelRequest:
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    # Structured mirror of exactly what was rendered into `system`/`messages`.
    # The scripted model reads this instead of parsing prose; it contains nothing
    # that is not also in the rendered context.
    view: dict[str, Any] = field(default_factory=dict)


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


class RecordingLLM:
    """Wraps a live model and appends every response to a JSONL cassette."""

    def __init__(self, inner: Any, path: str):
        self.inner, self.path = inner, path

    def complete(self, req: ModelRequest) -> ModelResponse:
        r = self.inner.complete(req)
        with open(self.path, "a") as f:
            f.write(json.dumps({"text": r.text, "tool_calls": [c.__dict__ for c in r.tool_calls],
                                "stop_reason": r.stop_reason, "input_tokens": r.input_tokens,
                                "output_tokens": r.output_tokens, "model": r.model}) + "\n")
        return r


class ReplayLLM:
    """Replays a cassette recorded by RecordingLLM, response by response (offline)."""

    def __init__(self, path: str):
        with open(path) as f:
            self.items = [json.loads(line) for line in f if line.strip()]
        self.i = 0

    def complete(self, req: ModelRequest) -> ModelResponse:
        if self.i >= len(self.items):
            return ModelResponse("(replay exhausted)", [], "end_turn", 0, 0, "replay")
        d = self.items[self.i]
        self.i += 1
        calls = [ToolCall(**c) for c in d["tool_calls"]]
        raw = ([{"type": "text", "text": d["text"]}] if d["text"] else []) + \
              [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
        return ModelResponse(d["text"], calls, d["stop_reason"], d["input_tokens"], d["output_tokens"],
                             d["model"], raw)


# ------------------------------------------------------------------ LiteLLM
def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert the agent's internal transcript (text / tool_use / tool_result blocks)
    into the OpenAI chat format LiteLLM expects."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        blocks = [b if isinstance(b, dict) else b.model_dump() for b in content]
        if m["role"] == "assistant":
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in blocks if b.get("type") == "tool_use"]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:
            for b in blocks:
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                "content": c if isinstance(c, str) else json.dumps(c)})
                elif b.get("type") == "text":
                    out.append({"role": "user", "content": b["text"]})
    return out


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t["input_schema"]}} for t in tools]


class LiteLLM:
    """Any model LiteLLM supports (ERRAND_MODEL=provider/model), or a LiteLLM proxy model alias
    with LITELLM_API_BASE / LITELLM_API_KEY."""

    def __init__(self, model: str | None = None, timeout_s: float = 60.0, completion_fn: Any = None):
        import litellm  # imported lazily so offline evals don't need it

        self.litellm = litellm
        self.completion = completion_fn or litellm.completion
        self.model = model or os.environ.get("ERRAND_MODEL")
        if not self.model:
            raise RuntimeError("Set ERRAND_MODEL to a LiteLLM model id (provider/model) or your proxy's model alias")
        self.timeout_s = timeout_s
        self.api_base = os.environ.get("LITELLM_API_BASE") or None
        self.api_key = os.environ.get("LITELLM_API_KEY") or None
        if self.api_base and not self.model.startswith(("openai/", "litellm_proxy/")):
            # With a proxy, ERRAND_MODEL is the proxy's alias (e.g. "azure/gpt-5.5"). Send it to the proxy's
            # OpenAI-compatible endpoint instead of letting LiteLLM call that provider directly.
            self.model = f"openai/{self.model}"

    def complete(self, req: ModelRequest) -> ModelResponse:
        kwargs: dict[str, Any] = dict(model=self.model, messages=to_openai_messages(req.system, req.messages),
                                      tools=to_openai_tools(req.tools), max_tokens=8000, timeout=self.timeout_s,
                                      num_retries=2)
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        resp = self.completion(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        text = msg.content or ""
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": tc.function.arguments}  # surfaced to the model as invalid_arguments
            calls.append(ToolCall(tc.id, tc.function.name, args if isinstance(args, dict) else {}))
        raw = ([{"type": "text", "text": text}] if text else []) + \
              [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
        usage = getattr(resp, "usage", None)
        try:
            cost = float(self.litellm.completion_cost(completion_response=resp))
        except Exception:
            cost = None
        return ModelResponse(text=text, tool_calls=calls,
                             stop_reason="tool_use" if calls else (choice.finish_reason or "end_turn"),
                             input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                             output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                             model=getattr(resp, "model", None) or self.model, raw_content=raw, cost_usd=cost)
