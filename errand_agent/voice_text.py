"""Turn chat replies (markdown, status lines, approval block) into natural speech.

Pure functions, no LiveKit dependency, so they are unit-tested offline."""
from __future__ import annotations

import re

DAYS = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday", "Fri": "Friday",
        "Sat": "Saturday", "Sun": "Sunday"}
MONTHS = {"Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April", "May": "May", "Jun": "June",
          "Jul": "July", "Aug": "August", "Sep": "September", "Oct": "October", "Nov": "November", "Dec": "December"}
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿️]")


def _dates(t: str) -> str:
    # "Mon 09 Nov, 09:00" / "Mon 09 Nov at 09:00" -> "Monday 9 November at 9 AM"
    def day(m: re.Match) -> str:
        return f"{DAYS[m.group(1)]} {int(m.group(2))} {MONTHS[m.group(3)]}"
    t = re.sub(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b,?(\s+at)?(?=\s+\d{1,2}:\d{2})",
               lambda m: day(m) + " at", t)
    t = re.sub(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", day, t)

    def clock(m: re.Match) -> str:
        h, mi = int(m.group(1)), int(m.group(2))
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12} {suffix}" if mi == 0 else f"{h12}:{mi:02d} {suffix}"
    t = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", clock, t)
    t = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})T", r"\1-\2-\3 ", t)
    return t


def speakable(text: str) -> str:
    out = []
    for block in re.split(r"\n{2,}", text.strip()):
        b = block.strip()
        if not b:
            continue
        ap = re.match(r"🔒 Approval needed \[\w+\]:\s*(.*?)(?:\nReply.*)?$", b, re.S)
        if ap:
            body = re.sub(r"\.?\s*Slot held until [^.]*\.?", ".", ap.group(1))
            out.append(f"To confirm: {body.strip()} Say yes to confirm, or tell me what to change.")
            continue
        if b.startswith("✅"):
            m = re.match(r"✅\s*Verified: booking \*\*(BK-\d+)\*\* confirmed", b)
            if m:
                out.append(f"Booking {m.group(1)} is confirmed with the provider.")
                continue
            b = re.sub(r"^✅\s*Verified:\s*", "Confirmed with the provider: ", b)
            b = re.sub(r"\s*\(checked via [^)]*\)", "", b)
        b = re.sub(r"^❌\s*Not completed \[\w+\]:\s*", "That didn't go through: ", b)
        out.append(b)
    t = "\n".join(out)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"\s*\((?:[A-Z]{3}-[A-Z]{3}-\d+|P[0-9A-F]{5})\)", "", t)  # provider / proposal ids
    t = re.sub(r"\[P[0-9A-F]{5}\]", "", t)
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)
    t = re.sub(r"₹\s?([\d,]+)", lambda m: f"{m.group(1).replace(',', '')} rupees", t)
    t = re.sub(r"\bat (\d+ rupees)", r"for \1", t)
    t = re.sub(r"★\s?([\d.]+)", r"rated \1", t)
    t = re.sub(r"\b(\w+)_(\w+)\b", r"\1 \2", t)
    t = _dates(t)
    t = EMOJI.sub("", t)
    t = t.replace("—", ",").replace(" ,", ",")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s*\n\s*", " ", t)
    return t.strip()
