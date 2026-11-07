"""Model adapters.

* AnthropicLLM  — real Claude via the official SDK (live runs, demo).
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


class AnthropicLLM:
    def __init__(self, model: str | None = None, effort: str = "medium", timeout_s: float = 60.0,
                 client: Any = None):
        import anthropic  # imported lazily so offline evals don't need credentials

        self.anthropic = anthropic
        self.client = client or anthropic.Anthropic(timeout=timeout_s, max_retries=2)
        self.model = model or os.environ.get("ERRAND_MODEL", "claude-opus-5")
        self.effort = effort
        self.use_fallbacks = os.environ.get("ERRAND_FALLBACKS", "1") == "1"

    def complete(self, req: ModelRequest) -> ModelResponse:
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=8000, system=req.system, messages=req.messages, tools=req.tools,
            thinking={"type": "adaptive"}, output_config={"effort": self.effort},
        )
        if self.use_fallbacks:
            try:
                # Server-side refusal fallbacks: if a safety classifier declines, the API retries on a
                # fallback model instead of returning stop_reason="refusal".
                resp = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            except self.anthropic.BadRequestError:
                self.use_fallbacks = False  # account/model doesn't support it: degrade once, keep going
                resp = self.client.messages.create(**kwargs)
        else:
            resp = self.client.messages.create(**kwargs)
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason == "refusal":
            text = text or "I can't help with that request."
            calls = []
        return ModelResponse(text=text, tool_calls=calls, stop_reason=resp.stop_reason or "end_turn",
                             input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                             model=resp.model, raw_content=resp.content)


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
