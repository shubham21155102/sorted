"""Google connector: OAuth 2.0 (installed-app loopback flow), Calendar and Gmail over plain HTTPS.

One Google sign-in covers both products:
  * Calendar: free/busy lookups (so Errand never proposes a slot you're busy) and events for
    verified bookings (created on booking, moved on reschedule, removed on cancellation).
  * Gmail: a confirmation email to your own address after a verified booking change.

No Google SDK: requests go through a tiny injectable `http` function, so tests run offline.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Any, Callable

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CALENDAR = "https://www.googleapis.com/calendar/v3"
GMAIL = "https://gmail.googleapis.com/gmail/v1"
SCOPES = [
    "openid", "email",
    "https://www.googleapis.com/auth/calendar.events",    # create / move / delete Errand's events
    "https://www.googleapis.com/auth/calendar.freebusy",  # busy times only, no event details
    "https://www.googleapis.com/auth/gmail.send",         # send confirmations to yourself
]

Http = Callable[[str, str, dict[str, str], bytes | None], tuple[int, Any]]


class GoogleError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"Google API {status}: {str(detail)[:300]}")
        self.status = status
        self.detail = detail


def default_http(method: str, url: str, headers: dict[str, str], data: bytes | None) -> tuple[int, Any]:
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


class GoogleClient:
    def __init__(self, client_id: str, client_secret: str, tokens: dict[str, Any] | None = None,
                 on_tokens: Callable[[dict[str, Any]], None] | None = None, http: Http | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.tokens = dict(tokens or {})
        self.on_tokens = on_tokens or (lambda t: None)
        self.http = http or default_http

    # ------------------------------------------------------------------ OAuth
    def auth_url(self, redirect_uri: str, state: str) -> str:
        return AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "true", "state": state})

    def _form(self, url: str, fields: dict[str, str]) -> dict[str, Any]:
        status, body = self.http("POST", url, {"Content-Type": "application/x-www-form-urlencoded"},
                                 urllib.parse.urlencode(fields).encode())
        if status >= 400:
            raise GoogleError(status, body)
        return body

    def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        t = self._form(TOKEN_URL, {"code": code, "client_id": self.client_id, "client_secret": self.client_secret,
                                   "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
        t["expires_at"] = time.time() + int(t.get("expires_in", 3600)) - 60
        self.tokens = t
        self.on_tokens(t)
        return t

    def _access_token(self) -> str:
        if self.tokens.get("access_token") and time.time() < self.tokens.get("expires_at", 0):
            return self.tokens["access_token"]
        if not self.tokens.get("refresh_token"):
            raise GoogleError(401, "not connected (no refresh token); reconnect Google")
        t = self._form(TOKEN_URL, {"refresh_token": self.tokens["refresh_token"], "client_id": self.client_id,
                                   "client_secret": self.client_secret, "grant_type": "refresh_token"})
        self.tokens.update(t, expires_at=time.time() + int(t.get("expires_in", 3600)) - 60)
        self.on_tokens(self.tokens)
        return self.tokens["access_token"]

    def revoke(self) -> None:
        tok = self.tokens.get("refresh_token") or self.tokens.get("access_token")
        if tok:
            self.http("POST", REVOKE_URL + "?" + urllib.parse.urlencode({"token": tok}),
                      {"Content-Type": "application/x-www-form-urlencoded"}, b"")

    def _api(self, method: str, url: str, body: Any = None, params: dict[str, Any] | None = None) -> Any:
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"}
        status, resp = self.http(method, url, headers, json.dumps(body).encode() if body is not None else None)
        if status == 401:  # access token revoked/expired early: refresh once and retry
            self.tokens["expires_at"] = 0
            headers["Authorization"] = f"Bearer {self._access_token()}"
            status, resp = self.http(method, url, headers, json.dumps(body).encode() if body is not None else None)
        if status >= 400:
            raise GoogleError(status, resp)
        return resp

    # --------------------------------------------------------------- account
    def email(self) -> str:
        return self._api("GET", USERINFO_URL).get("email", "")

    # -------------------------------------------------------------- calendar
    def busy(self, time_min: str, time_max: str, tz: str) -> list[tuple[str, str]]:
        r = self._api("POST", f"{CALENDAR}/freeBusy",
                      {"timeMin": time_min, "timeMax": time_max, "timeZone": tz, "items": [{"id": "primary"}]})
        return [(b["start"], b["end"]) for b in r.get("calendars", {}).get("primary", {}).get("busy", [])]

    def insert_event(self, event: dict[str, Any]) -> str:
        return self._api("POST", f"{CALENDAR}/calendars/primary/events", event, {"sendUpdates": "none"})["id"]

    def patch_event(self, event_id: str, patch: dict[str, Any]) -> None:
        self._api("PATCH", f"{CALENDAR}/calendars/primary/events/{urllib.parse.quote(event_id)}", patch)

    def delete_event(self, event_id: str) -> None:
        try:
            self._api("DELETE", f"{CALENDAR}/calendars/primary/events/{urllib.parse.quote(event_id)}")
        except GoogleError as e:
            if e.status not in (404, 410):  # already gone is fine
                raise

    # ----------------------------------------------------------------- gmail
    def send_email(self, to: str, subject: str, text: str) -> str:
        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return self._api("POST", f"{GMAIL}/users/me/messages/send", {"raw": raw}).get("id", "")
