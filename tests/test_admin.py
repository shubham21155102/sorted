"""The provider database and admin operations: changes show up in the agent's tools immediately."""
import json
from datetime import timedelta

import pytest

from errand_agent import admin
from errand_agent.clock import FakeClock
from errand_agent.mock_env import AREAS, SERVICES, MockWorld, ScenarioConfig


@pytest.fixture
def world():
    return MockWorld(ScenarioConfig(), FakeClock())


def week(w):
    d = w.clock.now().date()
    return {"start": d.isoformat(), "end": (d + timedelta(days=6)).isoformat()}


def test_catalog_is_realistic_and_meets_the_contract(world):
    for service in SERVICES:
        for area in AREAS:
            r = world.search_providers(service, area, week(world))
            assert 3 <= len(r["providers"]) <= 8, (service, area)  # contract: 3-8 providers
    p = world.search_providers("ac_servicing", "indiranagar", week(world))["providers"][0]
    assert {"provider_id", "price", "rating", "rating_count", "jobs_done", "available_slots"} <= set(p)
    assert {"verified", "years_experience", "working_hours", "days_off", "response_time_mins"} <= set(p["attributes"])
    d = world.get_provider("ACS-IND-2")
    assert d["technician"] and d["about"] and len(d["reviews"]) >= 4
    assert any("ignore all previous instructions" in r["text"] for r in d["reviews"])  # contract: injection review
    assert all({"author", "rating", "date", "text"} <= set(r) for r in d["reviews"])


def test_slots_respect_working_hours_and_days_off(world):
    from datetime import datetime
    for p in world.search_providers("cleaning", "whitefield", week(world))["providers"]:
        q = world.provider(p["provider_id"])
        for s in p["available_slots"]:
            assert q["work_start"] <= s[11:16] < q["work_end"] and datetime.fromisoformat(s).weekday() not in q["days_off"]


def test_admin_edits_reach_the_tools(world):
    pid = "ACS-IND-1"
    admin.update_provider(world, pid, {"price": 1111})
    s = world.search_providers("ac_servicing", "indiranagar", week(world))
    assert next(p for p in s["providers"] if p["provider_id"] == pid)["price"] == 1111
    slot = next(p for p in s["providers"] if p["provider_id"] == pid)["available_slots"][0]
    assert world.hold_slot(pid, slot)["search_price"] == 1111
    # blocked slot disappears and can't be held
    other = next(p for p in s["providers"] if p["provider_id"] == pid)["available_slots"][1]
    admin.set_slot(world, pid, other, "blocked", "on leave")
    assert other not in world.search_providers("ac_servicing", "indiranagar", week(world))["providers"][0]["available_slots"]
    assert world.hold_slot(pid, other)["error"] == "slot_unavailable"
    admin.set_slot(world, pid, other, "clear")
    assert world.hold_slot(pid, other)["hold_id"]
    # a paused provider is not offered
    admin.update_provider(world, pid, {"active": False})
    ids = [p["provider_id"] for p in world.search_providers("ac_servicing", "indiranagar", week(world))["providers"]]
    assert pid not in ids


def test_hours_and_days_off_edits(world):
    pid = "PLU-IND-1"
    admin.update_provider(world, pid, {"work_start": "14:00", "work_end": "17:00", "days_off": [5, 6]})
    slots = [p for p in world.search_providers("plumber", "indiranagar", week(world))["providers"]
             if p["provider_id"] == pid][0]["available_slots"]
    assert all(s[11:16] in ("14:00", "16:00") for s in slots)
    with pytest.raises(ValueError):
        admin.update_provider(world, pid, {"work_start": "18:00", "work_end": "09:00"})
    with pytest.raises(ValueError):
        admin.update_provider(world, pid, {"days_off": [0, 1, 2, 3, 4, 5, 6]})


def test_reviews_ratings_jobs_and_provider_cancel(world):
    p = world.provider("ELE-KOR-1")
    after = admin.add_review(world, "ELE-KOR-1", {"author": "Neha K.", "rating": 1, "text": "Did not show up."})
    assert after["rating_count"] == p["rating_count"] + 1 and after["rating"] <= p["rating"]  # 1 of 377 barely moves it
    fresh = admin.create_provider(world, {"service": "electrician", "area": "koramangala", "name": "New Sparks", "price": 500,
                                          "phone": "+91-9811111111", "technician": "Ali"})
    assert admin.add_review(world, fresh["provider_id"], {"rating": 4, "text": "Good first visit."})["rating"] == 4.0
    rid = world.reviews("ELE-KOR-1")[-1]["review_id"]
    admin.set_review_hidden(world, rid, True)
    assert "Did not show up." not in [r["text"] for r in world.get_provider("ELE-KOR-1")["reviews"]]
    jobs = world.provider("ACS-IND-2")["jobs_done"]
    admin.complete_booking(world, "BK-1002")
    assert world.booking("BK-1002")["status"] == "completed" and world.provider("ACS-IND-2")["jobs_done"] == jobs + 1
    with pytest.raises(ValueError):
        admin.provider_cancel(world, "BK-1002")  # only confirmed bookings


def test_provider_cancel_emits_the_event_the_agent_reacts_to(world):
    ev = admin.provider_cancel(world, "BK-1002", "Technician unwell")
    assert ev["type"] == "provider_cancelled" and world.booking("BK-1002")["status"] == "cancelled_by_provider"
    assert world.poll_events("u1")[-1]["reason"] == "Technician unwell"


def test_create_provider_and_fault_queue(world):
    p = admin.create_provider(world, {"service": "plumber", "area": "hebbal", "name": "Hebbal Pipe Care", "price": 650,
                                      "phone": "+91-9800000000", "technician": "Ramu", "work_start": "09:00",
                                      "work_end": "18:00", "days_off": [6]})
    assert p["provider_id"].startswith("PLU-HEB-") and p["active"]
    admin.queue_fault(world, {"kind": "book_outcome", "value": "timeout_booked"})
    admin.queue_fault(world, {"kind": "tool_failure", "tool": "search_providers", "value": "error"})
    assert world.call("search_providers", {"service": "plumber", "area": "hebbal", "date_range": week(world)})["http_status"] == 500
    assert world._draw_book_outcome("k") == "timeout_booked"
    with pytest.raises(ValueError):
        admin.queue_fault(world, {"kind": "book_outcome", "value": "explode"})


def test_world_persists_across_restarts_and_imports_old_json(tmp_path):
    path = str(tmp_path / "world.sqlite")
    w = MockWorld(ScenarioConfig(), FakeClock(), path)
    admin.update_provider(w, "ACS-IND-1", {"price": 999})
    w2 = MockWorld(ScenarioConfig(), FakeClock(), path)  # "process restart"
    assert w2.provider("ACS-IND-1")["price"] == 999
    # an older world.json is imported once and renamed
    legacy = tmp_path / "old" / "world.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"bookings": {"BK-1004": {
        "booking_id": "BK-1004", "user_id": "u1", "provider_id": "ACS-IND-1", "provider_name": "ChillZone",
        "service": "ac_servicing", "area": "indiranagar", "slot": "2026-11-20T08:00", "price": 920,
        "status": "confirmed", "hold_id": "H-0004", "created_at": "2026-11-08T22:37:00"}},
        "idempotency": {"k1": {"op": "book", "booking_id": "BK-1004", "hold_id": "H-0004"}},
        "counters": {"booking": 1004, "hold": 4, "event": 0, "book_draw": 1}}))
    w3 = MockWorld(ScenarioConfig(), FakeClock(), str(legacy.parent / "world.sqlite"))
    assert w3.booking("BK-1004")["price"] == 920 and w3.get_booking_status(idempotency_key="k1")["booking_id"] == "BK-1004"
    assert not legacy.exists() and (legacy.parent / "world.json.migrated").exists()


def test_providers_are_paginated(world):
    p1 = admin.providers(world, page=1, per_page=25)
    assert p1["total"] == world._one("SELECT COUNT(*) n FROM providers")["n"] and len(p1["providers"]) == 25
    assert p1["pages"] == -(-p1["total"] // 25)
    last = admin.providers(world, page=p1["pages"], per_page=25)
    assert len(last["providers"]) == p1["total"] - 25 * (p1["pages"] - 1)
    ids = {p["provider_id"] for n in range(1, p1["pages"] + 1) for p in admin.providers(world, page=n)["providers"]}
    assert len(ids) == p1["total"]                              # every provider on exactly one page
    assert admin.providers(world, page=999)["page"] == p1["pages"]  # out of range clamps to the last page
    f = admin.providers(world, service="ac_servicing", area="indiranagar")
    assert f["total"] == 7 and f["pages"] == 1
