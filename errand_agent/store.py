"""Durable agent-side state (SQLite): conversation, workflow state, approval
proposals (which double as the idempotency ledger), memories, processed events.

Every write is committed immediately, so a crash at any point leaves a state the
next process can recover from."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, user_id TEXT, role TEXT, content TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS workflow (
  session_id TEXT PRIMARY KEY, state_json TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, session_id TEXT, user_id TEXT, action TEXT, material_json TEXT,
  display_json TEXT, status TEXT, idempotency_key TEXT, presented_turn INTEGER,
  created_at TEXT, approved_at TEXT, result_json TEXT, status_reason TEXT);
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, user_id TEXT, key TEXT, value TEXT, scope TEXT, source TEXT,
  confirmed INTEGER, status TEXT, evidence TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS processed_events (
  event_id TEXT PRIMARY KEY, user_id TEXT, decision TEXT, at TEXT);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ messages
    def add_message(self, session_id: str, role: str, content: str, ts: str, user_id: str = "") -> None:
        self.db.execute("INSERT INTO messages(session_id, user_id, role, content, ts) VALUES (?,?,?,?,?)",
                        (session_id, user_id, role, content, ts))

    def redact_messages(self, user_id: str, value: str, replacement: str = "[forgotten]") -> int:
        """Remove a forgotten fact from every stored message of this user."""
        rows = self.db.execute("SELECT id, content FROM messages WHERE user_id=?", (user_id,)).fetchall()
        n = 0
        pat = re.compile(re.escape(value), re.IGNORECASE)
        for r in rows:
            if pat.search(r["content"]):
                self.db.execute("UPDATE messages SET content=? WHERE id=?", (pat.sub(replacement, r["content"]), r["id"]))
                n += 1
        return n

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT role, content, ts FROM messages WHERE session_id=? ORDER BY id",
                               (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def turn_count(self, session_id: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND role='assistant'",
                               (session_id,)).fetchone()[0]

    # ------------------------------------------------------------ workflow
    def load_workflow(self, session_id: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT state_json FROM workflow WHERE session_id=?", (session_id,)).fetchone()
        return json.loads(r[0]) if r else None

    def save_workflow(self, session_id: str, state: dict[str, Any], ts: str) -> None:
        self.db.execute("INSERT INTO workflow(session_id, state_json, updated_at) VALUES (?,?,?) "
                        "ON CONFLICT(session_id) DO UPDATE SET state_json=excluded.state_json, "
                        "updated_at=excluded.updated_at", (session_id, json.dumps(state), ts))

    # ----------------------------------------------------------- proposals
    def insert_proposal(self, p: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO proposals(id, session_id, user_id, action, material_json, display_json, status, "
            "idempotency_key, presented_turn, created_at, approved_at, result_json, status_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["id"], p["session_id"], p["user_id"], p["action"], json.dumps(p["material"]),
             json.dumps(p["display"]), p["status"], p["idempotency_key"], p.get("presented_turn"),
             p["created_at"], p.get("approved_at"), json.dumps(p.get("result")), p.get("status_reason")))

    def update_proposal(self, pid: str, **fields: Any) -> None:
        cols, vals = [], []
        for k, v in fields.items():
            if k in ("result",):
                k, v = "result_json", json.dumps(v)
            cols.append(f"{k}=?")
            vals.append(v)
        vals.append(pid)
        self.db.execute(f"UPDATE proposals SET {', '.join(cols)} WHERE id=?", vals)

    @staticmethod
    def _proposal_row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["material"] = json.loads(d.pop("material_json"))
        d["display"] = json.loads(d.pop("display_json"))
        d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
        return d

    def get_proposal(self, pid: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        return self._proposal_row(r) if r else None

    def proposals(self, session_id: str | None = None, status: str | None = None,
                  user_id: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM proposals WHERE 1=1", []
        if session_id:
            q += " AND session_id=?"
            args.append(session_id)
        if user_id:
            q += " AND user_id=?"
            args.append(user_id)
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY created_at, rowid"
        return [self._proposal_row(r) for r in self.db.execute(q, args).fetchall()]

    # ------------------------------------------------------------ memories
    def upsert_memory(self, m: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO memories(id, user_id, key, value, scope, source, confirmed, status, evidence, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "value=excluded.value, scope=excluded.scope, source=excluded.source, confirmed=excluded.confirmed, "
            "status=excluded.status, evidence=excluded.evidence, updated_at=excluded.updated_at",
            (m["id"], m["user_id"], m["key"], m["value"], m["scope"], m["source"], int(m["confirmed"]),
             m["status"], m.get("evidence"), m["created_at"], m["updated_at"]))

    def memories(self, user_id: str, status: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM memories WHERE user_id=?", [user_id]
        if status:
            q += " AND status=?"
            args.append(status)
        rows = self.db.execute(q + " ORDER BY created_at, id", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["confirmed"] = bool(d["confirmed"])
            out.append(d)
        return out

    # -------------------------------------------------------------- events
    def event_processed(self, event_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM processed_events WHERE event_id=?", (event_id,)).fetchone() is not None

    def mark_event(self, event_id: str, user_id: str, decision: str, at: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO processed_events(event_id, user_id, decision, at) VALUES (?,?,?,?)",
                        (event_id, user_id, decision, at))
