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
for _secret in ("livekit.env", "sarvam.env", "telephony.env", "google.env"):  # gitignored credential files
    load_dotenv(os.path.join("secrets", _secret))


def make_llm(kind: str) -> Any:
    if kind == "scripted":
        from .scripted_model import ScriptedModel
        return ScriptedModel()
    if kind.startswith("replay:"):
        from .llm import ReplayLLM
        return ReplayLLM(kind.split(":", 1)[1])
    if kind != "litellm":
        raise ValueError(f"unknown model kind {kind!r}: use litellm | scripted | replay:<cassette.jsonl>")
    from .llm import LiteLLM, RecordingLLM
    llm = LiteLLM()
    rec = os.environ.get("ERRAND_RECORD")
    return RecordingLLM(llm, rec) if rec else llm


def default_model_kind() -> str:
    """ERRAND_LLM wins; otherwise LiteLLM if a model is configured, else the offline scripted model."""
    if os.environ.get("ERRAND_LLM"):
        return os.environ["ERRAND_LLM"]
    return "litellm" if os.environ.get("ERRAND_MODEL") else "scripted"


def build(data_dir: str = "run", model: str | None = None, session_id: str = "s1", user_id: str = "u1",
          fresh: bool = False, config: ScenarioConfig | None = None, context_budget: int = 12000) -> ErrandAgent:
    os.makedirs(data_dir, exist_ok=True)
    db, world_path, trace = (os.path.join(data_dir, f) for f in ("agent.sqlite", "world.sqlite", "trace.jsonl"))
    if fresh:
        for p in (db, world_path, trace):
            if os.path.exists(p):
                os.remove(p)
    new_db = not os.path.exists(db)
    store = Store(db)
    if new_db:
        seed_store(store, user_id)
    clock = SystemClock()
    world = MockWorld(config or ScenarioConfig(), clock, world_path)
    kind = model or default_model_kind()
    return make_agent(store, world, make_llm(kind), kind, session_id, user_id, trace, context_budget)


ARCHIVE_SESSION = "archive"


def seed_store(store: Store, user_id: str = "u1") -> None:
    """54 memories plus a long prior conversation, kept in its own 'Earlier conversations' chat."""
    seed_all(store, ARCHIVE_SESSION, user_id, history_pairs=150)
    store.rename_session(ARCHIVE_SESSION, "Earlier conversations (imported)")


def make_agent(store: Store, world: MockWorld, llm: Any, kind: str, session_id: str, user_id: str,
               trace_path: str, context_budget: int = 12000, connectors: Any = None) -> ErrandAgent:
    return ErrandAgent(llm=llm, world=world, store=store, user_id=user_id, session_id=session_id,
                       clock=world.clock, trace_path=trace_path, limits=Limits(max_wall_seconds=180),
                       context_budget=context_budget, model_name=kind, connectors=connectors)
