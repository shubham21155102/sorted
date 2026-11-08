"""Admin operations on the provider world (the marketplace database behind the 8 tools).

Used by the admin dashboard (/admin). Every change is written straight to world.sqlite, so the
next search_providers / get_provider / hold_slot call sees it. Booking state changes that a real
marketplace would see from the provider side (job completed, provider cancelled) go through the
same paths the agent relies on: a provider cancellation emits the event the agent reacts to.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from .mock_env import AREAS, BOOK_OUTCOMES, SERVICES, SLOT_TIMES, TOOL_NAMES, WEEKDAYS, MockWorld

EDITABLE = {  # field -> validator returning the stored value (raises ValueError with a message)
    "name": lambda v: _text(v, "Name", 60),
    "price": lambda v: _int(v, "Price", 100, 50000),
    "phone": lambda v: _text(v, "Phone", 20),
    "technician": lambda v: _text(v, "Technician", 60),
    "about": lambda v: _text(v, "About", 600, allow_empty=True),
    "years_experience": lambda v: _int(v, "Experience", 0, 60),
    "warranty_days": lambda v: _int(v, "Warranty", 0, 365),
    "response_mins": lambda v: _int(v, "Response time", 5, 1440),
    "verified": lambda v: int(bool(v)),
    "emergency_service": lambda v: int(bool(v)),
    "active": lambda v: int(bool(v)),
    "languages": lambda v: json.dumps(_langs(v)),
    "work_start": lambda v: _hhmm(v, "Start time"),
    "work_end": lambda v: _hhmm(v, "End time"),
    "days_off": lambda v: json.dumps(_days(v)),
}


def _text(v: Any, label: str, n: int, allow_empty: bool = False) -> str:
    s = str(v or "").strip()
    if not s and not allow_empty:
        raise ValueError(f"{label} is required")
    if len(s) > n:
        raise ValueError(f"{label} is too long (max {n})")
    return s


def _int(v: Any, label: str, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number") from None
    if not lo <= n <= hi:
        raise ValueError(f"{label} must be between {lo} and {hi}")
    return n


def _hhmm(v: Any, label: str) -> str:
    s = str(v or "")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s):
        raise ValueError(f"{label} must be HH:MM")
    return s


def _langs(v: Any) -> list[str]:
    items = v if isinstance(v, list) else str(v or "").split(",")
    out = [str(x).strip()[:20] for x in items if str(x).strip()]
    if not out:
        raise ValueError("Add at least one language")
    return out[:6]


def _days(v: Any) -> list[int]:
    items = v if isinstance(v, list) else []
    out = sorted({int(d) for d in items if str(d).isdigit() and 0 <= int(d) <= 6})
    if len(out) == 7:
        raise ValueError("A provider needs at least one working day")
    return out


def _summary(w: MockWorld, p: dict[str, Any], today: date) -> dict[str, Any]:
    nxt = next(iter(w._slots_between(p["provider_id"], today, today + timedelta(days=13))), None)
    upcoming = w._one("SELECT COUNT(*) n FROM bookings WHERE provider_id = ? AND status = 'confirmed' AND slot > ?",
                      (p["provider_id"], w.clock.now().isoformat()))["n"]
    return {**{k: p[k] for k in ("provider_id", "name", "service", "area", "price", "rating", "rating_count",
                                 "jobs_done", "years_experience", "verified", "technician", "active",
                                 "work_start", "work_end", "response_mins", "warranty_days", "emergency_service")},
            "days_off": [WEEKDAYS[d] for d in p["days_off"]], "next_available": nxt, "upcoming_bookings": upcoming}


# ------------------------------------------------------------------ reads
def overview(w: MockWorld) -> dict[str, Any]:
    now = w.clock.now()
    t, today = now.isoformat(), now.date().isoformat()
    one = lambda sql, a=(): w._one(sql, a)
    by_status = {r["status"]: r["n"] for r in w._q("SELECT status, COUNT(*) n FROM bookings GROUP BY status")}
    return {
        "now": t,
        "providers": one("SELECT COUNT(*) n, SUM(active) active, ROUND(AVG(rating), 2) avg_rating, "
                         "SUM(jobs_done) jobs FROM providers"),
        "by_service": w._q("SELECT service, COUNT(*) providers, SUM(active) active, ROUND(AVG(price)) avg_price, "
                           "ROUND(AVG(rating), 2) avg_rating FROM providers GROUP BY service ORDER BY service"),
        "bookings": {"by_status": by_status,
                     "upcoming": one("SELECT COUNT(*) n, COALESCE(SUM(price), 0) value FROM bookings "
                                     "WHERE status = 'confirmed' AND slot > ?", (t,)),
                     "today": one("SELECT COUNT(*) n FROM bookings WHERE status = 'confirmed' AND substr(slot, 1, 10) = ?",
                                  (today,))["n"],
                     "completed_value": one("SELECT COALESCE(SUM(price), 0) v FROM bookings WHERE status = 'completed'")["v"]},
        "holds_active": one("SELECT COUNT(*) n FROM holds WHERE status = 'active' AND expires_at > ?", (t,))["n"],
        "faults": w.faults(),
        "recent": activity(w, 8),
    }


def providers(w: MockWorld, service: str = "", area: str = "", q: str = "", status: str = "") -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM providers WHERE 1=1", []
    if service:
        sql += " AND service = ?"; args.append(service)
    if area:
        sql += " AND area = ?"; args.append(area)
    if q:
        sql += " AND (name LIKE ? OR provider_id LIKE ? OR technician LIKE ?)"; args += [f"%{q}%"] * 3
    if status in ("active", "inactive"):
        sql += " AND active = ?"; args.append(1 if status == "active" else 0)
    sql += " ORDER BY service, area, CAST(substr(provider_id, 9) AS INTEGER)"
    today = w.clock.now().date()
    return [_summary(w, w._provider_row(r), today) for r in w._q(sql, tuple(args))]


def provider_detail(w: MockWorld, pid: str, start: str | None = None) -> dict[str, Any] | None:
    p = w.provider(pid)
    if not p:
        return None
    today = w.clock.now().date()
    first = date.fromisoformat(start) if start else today
    return {"provider": {**p, "days_off": p["days_off"]},
            "summary": _summary(w, p, today),
            "reviews": w.reviews(pid, include_hidden=True),
            "grid": w.slot_grid(pid, first, 7), "slot_times": SLOT_TIMES, "grid_start": first.isoformat(),
            "bookings": w._q("SELECT * FROM bookings WHERE provider_id = ? ORDER BY slot DESC LIMIT 50", (pid,))}


def bookings(w: MockWorld, status: str = "", q: str = "") -> list[dict[str, Any]]:
    sql, args = "SELECT b.*, p.phone, p.technician FROM bookings b LEFT JOIN providers p USING (provider_id) WHERE 1=1", []
    if status:
        sql += " AND b.status = ?"; args.append(status)
    if q:
        sql += " AND (b.booking_id LIKE ? OR b.provider_name LIKE ? OR b.provider_id LIKE ?)"; args += [f"%{q}%"] * 3
    rows = w._q(sql + " ORDER BY b.slot DESC", tuple(args))
    t = w.clock.now().isoformat()
    for r in rows:
        r["upcoming"] = r["slot"] > t
    return rows


def activity(w: MockWorld, limit: int = 100) -> list[dict[str, Any]]:
    rows = w._q("SELECT id, op, at, data FROM op_log ORDER BY id DESC LIMIT ?", (limit,))
    return [{"id": r["id"], "op": r["op"], "at": r["at"], **json.loads(r["data"] or "{}")} for r in rows]


def meta() -> dict[str, Any]:
    return {"services": list(SERVICES), "areas": list(AREAS), "weekdays": WEEKDAYS, "slot_times": SLOT_TIMES,
            "book_outcomes": BOOK_OUTCOMES, "tools": sorted(TOOL_NAMES), "failure_kinds": ["error", "exception", "malformed"]}


# ------------------------------------------------------------------ writes
def update_provider(w: MockWorld, pid: str, body: dict[str, Any]) -> dict[str, Any]:
    if not w.provider(pid):
        raise LookupError("Provider not found")
    fields = {k: EDITABLE[k](v) for k, v in body.items() if k in EDITABLE}
    if not fields:
        raise ValueError("Nothing to update")
    p = {**w.provider(pid), **{k: v for k, v in fields.items()}}
    if str(p["work_start"]) >= str(p["work_end"]):
        raise ValueError("End time must be after start time")
    w._x(f"UPDATE providers SET {', '.join(k + ' = ?' for k in fields)}, updated_at = ? WHERE provider_id = ?",
         (*fields.values(), w.clock.now().isoformat(), pid))
    w._log("admin_update_provider", provider_id=pid, fields=sorted(fields))
    return w.provider(pid)


def create_provider(w: MockWorld, body: dict[str, Any]) -> dict[str, Any]:
    service, area = body.get("service"), body.get("area")
    if service not in SERVICES:
        raise ValueError("Pick a service")
    if area not in AREAS:
        raise ValueError("Pick an area")
    prefix = f"{SERVICES[service]['code']}-{AREAS[area]}-"
    ids = [int(r["provider_id"][len(prefix):]) for r in
           w._q("SELECT provider_id FROM providers WHERE provider_id LIKE ?", (prefix + "%",))]
    pid = f"{prefix}{max(ids, default=0) + 1}"
    p = {"provider_id": pid, "name": _text(body.get("name"), "Name", 60), "service": service, "area": area,
         "price": _int(body.get("price"), "Price", 100, 50000), "rating": 0.0, "rating_count": 0, "jobs_done": 0,
         "years_experience": _int(body.get("years_experience", 1), "Experience", 0, 60),
         "verified": bool(body.get("verified")), "languages": _langs(body.get("languages") or "English, Hindi"),
         "warranty_days": _int(body.get("warranty_days", 0), "Warranty", 0, 365),
         "emergency_service": bool(body.get("emergency_service")), "phone": _text(body.get("phone"), "Phone", 20),
         "technician": _text(body.get("technician"), "Technician", 60),
         "about": _text(body.get("about", ""), "About", 600, allow_empty=True),
         "response_mins": _int(body.get("response_mins", 60), "Response time", 5, 1440),
         "work_start": _hhmm(body.get("work_start", "09:00"), "Start time"),
         "work_end": _hhmm(body.get("work_end", "19:00"), "End time"),
         "days_off": _days(body.get("days_off", [6])), "active": True}
    if p["work_start"] >= p["work_end"]:
        raise ValueError("End time must be after start time")
    w._insert_provider(p, [])
    w._log("admin_create_provider", provider_id=pid)
    return w.provider(pid)


def set_slot(w: MockWorld, pid: str, slot: str, state: str, reason: str = "") -> None:
    """state: blocked (provider unavailable), open (force-open even outside hours), clear (back to rules)."""
    if not w.provider(pid):
        raise LookupError("Provider not found")
    try:
        dt = datetime.fromisoformat(slot)
    except ValueError:
        raise ValueError("Bad slot") from None
    if slot[11:16] not in SLOT_TIMES or dt <= w.clock.now():
        raise ValueError("Pick a future slot")
    if state == "clear":
        w._x("DELETE FROM slot_overrides WHERE provider_id = ? AND slot = ?", (pid, slot))
    elif state in ("blocked", "open"):
        if state == "blocked" and w._one("SELECT 1 FROM bookings WHERE provider_id = ? AND slot = ? AND status = 'confirmed'",
                                         (pid, slot)):
            raise ValueError("That slot has a confirmed booking; cancel it from Bookings instead")
        w._x("INSERT OR REPLACE INTO slot_overrides VALUES (?,?,?,?)", (pid, slot, state, reason[:120]))
    else:
        raise ValueError("state must be blocked, open or clear")
    w._log("admin_slot", provider_id=pid, slot=slot, state=state)


def add_review(w: MockWorld, pid: str, body: dict[str, Any]) -> dict[str, Any]:
    p = w.provider(pid)
    if not p:
        raise LookupError("Provider not found")
    stars = _int(body.get("rating"), "Stars", 1, 5)
    text = _text(body.get("text"), "Review", 1000)
    author = _text(body.get("author") or "Customer", "Author", 40)
    pos = (w._one("SELECT MAX(position) m FROM reviews WHERE provider_id = ?", (pid,))["m"] or 0) + 1
    w._x("INSERT INTO reviews(provider_id, author, rating, text, created_at, position) VALUES (?,?,?,?,?,?)",
         (pid, author, stars, text, w.clock.now().date().isoformat(), pos))
    n = p["rating_count"]
    new = round((p["rating"] * n + stars) / (n + 1), 1)
    w._x("UPDATE providers SET rating = ?, rating_count = ?, updated_at = ? WHERE provider_id = ?",
         (new, n + 1, w.clock.now().isoformat(), pid))
    w._log("admin_add_review", provider_id=pid, rating=stars)
    return w.provider(pid)


def set_review_hidden(w: MockWorld, review_id: int, hidden: bool) -> None:
    if not w._one("SELECT 1 FROM reviews WHERE review_id = ?", (review_id,)):
        raise LookupError("Review not found")
    w._x("UPDATE reviews SET hidden = ? WHERE review_id = ?", (int(hidden), review_id))
    w._log("admin_review_visibility", review_id=review_id, hidden=bool(hidden))


def complete_booking(w: MockWorld, bid: str) -> dict[str, Any]:
    b = w.booking(bid)
    if not b:
        raise LookupError("Booking not found")
    if b["status"] != "confirmed":
        raise ValueError(f"Only confirmed bookings can be completed (this one is {b['status']})")
    w._x("UPDATE bookings SET status = 'completed', updated_at = ? WHERE booking_id = ?", (w.clock.now().isoformat(), bid))
    w._x("UPDATE providers SET jobs_done = jobs_done + 1 WHERE provider_id = ?", (b["provider_id"],))
    w._log("admin_complete", booking_id=bid)
    return w.booking(bid)


def provider_cancel(w: MockWorld, bid: str, reason: str = "") -> dict[str, Any]:
    b = w.booking(bid)
    if not b:
        raise LookupError("Booking not found")
    if b["status"] != "confirmed":
        raise ValueError(f"Only confirmed bookings can be cancelled by the provider (this one is {b['status']})")
    return w.simulate_provider_cancellation(bid, reason or "Technician unavailable")


def queue_fault(w: MockWorld, body: dict[str, Any]) -> list[dict[str, Any]]:
    kind = body.get("kind")
    if kind == "book_outcome":
        if body.get("value") not in BOOK_OUTCOMES:
            raise ValueError("Unknown booking outcome")
        w.queue_fault("book_outcome", body["value"])
    elif kind == "price_drift":
        w.queue_fault("price_drift", _int(body.get("value"), "Price change", -5000, 5000))
    elif kind == "tool_failure":
        if body.get("tool") not in TOOL_NAMES or body.get("value") not in ("error", "exception", "malformed"):
            raise ValueError("Pick a tool and a failure kind")
        w.queue_fault("tool_failure", body["value"], tool=body["tool"])
    else:
        raise ValueError("Unknown fault kind")
    w._log("admin_fault", kind=kind, value=body.get("value"), tool=body.get("tool"))
    return w.faults()
