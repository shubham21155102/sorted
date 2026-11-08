"""Terminal chat.

    python -m errand_agent.cli                 # LiteLLM if ERRAND_MODEL is set, else scripted
    python -m errand_agent.cli --model scripted --fresh

Commands: /events (poll provider events), /cancel BK-xxxx (simulate provider cancellation),
/memories, /state, /quit
"""
from __future__ import annotations

import argparse
import json

from .app import build


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="litellm | scripted | replay:<cassette.jsonl>")
    ap.add_argument("--data", default="run")
    ap.add_argument("--fresh", action="store_true", help="wipe local state and reseed")
    args = ap.parse_args()
    agent = build(args.data, args.model, fresh=args.fresh)
    print(f"Errand agent ({agent.model_name}). Trace: {args.data}/trace.jsonl. /quit to exit.")
    for m in agent.outbox:
        print(f"\nagent> {m}")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/events":
            for m in agent.process_events() or ["(no new events)"]:
                print(f"\nagent> {m}")
            continue
        if text.startswith("/cancel "):
            agent.world.simulate_provider_cancellation(text.split()[1])
            for m in agent.process_events():
                print(f"\nagent> {m}")
            continue
        if text == "/memories":
            print(json.dumps(agent.memory.describe(), indent=1))
            continue
        if text == "/state":
            print(json.dumps(agent.snapshot()["state"], indent=1, default=str))
            continue
        print(f"\nagent> {agent.handle_user_message(text)}")


if __name__ == "__main__":
    main()
