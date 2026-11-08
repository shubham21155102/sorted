"""Booking emails: one template, four events (booked / moved / cancelled / cancelled by the provider).

Every email is multipart: a plain-text part and an HTML part. The Errand logo is attached inline
(cid:errand-logo), so it shows without loading remote images. The HTML is table-based with inline styles,
because that is what Gmail, Outlook and Apple Mail all render the same way.
Values come from the provider service, so every one is HTML-escaped.
"""
from __future__ import annotations

import html
import os
from datetime import datetime
from typing import Any

LOGO_CID = "errand-logo"
LOGO_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "brand", "logo-128.png")

ACCENT = "#D9692F"
INK = "#1F1E1C"
MUTED = "#77736B"
LINE = "#ECE6D8"
CREAM = "#FFF8EA"

BADGES = {  # kind -> (label, text colour, background)
    "booked": ("Confirmed", "#1E7A45", "#E3F4E9"),
    "rescheduled": ("Moved", "#1D5FB4", "#E4EEFB"),
    "cancelled": ("Cancelled", "#5F5B53", "#EFEDE8"),
    "provider_cancelled": ("Cancelled by provider", "#B3261E", "#FBE7E5"),
}


def when(slot: str | None) -> str:
    """'2026-11-16T11:00' -> 'Monday, 16 November · 11:00 AM'"""
    if not slot:
        return ""
    try:
        d = datetime.fromisoformat(slot)
    except ValueError:
        return slot
    return f"{d:%A}, {d.day} {d:%B} · {d:%I:%M %p}".replace(" 0", " ")


def _service(b: dict[str, Any]) -> str:
    return str(b.get("service") or "service").replace("_", " ")


def render(kind: str, b: dict[str, Any], *, calendar: str | None = None) -> tuple[str, str, str]:
    """Return (subject, text, html) for one booking event.

    b: booking_id, service, provider_name, slot (and old_slot for a move), price, area.
    calendar: what happened in Google Calendar ("added" / "moved" / "removed" / "marked"), if anything.
    """
    service, provider = _service(b), b.get("provider_name") or "your provider"
    new, old = when(b.get("slot")), when(b.get("old_slot"))
    title, lead, subject = {
        "booked": (f"Your {service} is booked",
                   f"{provider} confirmed your visit. Errand checked the booking with the provider before sending this.",
                   f"Booked: {service.title()} with {provider} · {new}"),
        "rescheduled": (f"Your {service} visit has moved",
                        f"{provider} confirmed the new time.",
                        f"Moved: {service.title()} with {provider} → {new}"),
        "cancelled": (f"Your {service} visit is cancelled",
                      f"{provider} confirmed the cancellation.",
                      f"Cancelled: {service.title()} with {provider} · {new or old}"),
        "provider_cancelled": (f"{provider} cancelled your visit",
                               "Sorry about this. Open Errand to pick a replacement. Nothing new is booked "
                               "without your approval.",
                               f"{provider} cancelled your {service} visit · {new}"),
    }[kind]

    rows: list[tuple[str, str, str | None]] = [("Service", service.capitalize(), None), ("Provider", provider, None)]
    if kind == "rescheduled":
        rows.append(("When", new, old))
    else:
        rows.append(("When", new or old, None))
    if b.get("area"):
        rows.append(("Where", str(b["area"]).title(), None))
    if b.get("price") is not None:
        rows.append(("Price", f"₹{int(b['price']):,}", None))
    rows.append(("Booking ID", b.get("booking_id") or "", None))

    cal_line = {
        "added": "It's on your Google Calendar, with a reminder an hour before.",
        "moved": "Your Google Calendar event has moved to the new time.",
        "removed": "It's been removed from your Google Calendar.",
        "marked": "It's marked as cancelled in your Google Calendar.",
    }.get(calendar or "")
    tip = {"booked": "Need to change it? Just tell Errand, for example “move my plumber to Saturday”.",
           "rescheduled": "Need another change? Just tell Errand.",
           "cancelled": "Want to rebook? Just tell Errand when suits you.",
           "provider_cancelled": "Reply in Errand with a day and time that suits you."}[kind]

    # ---------------------------------------------------------------- text
    text_lines = [title, "", lead, ""]
    for label, value, was in rows:
        text_lines.append(f"{label}: {value}" + (f" (was {was})" if was else ""))
    if cal_line:
        text_lines += ["", cal_line]
    text_lines += ["", tip, "", "— Errand"]
    text = "\n".join(text_lines)

    # ---------------------------------------------------------------- html
    e = html.escape
    label, fg, bg = BADGES[kind]
    row_html = "".join(
        f'<tr><td style="padding:10px 0;border-top:1px solid {LINE};color:{MUTED};font-size:13px;width:110px;'
        f'vertical-align:top">{e(lab)}</td><td style="padding:10px 0;border-top:1px solid {LINE};color:{INK};'
        f'font-size:15px;font-weight:600">'
        + (f'<span style="color:{MUTED};font-weight:400;text-decoration:line-through">{e(was)}</span><br>' if was else "")
        + (f'<span style="font-family:Menlo,Consolas,monospace;font-size:14px">{e(val)}</span>' if lab == "Booking ID"
           else e(val))
        + "</td></tr>"
        for lab, val, was in rows)
    cal_html = (f'<tr><td style="padding:0 28px 6px"><table role="presentation" width="100%" cellpadding="0" '
                f'cellspacing="0" style="background:{CREAM};border-radius:10px"><tr><td style="padding:12px 14px;'
                f'font-size:14px;color:{INK}">📅&nbsp; {e(cal_line)}</td></tr></table></td></tr>') if cal_line else ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{e(subject)}</title></head>
<body style="margin:0;padding:0;background:#F5F3EE;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
<span style="display:none;max-height:0;overflow:hidden;opacity:0">{e(lead)}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F5F3EE">
<tr><td align="center" style="padding:28px 12px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px">
    <tr><td style="padding:0 4px 16px">
      <table role="presentation" cellpadding="0" cellspacing="0"><tr>
        <td style="vertical-align:middle"><img src="cid:{LOGO_CID}" width="36" height="36" alt="Errand"
            style="display:block;border:0;border-radius:9px"></td>
        <td style="vertical-align:middle;padding-left:10px;font-size:18px;font-weight:700;color:{INK};letter-spacing:-.01em">Errand</td>
      </tr></table>
    </td></tr>
    <tr><td style="background:#FFFFFF;border:1px solid {LINE};border-radius:16px;overflow:hidden">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr><td style="height:5px;background:{ACCENT};font-size:0;line-height:0">&nbsp;</td></tr>
        <tr><td style="padding:26px 28px 4px">
          <span style="display:inline-block;padding:4px 10px;border-radius:999px;background:{bg};color:{fg};
                font-size:12px;font-weight:700;letter-spacing:.02em">{e(label)}</span>
          <h1 style="margin:14px 0 8px;font-size:22px;line-height:1.3;color:{INK};font-weight:700">{e(title)}</h1>
          <p style="margin:0 0 18px;font-size:15px;line-height:1.55;color:{MUTED}">{e(lead)}</p>
        </td></tr>
        <tr><td style="padding:0 28px 18px">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{row_html}</table>
        </td></tr>
        {cal_html}
        <tr><td style="padding:12px 28px 26px;font-size:14px;line-height:1.55;color:{INK}">{e(tip)}</td></tr>
      </table>
    </td></tr>
    <tr><td style="padding:16px 8px 0;font-size:12px;line-height:1.6;color:{MUTED};text-align:center">
      Sent by Errand to your own address, because you turned on email confirmations in Connectors.<br>
      Errand only books, moves or cancels after you approve the exact details.
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""
    return subject, text, page


def logo_bytes() -> bytes | None:
    try:
        with open(LOGO_PATH, "rb") as f:
            return f.read()
    except OSError:
        return None
