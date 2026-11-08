"""ConnectorHub: the single place the agent talks to external accounts.

Rules (enforced here, in code):
  * Read-only checks (calendar busy times) may run while the agent searches, and the user's
    events can be read when they ask about their schedule (get_calendar).
  * Writes (calendar events, confirmation emails) happen ONLY after a booking change has been
    executed AND verified with the provider. The model cannot trigger them directly.
  * A connector failure never undoes or hides a verified booking: it is reported as a warning
    status line and traced.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta
from typing import Any, Callable

from ..clock import Clock
from ..store import Store
from . import email_templates
from .google import GoogleClient, GoogleError

TZ = "Asia/Kolkata"
TZ_OFFSET = "+05:30"
EVENT_MINUTES = 60
DEFAULT_SETTINGS = {"calendar_check": True, "calendar_events": True, "email_confirmations": True}


def _human(slot: str) -> str:
    try:
        return datetime.fromisoformat(slot).strftime("%a %d %b, %H:%M")
    except ValueError:
        return slot


class ConnectorHub:
    def __init__(self, store: Store, user_id: str, clock: Clock,
                 google_factory: Callable[[dict[str, Any] | None, Callable], Any] | None = None):
        self.store = store
        self.user_id = user_id
        self.clock = clock
        self._google_factory = google_factory
        self._busy_cache: dict[tuple[str, str], list[tuple[datetime, datetime]]] = {}

    # ------------------------------------------------------------- accounts
    @staticmethod
    def google_configured() -> bool:
        return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))

    def account(self, connector: str = "google") -> dict[str, Any] | None:
        return self.store.connector_account(self.user_id, connector)

    def settings(self) -> dict[str, bool]:
        acct = self.account() or {}
        return {**DEFAULT_SETTINGS, **(acct.get("settings") or {})}

    def update_settings(self, changes: dict[str, Any]) -> dict[str, bool]:
        acct = self.account()
        if not acct:
            raise ValueError("Google is not connected")
        s = {**self.settings(), **{k: bool(v) for k, v in changes.items() if k in DEFAULT_SETTINGS}}
        self.store.save_connector_account(self.user_id, "google", acct["account"], acct["data"], s,
                                          self.clock.now().isoformat())
        return s

    def _client(self, tokens: dict[str, Any] | None = None) -> Any:
        def save(t: dict[str, Any]) -> None:
            acct = self.account()
            if acct:
                self.store.save_connector_account(self.user_id, "google", acct["account"], t, acct.get("settings"),
                                                  self.clock.now().isoformat())
        if self._google_factory:
            return self._google_factory(tokens, save)
        return GoogleClient(os.environ.get("GOOGLE_CLIENT_ID", ""), os.environ.get("GOOGLE_CLIENT_SECRET", ""),
                            tokens, save)

    def google(self) -> Any | None:
        acct = self.account()
        if not acct or not (self._google_factory or self.google_configured()):
            return None
        return self._client(acct["data"])

    def begin_google(self, redirect_uri: str) -> str:
        if not (self._google_factory or self.google_configured()):
            raise ValueError("Google OAuth client not configured: set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET")
        state = secrets.token_urlsafe(24)
        self.store.save_oauth_state(state, self.user_id, "google", redirect_uri, self.clock.now().isoformat())
        return self._client().auth_url(redirect_uri, state)

    def finish_google(self, code: str, state: str) -> str:
        saved = self.store.pop_oauth_state(state)
        if not saved or saved["user_id"] != self.user_id:
            raise ValueError("invalid or expired sign-in state; start the connection again")
        client = self._client()
        tokens = client.exchange_code(code, saved["redirect_uri"])
        email = client.email()
        prev = (self.account() or {}).get("settings")
        self.store.save_connector_account(self.user_id, "google", email, tokens, prev or DEFAULT_SETTINGS,
                                          self.clock.now().isoformat())
        return email

    def disconnect_google(self) -> None:
        g = self.google()
        if g:
            try:
                g.revoke()
            except Exception:
                pass
        self.store.delete_connector_account(self.user_id, "google")

    def status(self) -> list[dict[str, Any]]:
        acct = self.account()
        s = self.settings()
        base = {"connected": bool(acct), "account": (acct or {}).get("account"),
                "available": self.google_configured() or bool(self._google_factory)}
        return [
            {"id": "google_calendar", "name": "Google Calendar", "via": "google", **base,
             "description": "Avoids slots when you're busy, and adds every confirmed booking to your calendar "
                            "(moved on reschedule, removed on cancellation).",
             "settings": {"calendar_check": s["calendar_check"], "calendar_events": s["calendar_events"]}},
            {"id": "gmail", "name": "Gmail", "via": "google", **base,
             "description": "Emails you a confirmation after each confirmed booking, cancellation or change.",
             "settings": {"email_confirmations": s["email_confirmations"]}},
        ]

    # ------------------------------------------------------- read: busy times
    def busy(self, date_start: str, date_end: str) -> list[tuple[datetime, datetime]] | None:
        """Busy intervals (naive local time) between two dates, or None if the calendar isn't usable."""
        if not self.settings()["calendar_check"]:
            return None
        g = self.google()
        if not g:
            return None
        key = (date_start, date_end)
        if key not in self._busy_cache:
            raw = g.busy(f"{date_start}T00:00:00{TZ_OFFSET}", f"{date_end}T23:59:59{TZ_OFFSET}", TZ)
            out = []
            for a, b in raw:
                sa, sb = datetime.fromisoformat(a.replace("Z", "+00:00")), datetime.fromisoformat(b.replace("Z", "+00:00"))
                out.append((self._local(sa), self._local(sb)))
            self._busy_cache[key] = out
        return self._busy_cache[key]

    def events(self, date_start: str, date_end: str) -> list[dict[str, Any]] | None:
        """The user's events (local time) between two dates, or None if Google isn't connected.
        Titles and locations are the user's own text: callers treat them as data."""
        g = self.google()
        if not g:
            return None
        out = []
        for e in g.list_events(f"{date_start}T00:00:00{TZ_OFFSET}", f"{date_end}T23:59:59{TZ_OFFSET}", TZ):
            if e.get("status") == "cancelled":
                continue
            st, en = e.get("start", {}), e.get("end", {})
            all_day = "date" in st and "dateTime" not in st
            fmt = (lambda x: x["date"]) if all_day else \
                (lambda x: self._local(datetime.fromisoformat(x["dateTime"].replace("Z", "+00:00"))).isoformat(timespec="minutes"))
            out.append({"title": (e.get("summary") or "(no title)")[:120], "start": fmt(st), "end": fmt(en),
                        "all_day": all_day, "location": (e.get("location") or "")[:120] or None,
                        "booked_by_errand": bool(e.get("extendedProperties", {}).get("private", {})
                                                 .get("errand_booking_id"))})
        return out

    @staticmethod
    def _local(dt: datetime) -> datetime:
        """Aware datetime -> naive India time (slots are naive local times)."""
        if dt.tzinfo is None:
            return dt
        return (dt - dt.utcoffset() + timedelta(hours=5, minutes=30)).replace(tzinfo=None)

    @staticmethod
    def overlaps(slot: str, busy: list[tuple[datetime, datetime]]) -> bool:
        s = datetime.fromisoformat(slot)
        e = s + timedelta(minutes=EVENT_MINUTES)
        return any(a < e and s < b for a, b in busy)

    def calendar_will_record(self) -> bool:
        return bool(self.google() and self.settings()["calendar_events"])

    # -------------------------------------- writes: only after verified changes
    def _event_body(self, b: dict[str, Any]) -> dict[str, Any]:
        start = datetime.fromisoformat(b["slot"])
        end = start + timedelta(minutes=EVENT_MINUTES)
        service = str(b.get("service", "service")).replace("_", " ")
        return {
            "summary": f"{service.title()}: {b.get('provider_name', 'provider')} (Errand)",
            "location": (b.get("area") or "").title(),
            "description": (f"Booked by Errand, confirmed with the provider.\nBooking: {b['booking_id']}\n"
                            f"Provider: {b.get('provider_name')} ({b.get('provider_id')})\nPrice: ₹{b.get('price')}"),
            "start": {"dateTime": start.isoformat(), "timeZone": TZ},
            "end": {"dateTime": end.isoformat(), "timeZone": TZ},
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 60}]},
            "extendedProperties": {"private": {"errand_booking_id": b["booking_id"]}},
        }

    def _email(self, g: Any, kind: str, b: dict[str, Any], calendar: str | None = None) -> str | None:
        """Send the `kind` booking email (see email_templates) to the connected account itself."""
        if not self.settings()["email_confirmations"]:
            return None
        acct = self.account() or {}
        to = acct.get("account")
        if not to:
            return None
        subject, text, page = email_templates.render(kind, b, calendar=calendar)
        logo = email_templates.logo_bytes()
        g.send_email(to, subject, text, html=page, inline={email_templates.LOGO_CID: logo} if logo else None)
        return to

    def on_booked(self, b: dict[str, Any]) -> list[dict[str, str]]:
        """b: booking_id, slot, price, provider_id, provider_name, service, area. Returns status notes."""
        g = self.google()
        if not g:
            return []
        notes: list[dict[str, str]] = []
        s = self.settings()
        cal = None
        if s["calendar_events"]:
            try:
                if not self.store.calendar_event(b["booking_id"]):  # idempotent: never two events per booking
                    eid = g.insert_event(self._event_body(b))
                    self.store.save_calendar_event(b["booking_id"], self.user_id, eid, self.clock.now().isoformat())
                cal = "added"
                notes.append({"kind": "calendar", "ok": "yes", "text": "📅 Added to your Google Calendar."})
            except GoogleError as e:
                notes.append({"kind": "calendar", "ok": "no",
                              "text": f"⚠️ Couldn't add it to your Google Calendar ({e.status}). The booking itself is confirmed."})
        try:
            to = self._email(g, "booked", b, cal)
            if to:
                notes.append({"kind": "email", "ok": "yes", "text": f"✉️ Confirmation emailed to {to}."})
        except GoogleError as e:
            notes.append({"kind": "email", "ok": "no", "text": f"⚠️ Couldn't send the confirmation email ({e.status})."})
        return notes

    def on_cancelled(self, booking_id: str, by_provider: bool = False, summary: str = "",
                     details: dict[str, Any] | None = None) -> list[dict[str, str]]:
        """details: the booking (service, provider_name, slot, price, area) for the email."""
        g = self.google()
        if not g:
            return []
        notes: list[dict[str, str]] = []
        cal = None
        ev = self.store.calendar_event(booking_id)
        if ev and self.settings()["calendar_events"]:
            try:
                if by_provider:  # keep it visible so you notice, but clearly marked
                    g.patch_event(ev["event_id"], {"summary": f"[Cancelled by provider] {summary or booking_id}",
                                                   "transparency": "transparent"})
                    cal = "marked"
                    notes.append({"kind": "calendar", "ok": "yes", "text": "📅 Marked as cancelled in your Google Calendar."})
                else:
                    g.delete_event(ev["event_id"])
                    self.store.delete_calendar_event(booking_id)
                    cal = "removed"
                    notes.append({"kind": "calendar", "ok": "yes", "text": "📅 Removed from your Google Calendar."})
            except GoogleError as e:
                notes.append({"kind": "calendar", "ok": "no", "text": f"⚠️ Couldn't update your Google Calendar ({e.status})."})
        try:
            to = self._email(g, "provider_cancelled" if by_provider else "cancelled",
                             {**(details or {}), "booking_id": booking_id}, cal)
            if to:
                notes.append({"kind": "email", "ok": "yes", "text": f"✉️ Cancellation emailed to {to}."})
        except GoogleError as e:
            notes.append({"kind": "email", "ok": "no", "text": f"⚠️ Couldn't send the cancellation email ({e.status})."})
        return notes

    def on_rescheduled(self, booking_id: str, new_slot: str,
                       details: dict[str, Any] | None = None) -> list[dict[str, str]]:
        """details: the booking before the move (service, provider_name, slot = old slot, price, area)."""
        g = self.google()
        if not g:
            return []
        notes: list[dict[str, str]] = []
        cal = None
        ev = self.store.calendar_event(booking_id)
        if ev and self.settings()["calendar_events"]:
            try:
                start = datetime.fromisoformat(new_slot)
                g.patch_event(ev["event_id"], {
                    "start": {"dateTime": start.isoformat(), "timeZone": TZ},
                    "end": {"dateTime": (start + timedelta(minutes=EVENT_MINUTES)).isoformat(), "timeZone": TZ}})
                cal = "moved"
                notes.append({"kind": "calendar", "ok": "yes", "text": f"📅 Moved in your Google Calendar to {_human(new_slot)}."})
            except GoogleError as e:
                notes.append({"kind": "calendar", "ok": "no", "text": f"⚠️ Couldn't move the calendar event ({e.status})."})
        try:
            d = details or {}
            to = self._email(g, "rescheduled", {**d, "booking_id": booking_id, "old_slot": d.get("slot"),
                                                "slot": new_slot}, cal)
            if to:
                notes.append({"kind": "email", "ok": "yes", "text": f"✉️ Change emailed to {to}."})
        except GoogleError as e:
            notes.append({"kind": "email", "ok": "no", "text": f"⚠️ Couldn't send the change email ({e.status})."})
        return notes


    # ------------------------------------------- user actions from the Bookings panel
    # These only re-send or tidy up what a verified booking already produced; they never change a booking.
    def resend_email(self, b: dict[str, Any]) -> str:
        """Send the email for the booking's current state again. Returns the address it went to."""
        g = self.google()
        if not g:
            raise ValueError("Connect Google first (Connectors).")
        kind = {"confirmed": "booked", "cancelled": "cancelled",
                "cancelled_by_provider": "provider_cancelled"}.get(b.get("status") or "")
        if not kind:
            raise ValueError("There's no email for a completed visit.")
        acct = (self.account() or {}).get("account")
        if not acct:
            raise ValueError("No Google account email to send to.")
        cal = "added" if kind == "booked" and self.store.calendar_event(b["booking_id"]) else None
        subject, text, page = email_templates.render(kind, b, calendar=cal)
        logo = email_templates.logo_bytes()
        g.send_email(acct, subject, text, html=page, inline={email_templates.LOGO_CID: logo} if logo else None)
        return acct

    def add_to_calendar(self, b: dict[str, Any]) -> bool:
        """Put an upcoming confirmed booking on the calendar (no-op if it's already there)."""
        g = self.google()
        if not g:
            raise ValueError("Connect Google first (Connectors).")
        if b.get("status") != "confirmed":
            raise ValueError("Only confirmed bookings can be added to the calendar.")
        if self.store.calendar_event(b["booking_id"]):
            return False
        eid = g.insert_event(self._event_body(b))
        self.store.save_calendar_event(b["booking_id"], self.user_id, eid, self.clock.now().isoformat())
        return True

    def remove_from_calendar(self, booking_id: str) -> bool:
        """Delete the booking's calendar event (the booking itself is untouched)."""
        g = self.google()
        if not g:
            raise ValueError("Connect Google first (Connectors).")
        ev = self.store.calendar_event(booking_id)
        if not ev:
            return False
        g.delete_event(ev["event_id"])
        self.store.delete_calendar_event(booking_id)
        return True


class FakeGoogle:
    """In-memory stand-in used by tests and evals (same methods as GoogleClient)."""

    def __init__(self, busy: list[tuple[str, str]] | None = None, fail: set[str] | None = None,
                 email: str = "user@example.com"):
        self._busy = list(busy or [])
        self.fail = set(fail or ())
        self.events: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, str]] = []
        self.revoked = False
        self._email = email
        self._n = 0

    def _check(self, op: str) -> None:
        if op in self.fail:
            raise GoogleError(503, f"{op} failed (simulated)")

    def auth_url(self, redirect_uri: str, state: str) -> str:
        return f"https://accounts.google.com/fake?state={state}&redirect_uri={redirect_uri}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        self._check("exchange")
        return {"access_token": "fake-access", "refresh_token": "fake-refresh", "expires_at": 9e12}

    def email(self) -> str:
        return self._email

    def revoke(self) -> None:
        self.revoked = True

    def busy(self, time_min: str, time_max: str, tz: str) -> list[tuple[str, str]]:
        self._check("busy")
        return [(a, b) for a, b in self._busy if a[:10] <= time_max[:10] and b[:10] >= time_min[:10]]

    def list_events(self, time_min: str, time_max: str, tz: str) -> list[dict[str, Any]]:
        self._check("list")
        evs = [e for e in self.events.values()
               if time_min[:10] <= (e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", ""))[:10] <= time_max[:10]]
        return sorted(evs, key=lambda e: e["start"].get("dateTime") or e["start"].get("date"))

    def insert_event(self, event: dict[str, Any]) -> str:
        self._check("insert")
        self._n += 1
        eid = f"evt{self._n}"
        self.events[eid] = json.loads(json.dumps(event))
        return eid

    def patch_event(self, event_id: str, patch: dict[str, Any]) -> None:
        self._check("patch")
        self.events[event_id].update(patch)

    def delete_event(self, event_id: str) -> None:
        self._check("delete")
        self.events.pop(event_id, None)

    def send_email(self, to: str, subject: str, text: str, html: str | None = None,
                   inline: dict[str, bytes] | None = None) -> str:
        self._check("email")
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html, "inline": sorted(inline or {})})
        return f"msg{len(self.sent)}"
