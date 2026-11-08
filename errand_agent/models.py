"""Model catalog for the model picker.

Sources, in order:
  1. ERRAND_MODELS (comma-separated ids): an explicit allow-list, in that order.
  2. The LiteLLM proxy's /model_group/info: chat models that support tool calling,
     with per-token prices (used for cost in the trace).
  3. The proxy's /v1/models (no capability info), then ERRAND_MODEL alone.
The offline scripted model is always available.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

SCRIPTED = {"id": "scripted", "label": "Offline (scripted)", "family": "Offline", "provider": "local",
            "input_cost_per_token": 0.0, "output_cost_per_token": 0.0, "max_input_tokens": None}

FAMILIES = [("claude", "Anthropic"), ("anthropic", "Anthropic"), ("gpt", "OpenAI"), ("openai", "OpenAI"),
            ("o3", "OpenAI"), ("o4", "OpenAI"), ("gemini", "Google"), ("gemma", "Google"), ("llama", "Meta"),
            ("meta", "Meta"), ("mistral", "Mistral"), ("magistral", "Mistral"), ("devstral", "Mistral"),
            ("ministral", "Mistral"), ("nova", "Amazon"), ("qwen", "Qwen"), ("deepseek", "DeepSeek"),
            ("grok", "xAI"), ("xai", "xAI"), ("kimi", "Moonshot"), ("nemotron", "NVIDIA"), ("glm", "Zhipu"),
            ("minimax", "MiniMax"), ("palmyra", "Writer")]


def family(model_id: str) -> str:
    low = model_id.lower()
    for key, fam in FAMILIES:
        if key in low:
            return fam
    return "Other"


def label(model_id: str) -> str:
    """'bedrock/us.anthropic.claude-sonnet-5-5' -> 'claude-sonnet-5-5'"""
    tail = model_id.split("/")[-1]
    for prefix in ("us.", "eu.", "global."):
        if tail.startswith(prefix):
            tail = tail[len(prefix):]
    parts = tail.split(".", 1)
    if len(parts) == 2 and parts[0] in ("anthropic", "openai", "meta", "mistral", "amazon", "qwen", "deepseek",
                                        "google", "xai", "moonshotai", "nvidia", "zai", "minimax", "writer"):
        tail = parts[1]
    return tail


def _get(url: str, key: str | None, timeout: float = 10) -> Any:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _entry(mid: str, info: dict[str, Any] | None = None, provider: str | None = None) -> dict[str, Any]:
    info = info or {}
    return {"id": mid, "label": label(mid), "family": family(mid),
            "provider": provider or ",".join(info.get("providers") or []) or mid.split("/")[0],
            "input_cost_per_token": info.get("input_cost_per_token"),
            "output_cost_per_token": info.get("output_cost_per_token"),
            "max_input_tokens": info.get("max_input_tokens")}


class Catalog:
    def __init__(self, ttl_s: float = 600):
        self.ttl_s = ttl_s
        self._models: list[dict[str, Any]] | None = None
        self._at = 0.0
        self.source = "none"
        self.blocked: dict[str, str] = {}  # model id -> reason, learned from failed calls

    def default_model(self) -> str:
        return os.environ.get("ERRAND_MODEL") or "scripted"

    def models(self, refresh: bool = False) -> list[dict[str, Any]]:
        if self._models is not None and not refresh and time.time() - self._at < self.ttl_s:
            return self._models
        base = (os.environ.get("LITELLM_API_BASE") or "").rstrip("/")
        key = os.environ.get("LITELLM_API_KEY")
        found: dict[str, dict[str, Any]] = {}
        if base:
            try:
                for m in _get(f"{base}/model_group/info", key).get("data", []):
                    if m.get("mode") == "chat" and m.get("supports_function_calling"):
                        found[m["model_group"]] = _entry(m["model_group"], m)
                self.source = "proxy:/model_group/info"
            except Exception:
                try:
                    for m in _get(f"{base}/v1/models", key).get("data", []):
                        if m.get("mode", "chat") == "chat":
                            found[m["id"]] = _entry(m["id"])
                    self.source = "proxy:/v1/models"
                except Exception:
                    self.source = "unreachable"
        allow = [m.strip() for m in (os.environ.get("ERRAND_MODELS") or "").split(",") if m.strip()]
        if allow:
            out = [found.get(m) or _entry(m) for m in allow]
            self.source += "+ERRAND_MODELS"
        else:
            out = sorted(found.values(), key=lambda m: (m["family"], m["label"]))
        default = os.environ.get("ERRAND_MODEL")
        if default and default not in {m["id"] for m in out}:
            out.insert(0, _entry(default))
        out.append(SCRIPTED)
        self._models, self._at = out, time.time()
        return out

    def mark_blocked(self, model_id: str, reason: str) -> None:
        self.blocked[model_id] = reason

    def with_status(self, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**m, "unavailable": self.blocked.get(m["id"])} for m in models]

    def get(self, model_id: str) -> dict[str, Any] | None:
        return next((m for m in self.models() if m["id"] == model_id), None)
