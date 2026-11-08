"""Build a ready-to-use agent (shared by the CLI and the web demo)."""
from __future__ import annotations

import os
from typing import Any

from .agent import ErrandAgent, Limits
from .clock import SystemClock
from .mock_env import MockWorld, ScenarioConfig
from .seed import seed_all
from .store import Store


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win; empty values are skipped."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value and key.strip() not in os.environ:
                os.environ[key.strip()] = value


load_dotenv()


def make_llm(kind: str) -> Any:
    if kind == "scripted":
        from .scripted_model import ScriptedModel
        return ScriptedModel()
    if kind.startswith("replay:"):
        from .llm import ReplayLLM
        return ReplayLLM(kind.split(":", 1)[1])
    from .llm import AnthropicLLM, LiteLLM, RecordingLLM
    llm = LiteLLM() if kind == "litellm" else AnthropicLLM(model=os.environ.get("ERRAND_MODEL"))
    rec = os.environ.get("ERRAND_RECORD")
    return RecordingLLM(llm, rec) if rec else llm


def default_model_kind() -> str:
    return os.environ.get("ERRAND_LLM") or ("live" if os.environ.get("ANTHROPIC_API_KEY") else "scripted")


def build(data_dir: str = "run", model: str | None = None, session_id: str = "s1", user_id: str = "u1",
          fresh: bool = False, config: ScenarioConfig | None = None, context_budget: int = 12000) -> ErrandAgent:
    os.makedirs(data_dir, exist_ok=True)
    db, world_path, trace = (os.path.join(data_dir, f) for f in ("agent.sqlite", "world.json", "trace.jsonl"))
    if fresh:
        for p in (db, world_path, trace):
            if os.path.exists(p):
                os.remove(p)
    new_db = not os.path.exists(db)
    store = Store(db)
    if new_db:
        seed_all(store, session_id, user_id, history_pairs=150)
    clock = SystemClock()
    world = MockWorld(config or ScenarioConfig(), clock, world_path)
    kind = model or default_model_kind()
    return ErrandAgent(llm=make_llm(kind), world=world, store=store, user_id=user_id, session_id=session_id,
                       clock=clock, trace_path=trace, limits=Limits(max_wall_seconds=180),
                       context_budget=context_budget, model_name=kind)
