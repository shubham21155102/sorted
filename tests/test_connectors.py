"""Google Calendar / Gmail connectors, offline (FakeGoogle + a fake HTTP layer for GoogleClient)."""
import json
import time

import pytest

from errand_agent.agent import ErrandAgent
from errand_agent.clock import FakeClock
from errand_agent.connectors import ConnectorHub, FakeGoogle, GoogleClient
from errand_agent.mock_env import MockWorld, ScenarioConfig
from errand_agent.scripted_model import ScriptedModel
from errand_agent.store import Store

REQ = "Find a plumber in Indiranagar this week, mornings, under 1500"


def make(fake: FakeGoogle, outcomes=("success",), connected=True):
    clock = FakeClock()
    store = Store()
    hub = ConnectorHub(store, "u1", clock, google_factory=lambda tokens, save: fake)
    if connected:
        store.save_connector_account("u1", "google", fake.email(), {"refresh_token": "r"}, None, "t")
    world = MockWorld(ScenarioConfig(book_outcomes=list(outcomes)), clock)
    agent = ErrandAgent(llm=ScriptedModel(), world=world, store=store, clock=clock, connectors=hub)
    return agent, world, hub


def test_verified_booking_creates_one_event_and_email():
    fake = FakeGoogle()
    agent, world, _ = make(fake)
    r1 = agent.handle_user_message(REQ)
    assert "It will be added to your Google Calendar" in r1
    assert not fake.events and not fake.sent  # nothing before approval
    r2 = agent.handle_user_message("yes")
    assert "📅 Added to your Google Calendar." in r2 and "✉️ Confirmation emailed to user@example.com." in r2
    (ev,) = fake.events.values()
    op = world.side_effects()[0]
    assert ev["extendedProperties"]["private"]["errand_booking_id"] == op["booking_id"]
    assert ev["start"]["dateTime"].startswith(op["slot"]) and "Plumber" in ev["summary"]
    assert fake.sent[0]["to"] == "user@example.com" and op["booking_id"] in fake.sent[0]["text"]
    agent.handle_user_message("yes")  # repeated confirmation: no second event or email
    assert len(fake.events) == 1 and len(fake.sent) == 1


def test_busy_slots_are_never_proposed():
    probe = FakeGoogle()
    agent, _, _ = make(probe)
    agent.handle_user_message(REQ)
    first = agent.store.proposals(session_id="s1")[-1]["material"]["slot"]
    # Same scenario, but the user is busy exactly then: a different slot must be chosen.
    end = first[:11] + f"{int(first[11:13]) + 1:02d}:00"
    busy = FakeGoogle(busy=[(first + ":00+05:30", end + ":00+05:30")])
    agent2, _, _ = make(busy)
    agent2.handle_user_message(REQ)
    second = agent2.store.proposals(session_id="s1")[-1]["material"]["slot"]
    assert second != first
    assert any(e["event"] == "connector" and e.get("action") == "busy_checked" for e in agent2.tracer.events)


def test_failed_booking_writes_nothing():
    fake = FakeGoogle()
    agent, _, _ = make(fake, outcomes=("slot_taken", "slot_taken", "slot_taken"))
    agent.handle_user_message(REQ)
    agent.handle_user_message("yes")
    assert not fake.events and not fake.sent


def test_calendar_outage_does_not_hide_a_confirmed_booking():
    fake = FakeGoogle(fail={"insert", "email"})
    agent, world, _ = make(fake)
    agent.handle_user_message(REQ)
    r = agent.handle_user_message("yes")
    assert len(world.side_effects()) == 1 and "✅ Verified" in r
    assert "Couldn't add it to your Google Calendar (503). The booking itself is confirmed." in r


def test_busy_check_failure_falls_back_to_all_slots():
    fake = FakeGoogle(fail={"busy"})
    agent, _, _ = make(fake)
    r = agent.handle_user_message(REQ)
    assert "Approval needed" in r
    assert any(e.get("action") == "busy_check_failed" for e in agent.tracer.events)


def test_cancel_reschedule_and_provider_cancel_update_the_event():
    fake = FakeGoogle()
    agent, world, hub = make(fake)
    # pretend the seeded AC booking was created by Errand earlier
    agent.store.save_calendar_event("BK-1002", "u1", fake.insert_event({"summary": "AC"}), "t")
    notes = hub.on_rescheduled("BK-1002", "2026-11-14T14:00")
    assert notes[0]["text"].startswith("📅 Moved") and fake.events["evt1"]["start"]["dateTime"] == "2026-11-14T14:00:00"
    notes = hub.on_cancelled("BK-1002", by_provider=True, summary="Ac Servicing: CoolAir")
    assert fake.events["evt1"]["summary"].startswith("[Cancelled by provider]")
    notes = hub.on_cancelled("BK-1002")
    assert "evt1" not in fake.events and agent.store.calendar_event("BK-1002") is None


def test_settings_turn_writes_off():
    fake = FakeGoogle()
    agent, _, hub = make(fake)
    hub.update_settings({"calendar_events": False, "email_confirmations": False})
    agent.handle_user_message(REQ)
    r = agent.handle_user_message("yes")
    assert "✅ Verified" in r and not fake.events and not fake.sent
    assert "Google Calendar" not in r


def test_not_connected_means_no_calendar_mentions():
    fake = FakeGoogle()
    agent, _, _ = make(fake, connected=False)
    r = agent.handle_user_message(REQ)
    assert "Google Calendar" not in r
    assert "Google Calendar" not in agent.handle_user_message("yes")


def test_oauth_state_is_checked_and_single_use():
    fake = FakeGoogle(email="me@gmail.com")
    clock = FakeClock()
    store = Store()
    hub = ConnectorHub(store, "u1", clock, google_factory=lambda tokens, save: fake)
    url = hub.begin_google("http://127.0.0.1:8765/api/connectors/google/callback")
    state = url.split("state=")[1].split("&")[0]
    with pytest.raises(ValueError):
        hub.finish_google("code", "forged-state")
    assert hub.finish_google("code", state) == "me@gmail.com"
    assert hub.status()[0]["connected"] and hub.status()[0]["account"] == "me@gmail.com"
    with pytest.raises(ValueError):
        hub.finish_google("code", state)  # replay
    hub.disconnect_google()
    assert fake.revoked and not hub.status()[0]["connected"]


class FakeHttp:
    def __init__(self):
        self.calls = []
        self.unauthorized_once = True

    def __call__(self, method, url, headers, data):
        self.calls.append((method, url.split("?")[0], headers.get("Authorization", "")))
        if url.startswith("https://oauth2.googleapis.com/token"):
            return 200, {"access_token": f"tok{len(self.calls)}", "expires_in": 3600}
        if "freeBusy" in url:
            if self.unauthorized_once:
                self.unauthorized_once = False
                return 401, {"error": "expired"}
            body = json.loads(data)
            assert body["items"] == [{"id": "primary"}] and body["timeZone"] == "Asia/Kolkata"
            return 200, {"calendars": {"primary": {"busy": [{"start": "2026-11-10T04:30:00Z", "end": "2026-11-10T05:30:00Z"}]}}}
        return 404, {}


def test_google_client_refreshes_and_retries_once_on_401():
    http = FakeHttp()
    saved = []
    g = GoogleClient("cid", "secret", {"refresh_token": "r", "access_token": "old", "expires_at": time.time() + 999},
                     saved.append, http)
    busy = g.busy("2026-11-10T00:00:00+05:30", "2026-11-10T23:59:59+05:30", "Asia/Kolkata")
    assert busy == [("2026-11-10T04:30:00Z", "2026-11-10T05:30:00Z")]
    assert [c[1] for c in http.calls] == ["https://www.googleapis.com/calendar/v3/freeBusy",
                                         "https://oauth2.googleapis.com/token",
                                         "https://www.googleapis.com/calendar/v3/freeBusy"]
    assert saved and saved[-1]["access_token"] == "tok2"


def test_hub_converts_utc_busy_to_india_time():
    fake = FakeGoogle(busy=[("2026-11-10T04:30:00Z", "2026-11-10T05:30:00Z")])  # 10:00-11:00 IST
    store = Store()
    hub = ConnectorHub(store, "u1", FakeClock(), google_factory=lambda t, s: fake)
    store.save_connector_account("u1", "google", "me@x", {"refresh_token": "r"}, None, "t")
    busy = hub.busy("2026-11-10", "2026-11-10")
    assert hub.overlaps("2026-11-10T10:00", busy) and hub.overlaps("2026-11-10T09:30", busy)
    assert not hub.overlaps("2026-11-10T11:00", busy) and not hub.overlaps("2026-11-10T09:00", busy)
