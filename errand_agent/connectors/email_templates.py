"""Booking emails: one template, four events (booked / moved / cancelled / cancelled by the provider).

Every email is multipart: a plain-text part and an HTML part. The logo is attached inline
(cid:errand-logo), so it shows without loading remote images. The HTML is table-based with inline styles,
because that is what Gmail, Outlook and Apple Mail all render the same way.
Values come from the provider service, so every one is HTML-escaped.
"""
from __future__ import annotations

import html
import os
from datetime import datetime
from typing import Any
from ..brand import NAME, TAGLINE

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
                   f"{provider} confirmed your visit. {NAME} checked the booking with the provider before sending this.",
                   f"Booked: {service.title()} with {provider} · {new}"),
        "rescheduled": (f"Your {service} visit has moved",
                        f"{provider} confirmed the new time.",
                        f"Moved: {service.title()} with {provider} → {new}"),
        "cancelled": (f"Your {service} visit is cancelled",
                      f"{provider} confirmed the cancellation.",
                      f"Cancelled: {service.title()} with {provider} · {new or old}"),
        "provider_cancelled": (f"{provider} cancelled your visit",
                               f"Sorry about this. Open {NAME} to pick a replacement. Nothing new is booked "
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
    tip = {"booked": f"Need to change it? Just tell {NAME}, for example “move my plumber to Saturday”.",
           "rescheduled": f"Need another change? Just tell {NAME}.",
           "cancelled": f"Want to rebook? Just tell {NAME} when suits you.",
           "provider_cancelled": f"Reply in {NAME} with a day and time that suits you."}[kind]

    # ---------------------------------------------------------------- text
    text_lines = [title, "", lead, ""]
    for label, value, was in rows:
        text_lines.append(f"{label}: {value}" + (f" (was {was})" if was else ""))
    if cal_line:
        text_lines += ["", cal_line]
    text_lines += ["", tip, "", f"— {NAME}"]
    text = "\n".join(text_lines)

    # ---------------------------------------------------------------- html
    return subject, text, _html(kind, b, title=title, lead=lead, subject=subject, cal_line=cal_line, tip=tip)


THEME = {  # kind -> (eyebrow, mark glyph, strong colour, soft background)
    "booked": ("Booking confirmed", "&#10003;", "#1E7A45", "#E8F5EC"),
    "rescheduled": ("Booking moved", "&#8594;", "#1D5FB4", "#E8F0FB"),
    "cancelled": ("Booking cancelled", "&#215;", "#5F5B53", "#F0EEE9"),
    "provider_cancelled": ("Cancelled by provider", "!", "#B3261E", "#FCEBE9"),
}
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _parts(slot: str | None) -> tuple[str, str, str, str] | None:
    """'2026-11-16T11:00' -> ('NOV', '16', 'MON', '11:00 AM')"""
    try:
        d = datetime.fromisoformat(slot or "")
    except ValueError:
        return None
    return f"{d:%b}".upper(), str(d.day), f"{d:%a}".upper(), f"{d:%I:%M %p}".lstrip("0")


def _html(kind: str, b: dict[str, Any], *, title: str, lead: str, subject: str, cal_line: str | None, tip: str) -> str:
    e = html.escape
    eyebrow, glyph, strong, soft = THEME[kind]
    label = BADGES[kind][0]
    first = os.environ.get("ERRAND_USER_NAME", "Shubham").split(" ")[0]  # same default as the server
    app_url = os.environ.get("ERRAND_APP_URL", "http://127.0.0.1:8765")
    service, provider = _service(b), b.get("provider_name") or "your provider"
    greeting = {"booked": f"You're all set{', ' + first if first else ''}.",
                "rescheduled": f"Done{', ' + first if first else ''}: new time below.",
                "cancelled": f"All cancelled{', ' + first if first else ''}.",
                "provider_cancelled": f"Sorry{', ' + first if first else ''}, plans changed."}[kind]
    p = _parts(b.get("slot"))
    month, day, wday, time_ = p if p else ("", "", "", "")
    struck = kind in ("cancelled", "provider_cancelled")
    was = ""
    if kind == "rescheduled" and b.get("old_slot"):
        try:
            o = datetime.fromisoformat(b["old_slot"])
            was = f"{o:%a} {o.day} {o:%b}, {o:%I:%M %p}".replace(", 0", ", ")
        except ValueError:
            was = b["old_slot"]
    area = str(b.get("area") or "").title()
    price = f"₹{int(b['price']):,}" if b.get("price") is not None else "—"
    dim = "opacity:.55;" if struck else ""

    date_tile = f"""<table role="presentation" cellpadding="0" cellspacing="0" width="76" style="width:76px;border-collapse:separate;{dim}">
      <tr><td align="center" style="background:{strong};color:#fff;font:700 11px/1 {FONT};letter-spacing:.12em;padding:7px 0;border-radius:12px 12px 0 0">{e(month)}</td></tr>
      <tr><td align="center" style="background:#fff;border:1px solid {LINE};border-top:0;border-radius:0 0 12px 12px;padding:6px 0 8px">
        <div style="font:800 30px/1.05 {FONT};color:{INK}">{e(day)}</div>
        <div style="font:600 10px/1.4 {FONT};color:{MUTED};letter-spacing:.12em">{e(wday)}</div></td></tr></table>"""
    when_html = (f'<div style="font:700 20px/1.25 {FONT};color:{INK};{"text-decoration:line-through;color:" + MUTED + ";" if struck else ""}">{e(time_)}</div>'
                 + (f'<div style="font:400 13px/1.5 {FONT};color:{MUTED};padding-top:2px">was <span style="text-decoration:line-through">{e(was)}</span></div>' if was else "")
                 + f'<div style="font:600 15px/1.45 {FONT};color:{INK};padding-top:6px">{e(service.capitalize())} &middot; {e(provider)}</div>'
                 + (f'<div style="font:400 14px/1.45 {FONT};color:{MUTED}">{e(f"{wday.title()}, {day} {month.title()}")}</div>' if p else ""))

    def cell(lab: str, val: str, mono: bool = False) -> str:
        fam = "Menlo,Consolas,monospace" if mono else FONT
        return (f'<td width="50%" valign="top" style="padding:14px 0 0">'
                f'<div style="font:600 11px/1.4 {FONT};color:{MUTED};letter-spacing:.08em;text-transform:uppercase">{e(lab)}</div>'
                f'<div style="font:700 16px/1.4 {fam};color:{INK};padding-top:2px">{e(val)}</div></td>')

    cal_html = (f"""<tr><td style="padding:0 28px 4px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr><td style="background:{CREAM};border-radius:12px;padding:12px 14px;font:400 14px/1.5 {FONT};color:{INK}">
        <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{ACCENT};margin-right:8px;vertical-align:1px"></span>{e(cal_line)}</td></tr></table></td></tr>"""
                if cal_line else "")
    button = f"""<table role="presentation" cellpadding="0" cellspacing="0"><tr>
      <td style="border-radius:999px;background:{INK}"><a href="{e(app_url)}" style="display:inline-block;padding:12px 22px;font:600 14px/1 {FONT};color:#fff;text-decoration:none;border-radius:999px">Open {e(NAME)}</a></td></tr></table>"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><meta name="supported-color-schemes" content="light"><title>{e(subject)}</title></head>
<body style="margin:0;padding:0;background:#F4EFE6;-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">{e(lead)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F4EFE6">
<tr><td align="center" style="padding:28px 12px 36px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px">

  <tr><td style="padding:0 6px 18px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td valign="middle" width="44"><img src="cid:{LOGO_CID}" width="40" height="40" alt="{e(NAME)}" style="display:block;border:0;border-radius:11px"></td>
      <td valign="middle" style="padding-left:10px;font:800 20px/1 {FONT};color:{INK};letter-spacing:-.02em">{e(NAME)}</td>
      <td valign="middle" align="right" style="font:500 12px/1.4 {FONT};color:{MUTED}">Booking <span style="font-family:Menlo,Consolas,monospace;color:{INK}">{e(b.get("booking_id") or "")}</span></td>
    </tr></table></td></tr>

  <tr><td style="background:#ffffff;border-radius:22px;border:1px solid {LINE};overflow:hidden">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">

      <tr><td style="background:{soft};padding:28px 28px 24px;border-radius:22px 22px 0 0">
        <table role="presentation" cellpadding="0" cellspacing="0"><tr>
          <td width="44" height="44" align="center" valign="middle" style="width:44px;height:44px;border-radius:50%;background:{strong};color:#fff;font:800 22px/44px {FONT}">{glyph}</td>
          <td style="padding-left:12px"><div style="font:700 12px/1.4 {FONT};color:{strong};letter-spacing:.1em;text-transform:uppercase">{e(eyebrow)}</div>
            <div style="font:500 14px/1.4 {FONT};color:{INK}">{e(greeting)}</div></td>
        </tr></table>
        <h1 style="margin:18px 0 6px;font:800 26px/1.2 {FONT};color:{INK};letter-spacing:-.02em">{e(title)}</h1>
        <p style="margin:0;font:400 15px/1.55 {FONT};color:#4A4740">{e(lead)}</p>
      </td></tr>

      <tr><td style="padding:24px 28px 6px">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
          <td width="76" valign="top">{date_tile}</td>
          <td valign="top" style="padding-left:16px">{when_html}</td>
        </tr></table>
      </td></tr>

      <tr><td style="padding:18px 28px 0"><div style="border-top:2px dashed {LINE};height:0;line-height:0;font-size:0">&nbsp;</div></td></tr>

      <tr><td style="padding:0 28px 18px">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          <tr>{cell("Price", price)}{cell("Booking ID", b.get("booking_id") or "", mono=True)}</tr>
          <tr>{cell("Where", f"{area}, Bangalore" if area else "—")}<td width="50%" valign="top" style="padding:14px 0 0">
            <div style="font:600 11px/1.4 {FONT};color:{MUTED};letter-spacing:.08em;text-transform:uppercase">Status</div>
            <div style="padding-top:4px"><span style="display:inline-block;padding:3px 10px;border-radius:999px;background:{soft};color:{strong};font:700 12px/1.5 {FONT}">{e(label)}</span></div></td></tr>
        </table>
      </td></tr>

      {cal_html}

      <tr><td style="padding:16px 28px 28px">
        <p style="margin:0 0 16px;font:400 14px/1.55 {FONT};color:#4A4740">{e(tip)}</p>
        {button}
      </td></tr>
    </table>
  </td></tr>

  <tr><td align="center" style="padding:22px 18px 0;font:400 12px/1.65 {FONT};color:{MUTED}">
    <div style="font:700 13px/1.4 {FONT};color:{INK};padding-bottom:4px">{e(NAME)} &middot; {e(TAGLINE)}</div>
    {e(NAME)} only books, moves or cancels after you approve the exact details, and checks every change with the provider.<br>
    You get this because email confirmations are on (Connectors in {e(NAME)}).
  </td></tr>

</table></td></tr></table>
</body></html>"""


def logo_bytes() -> bytes | None:
    try:
        with open(LOGO_PATH, "rb") as f:
            return f.read()
    except OSError:
        return None
