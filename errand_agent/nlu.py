"""Small rule-based language understanding used ONLY by the offline scripted model.

The live agent uses the LLM for understanding; this exists so evals run offline
and deterministically against a "scripted model" that still reads natural
language from simulated users."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from .mock_env import AREAS

SERVICE_WORDS = [
    (r"\b(plumb\w*|tap|leak\w*|pipe|drain|faucet)\b", "plumber"),
    (r"\b(electric\w*|wiring|switch|socket|fan|mcb)\b", "electrician"),
    (r"\b(ac|a\.c\.|air ?con\w*)\b", "ac_servicing"),
    (r"\b(clean\w*|deep clean)\b", "cleaning"),
    (r"\b(carpent\w*|furniture|wardrobe)\b", "carpenter"),
    (r"\b(pest|cockroach\w*|termite\w*)\b", "pest_control"),
]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _time(h: str, m: str | None, ampm: str | None) -> str:
    hh = int(h)
    if ampm and ampm.startswith("p") and hh < 12:
        hh += 12
    if ampm and ampm.startswith("a") and hh == 12:
        hh = 0
    return f"{hh:02d}:{int(m or 0):02d}"


def parse_now(now_str: str) -> datetime:
    return datetime.strptime(now_str.split(" ", 1)[1], "%Y-%m-%d %H:%M")


def next_weekday(today: date, name: str, allow_today: bool = False) -> date:
    target = WEEKDAYS.index(name)
    delta = (target - today.weekday()) % 7
    if delta == 0 and not allow_today:
        delta = 7
    return today + timedelta(days=delta)


def extract(text: str, now: datetime) -> dict[str, Any]:
    low = text.lower()
    out: dict[str, Any] = {}
    for pat, svc in SERVICE_WORDS:
        if re.search(pat, low):
            out["service"] = svc
            break
    for area in AREAS:
        if area in low:
            out["area"] = area
            break
    today = now.date()
    if "this week" in low:
        out["date_start"], out["date_end"] = today.isoformat(), (today + timedelta(days=6 - today.weekday())).isoformat()
    elif "next week" in low:
        start = today + timedelta(days=7 - today.weekday())
        out["date_start"], out["date_end"] = start.isoformat(), (start + timedelta(days=6)).isoformat()
    elif "weekend" in low:
        sat = next_weekday(today, "saturday", allow_today=True)
        out["date_start"], out["date_end"] = sat.isoformat(), (sat + timedelta(days=1)).isoformat()
    elif "tomorrow" in low:
        d = today + timedelta(days=1)
        out["date_start"] = out["date_end"] = d.isoformat()
    elif re.search(r"\btoday\b", low):
        out["date_start"] = out["date_end"] = today.isoformat()
    else:
        for wd in WEEKDAYS:
            if re.search(rf"\b{wd}\b", low):
                d = next_weekday(today, wd, allow_today=False)
                out["date_start"] = out["date_end"] = d.isoformat()
                break
    if re.search(r"\bmornings?\b", low):
        out["earliest_time"], out["latest_time"] = "08:00", "12:00"
    elif re.search(r"\bafternoons?\b", low):
        out["earliest_time"], out["latest_time"] = "12:00", "17:00"
    elif re.search(r"\bevenings?\b", low):
        out["earliest_time"], out["latest_time"] = "17:00", "20:00"
    m = re.search(r"(?:not before|after|no earlier than|from)\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", low)
    if m:
        out["earliest_time"] = _time(m.group(1), m.group(2), (m.group(3) or "am").replace(".", ""))
    m = re.search(r"(?:before|no later than|until|by)\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", low)
    if m and not re.search(r"not before", low[max(0, m.start() - 4):m.end()]):
        out["latest_time"] = _time(m.group(1), m.group(2), m.group(3).replace(".", ""))
    m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", low)
    if m:
        out["target_time"] = _time(m.group(1), m.group(2), (m.group(3) or ("pm" if int(m.group(1)) < 7 else "am")).replace(".", ""))
    if re.search(r"\bno (budget|limit)|any price|budget doesn'?t matter|no budget limit", low):
        out["budget_max"] = "none"
    else:
        m = re.search(r"(?:under|below|less than|max(?:imum)?|budget(?: of| is)?|upto|up to|within)\s*(?:rs\.?|inr|₹)?\s*([\d,]{3,6})", low) \
            or re.search(r"₹\s*([\d,]{3,6})", low)
        if m:
            out["budget_max"] = int(m.group(1).replace(",", ""))
    if re.search(r"\bsame\b.*\blast time\b", low):
        out["provider_preference"] = "same as last time"
    return out


def intent(text: str) -> str:
    low = text.lower()
    if re.search(r"did it go through|what('s| is) the status|is it (booked|done|confirmed)|what happened", low):
        return "status"
    if re.search(r"what do you (remember|know)|what have you (saved|stored)|show (me )?my (memories|preferences)", low):
        return "memory_list"
    if re.search(r"\b(forget|delete|erase|remove)\b", low):
        return "memory_forget"
    if re.search(r"\b(move|reschedule|shift|postpone|prepone)\b", low):
        return "reschedule"
    if re.search(r"\bcancel\b", low) and re.search(r"\b(booking|my|the)\b", low):
        return "cancel"
    if re.search(r"(my (home )?area is|i live in|i'?ve moved to|i moved to)", low):
        return "memory_area"
    if re.search(r"\bremember\b", low):
        return "memory_remember"
    if re.search(r"\b(that'?s right|correct|yes,? .*(prefer|preferred|usual))", low) and re.search(r"prefer|usual|right|correct", low):
        return "memory_confirm"
    return "book"


def area_phrase(text: str) -> tuple[str, str] | None:
    low = text.lower()
    for area in AREAS:
        m = re.search(rf"((?:my (?:home )?area is|i live in|i'?ve moved to|i moved to)\s+(?:now\s+)?{area})", low)
        if m:
            start = m.start(1)
            return area, text[start:m.end(1)]
    return None
