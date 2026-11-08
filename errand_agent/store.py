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
CREATE TABLE IF NOT EXISTS phone_calls (
  call_id TEXT PRIMARY KEY, session_id TEXT, provider TEXT, direction TEXT, caller TEXT, started_at TEXT,
  ended_at TEXT);
CREATE TABLE IF NOT EXISTS connector_accounts (
  user_id TEXT, connector TEXT, account TEXT, data_json TEXT, settings_json TEXT, updated_at TEXT,
  PRIMARY KEY (user_id, connector));
CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY, user_id TEXT, connector TEXT, redirect_uri TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS calendar_events (
  booking_id TEXT PRIMARY KEY, user_id TEXT, event_id TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS profile (
  user_id TEXT PRIMARY KEY, phone TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, user_id TEXT, title TEXT, created_at TEXT, updated_at TEXT);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")  # lets the web server read while the agent writes
        self.db.executescript(SCHEMA)
        for ddl in ("ALTER TABLE messages ADD COLUMN kind TEXT DEFAULT 'chat'",  # migrations for older DBs
                    "ALTER TABLE messages ADD COLUMN model TEXT",
                    "ALTER TABLE sessions ADD COLUMN model TEXT"):
            try:
                self.db.execute(ddl)
            except sqlite3.OperationalError:
                pass

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ messages
    def add_message(self, session_id: str, role: str, content: str, ts: str, user_id: str = "",
                    kind: str = "chat", model: str | None = None) -> None:
        """kind: chat | proactive (sent without a user prompt) | recovery (sent after a restart).
        model: which model produced an assistant message."""
        self.db.execute("INSERT INTO messages(session_id, user_id, role, content, ts, kind, model) "
                        "VALUES (?,?,?,?,?,?,?)", (session_id, user_id, role, content, ts, kind, model))
        title = " ".join(content.split())[:60] if role == "user" else None
        self.db.execute("INSERT OR IGNORE INTO sessions(id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                        (session_id, user_id, title, ts, ts))
        self.db.execute("UPDATE sessions SET updated_at=?, title=COALESCE(title, ?) WHERE id=?", (ts, title, session_id))

    # ------------------------------------------------------------ sessions
    def create_session(self, session_id: str, user_id: str, ts: str, title: str | None = None,
                       model: str | None = None) -> None:
        self.db.execute("INSERT OR IGNORE INTO sessions(id, user_id, title, created_at, updated_at, model) "
                        "VALUES (?,?,?,?,?,?)", (session_id, user_id, title, ts, ts, model))

    def session(self, session_id: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(r) if r else None

    def set_session_model(self, session_id: str, model: str) -> None:
        self.db.execute("UPDATE sessions SET model=? WHERE id=?", (model, session_id))

    def sessions(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS n "
                               "FROM sessions s WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def rename_session(self, session_id: str, title: str) -> None:
        self.db.execute("UPDATE sessions SET title=? WHERE id=?", (title, session_id))

    def delete_session(self, session_id: str) -> None:
        """Deletes the chat itself. The approval ledger is kept: it's the audit trail of side effects."""
        for t in ("messages", "workflow", "sessions"):
            self.db.execute(f"DELETE FROM {t} WHERE {'id' if t == 'sessions' else 'session_id'}=?", (session_id,))

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
        rows = self.db.execute("SELECT role, content, ts, kind, model FROM messages WHERE session_id=? ORDER BY id",
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

    # ----------------------------------------------------------- connectors
    def connector_account(self, user_id: str, connector: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM connector_accounts WHERE user_id=? AND connector=?",
                            (user_id, connector)).fetchone()
        if not r:
            return None
        return {"account": r["account"], "data": json.loads(r["data_json"] or "{}"),
                "settings": json.loads(r["settings_json"] or "{}"), "updated_at": r["updated_at"]}

    def save_connector_account(self, user_id: str, connector: str, account: str, data: dict[str, Any],
                               settings: dict[str, Any] | None, ts: str) -> None:
        self.db.execute("INSERT INTO connector_accounts(user_id, connector, account, data_json, settings_json, "
                        "updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(user_id, connector) DO UPDATE SET "
                        "account=excluded.account, data_json=excluded.data_json, settings_json=excluded.settings_json, "
                        "updated_at=excluded.updated_at",
                        (user_id, connector, account, json.dumps(data), json.dumps(settings or {}), ts))

    def delete_connector_account(self, user_id: str, connector: str) -> None:
        self.db.execute("DELETE FROM connector_accounts WHERE user_id=? AND connector=?", (user_id, connector))

    def save_oauth_state(self, state: str, user_id: str, connector: str, redirect_uri: str, ts: str) -> None:
        self.db.execute("INSERT INTO oauth_states(state, user_id, connector, redirect_uri, created_at) VALUES (?,?,?,?,?)",
                        (state, user_id, connector, redirect_uri, ts))

    def pop_oauth_state(self, state: str) -> dict[str, Any] | None:
        """One-time use: the state is deleted as it is read."""
        r = self.db.execute("SELECT * FROM oauth_states WHERE state=?", (state,)).fetchone()
        if r:
            self.db.execute("DELETE FROM oauth_states WHERE state=?", (state,))
        return dict(r) if r else None

    def calendar_event(self, booking_id: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM calendar_events WHERE booking_id=?", (booking_id,)).fetchone()
        return dict(r) if r else None

    def save_calendar_event(self, booking_id: str, user_id: str, event_id: str, ts: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO calendar_events(booking_id, user_id, event_id, updated_at) "
                        "VALUES (?,?,?,?)", (booking_id, user_id, event_id, ts))

    def delete_calendar_event(self, booking_id: str) -> None:
        self.db.execute("DELETE FROM calendar_events WHERE booking_id=?", (booking_id,))

    # ------------------------------------------------------------- profile
    def profile(self, user_id: str) -> dict[str, Any]:
        r = self.db.execute("SELECT * FROM profile WHERE user_id=?", (user_id,)).fetchone()
        return dict(r) if r else {"user_id": user_id, "phone": None}

    def set_phone(self, user_id: str, phone: str | None, ts: str) -> None:
        self.db.execute("INSERT INTO profile(user_id, phone, updated_at) VALUES (?,?,?) ON CONFLICT(user_id) "
                        "DO UPDATE SET phone=excluded.phone, updated_at=excluded.updated_at", (user_id, phone, ts))

    # ---------------------------------------------------------- phone calls
    def phone_call(self, call_id: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM phone_calls WHERE call_id=?", (call_id,)).fetchone()
        return dict(r) if r else None

    def add_phone_call(self, call_id: str, session_id: str, provider: str, direction: str, caller: str,
                       ts: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO phone_calls(call_id, session_id, provider, direction, caller, "
                        "started_at) VALUES (?,?,?,?,?,?)", (call_id, session_id, provider, direction, caller, ts))

    def end_phone_call(self, call_id: str, ts: str) -> bool:
        """Mark a call ended; False if it was already ended (webhooks can be retried)."""
        cur = self.db.execute("UPDATE phone_calls SET ended_at=? WHERE call_id=? AND ended_at IS NULL", (ts, call_id))
        return cur.rowcount > 0

    # -------------------------------------------------------------- events
    def event_processed(self, event_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM processed_events WHERE event_id=?", (event_id,)).fetchone() is not None

    def mark_event(self, event_id: str, user_id: str, decision: str, at: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO processed_events(event_id, user_id, decision, at) VALUES (?,?,?,?)",
                        (event_id, user_id, decision, at))
