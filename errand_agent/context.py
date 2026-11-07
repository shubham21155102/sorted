"""Context assembly under a fixed token budget.

Priority (highest first): system rules + tool schemas, errand state + pending
proposal + application notes, the current turn (user message and in-turn tool
exchange), relevant memories, then as much recent history as fits.

Memory gating happens here and is the only path from the memory table to the
model: forgotten, superseded and unconfirmed-inferred facts are excluded by
construction, and remaining facts are filtered for relevance to the current
errand. Every build returns a manifest (what was included/excluded and why, and
token counts per section) that the agent writes to the trace and tests assert on.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .llm import ModelRequest, estimate_tokens

GLOBAL_RELEVANT_KEYS = ["preferred_area", "earliest_time", "latest_time", "preferred_days", "budget_max",
                        "address_notes", "contact_preference", "language"]
SERVICE_RELEVANT_KEYS = ["technician_preference", "budget_max", "earliest_time", "latest_time", "avoid_provider",
                         "notes"]


class ContextOverflow(Exception):
    pass


@dataclass
class BuiltContext:
    request: ModelRequest
    manifest: dict[str, Any]


def _tok(obj: Any) -> int:
    return estimate_tokens(obj if isinstance(obj, str) else json.dumps(obj, default=str))


def select_memories(memories_all: list[dict[str, Any]], service: str | None, intent: str | None,
                    cap_tokens: int) -> tuple[list[dict[str, Any]], dict[str, str]]:
    included: list[dict[str, Any]] = []
    excluded: dict[str, str] = {}
    candidates = []
    for m in memories_all:
        if m["status"] == "forgotten":
            excluded[m["id"]] = "forgotten"
        elif m["status"] != "active":
            excluded[m["id"]] = m["status"]
        elif m["source"] == "inferred" and not m["confirmed"]:
            excluded[m["id"]] = "inferred_unconfirmed"
        elif m["scope"] == "global" and m["key"] in GLOBAL_RELEVANT_KEYS:
            candidates.append((1, GLOBAL_RELEVANT_KEYS.index(m["key"]), m))
        elif service and m["scope"] == service and m["key"] in SERVICE_RELEVANT_KEYS:
            candidates.append((0, SERVICE_RELEVANT_KEYS.index(m["key"]), m))
        else:
            excluded[m["id"]] = "not_relevant"
    used = 0
    for _, _, m in sorted(candidates, key=lambda c: (c[0], c[1])):
        t = _tok(f"{m['key']} {m['scope']} {m['value']}") + 6
        if used + t > cap_tokens:
            excluded[m["id"]] = "memory_budget"
            continue
        used += t
        included.append(m)
    return included, excluded


def _merge_roles(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in msgs:
        if out and out[-1]["role"] == m["role"] and isinstance(out[-1]["content"], str) and isinstance(m["content"], str):
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
        else:
            out.append(dict(m))
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


class ContextBuilder:
    def __init__(self, system_rules: str, tools: list[dict[str, Any]], budget_tokens: int = 8000,
                 memory_share: float = 0.15, max_history_messages: int = 40):
        self.system_rules = system_rules
        self.tools = tools
        self.budget = budget_tokens
        self.memory_cap = int(budget_tokens * memory_share)
        self.max_history = max_history_messages

    def build(self, *, state_view: dict[str, Any], notes: list[str], memories_all: list[dict[str, Any]],
              history: list[dict[str, str]], turn_messages: list[dict[str, Any]],
              turn_tool_results: list[dict[str, Any]], user_text: str) -> BuiltContext:
        req = state_view.get("request", {})
        mems, excluded = select_memories(memories_all, req.get("service"), req.get("intent"), self.memory_cap)

        def render_system(mem_list: list[dict[str, Any]]) -> str:
            mem_lines = [f"- [{m['id']}] {m['key']} ({m['scope']}, {m['source']}): {m['value']}" for m in mem_list]
            return (self.system_rules
                    + "\n## Errand state (maintained by the application)\n" + json.dumps(state_view, indent=1)
                    + "\n\n## Known user preferences (memory; current and relevant only)\n"
                    + ("\n".join(mem_lines) if mem_lines else "(none relevant)")
                    + "\n\n## Application notes for this turn\n"
                    + ("\n".join(f"- {n}" for n in notes) if notes else "(none)"))

        fixed = _tok(self.tools)
        system = render_system(mems)
        turn = [dict(m) for m in turn_messages]
        required = fixed + _tok(system) + _tok(turn)

        # Compress older in-turn tool results first (keep the latest two intact).
        elided = 0
        if required > self.budget:
            tool_msg_idx = [i for i, m in enumerate(turn) if m["role"] == "user" and isinstance(m["content"], list)]
            for i in tool_msg_idx[:-2]:
                new_blocks = []
                for b in turn[i]["content"]:
                    if b.get("type") == "tool_result":
                        b = {**b, "content": "[elided to fit context budget; call the tool again if needed]"}
                        elided += 1
                    new_blocks.append(b)
                turn[i] = {"role": "user", "content": new_blocks}
            required = fixed + _tok(system) + _tok(turn)
        if required > self.budget:
            # Drop memories before giving up.
            for m in mems:
                excluded[m["id"]] = "context_budget"
            mems = []
            system = render_system(mems)
            required = fixed + _tok(system) + _tok(turn)
        if required > self.budget:
            raise ContextOverflow(f"required context {required} > budget {self.budget}")

        remaining = self.budget - required
        hist_msgs: list[dict[str, str]] = []
        for m in reversed(history[-self.max_history:]):
            t = _tok(m["content"]) + 4
            if t > remaining:
                break
            remaining -= t
            hist_msgs.insert(0, {"role": m["role"], "content": m["content"]})
        messages = _merge_roles(hist_msgs + turn) or turn
        total = fixed + _tok(system) + _tok(messages)
        while total > self.budget and hist_msgs:  # per-message estimates can drift after merging
            hist_msgs.pop(0)
            messages = _merge_roles(hist_msgs + turn) or turn
            total = fixed + _tok(system) + _tok(messages)
        view = {"state": state_view, "memories": [{k: m[k] for k in ("id", "key", "value", "scope", "source")}
                                                  for m in mems],
                "notes": list(notes), "user_text": user_text, "turn_tool_results": turn_tool_results,
                "history": hist_msgs}
        manifest = {
            "budget_tokens": self.budget, "total_tokens": total,
            "sections": {"tools": fixed, "system_and_state": _tok(system), "messages": _tok(messages)},
            "memory_included": [m["id"] for m in mems],
            "memory_excluded": excluded,
            "history_messages_available": len(history),
            "history_messages_included": len(hist_msgs),
            "turn_tool_results_elided": elided,
        }
        return BuiltContext(ModelRequest(system=system, messages=messages, tools=self.tools, view=view), manifest)
