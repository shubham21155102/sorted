"""Seed data: 55 memories (mixed explicit / inferred / superseded / forgotten /
irrelevant) and a long prior conversation, to exercise context management."""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from .store import Store

# (key, value, scope, source, confirmed, status)
SEED_MEMORIES = [
    ("preferred_area", "indiranagar", "global", "explicit", True, "active"),
    ("preferred_area", "jayanagar", "global", "explicit", True, "superseded"),
    ("earliest_time", "09:00", "global", "explicit", True, "active"),
    ("latest_time", "19:00", "global", "explicit", True, "active"),
    ("preferred_days", "weekdays", "global", "explicit", True, "active"),
    ("budget_max", "1500", "plumber", "explicit", True, "active"),
    ("budget_max", "2000", "ac_servicing", "explicit", True, "active"),
    ("budget_max", "1200", "electrician", "explicit", True, "active"),
    ("budget_max", "900", "plumber", "explicit", True, "superseded"),
    ("earliest_time", "10:00", "electrician", "explicit", True, "active"),
    ("avoid_provider", "TapMasters (PLU-IND-4) - left a mess", "plumber", "explicit", True, "active"),
    ("contact_preference", "WhatsApp, not calls", "global", "explicit", True, "active"),
    ("language", "English or Hindi", "global", "explicit", True, "active"),
    ("address_notes", "Gate 2, building B, 4th floor", "global", "explicit", True, "active"),
    ("gate_code", "4471", "global", "explicit", True, "forgotten"),
    ("technician_preference", "AquaCare (PLU-IND-3)", "plumber", "inferred", False, "active"),
    ("technician_preference", "CoolAir Services (ACS-IND-2)", "ac_servicing", "inferred", False, "active"),
    ("pays_by", "UPI", "global", "inferred", False, "active"),
    ("home_type", "2BHK apartment", "global", "inferred", True, "active"),
    ("pet", "a dog named Bruno", "global", "explicit", True, "active"),
    ("favourite_cuisine", "South Indian", "global", "explicit", True, "active"),
    ("works_from_home", "Tuesdays and Thursdays", "global", "explicit", True, "active"),
    ("partner_name", "Ananya", "global", "explicit", True, "forgotten"),
    ("birthday", "14 March", "global", "explicit", True, "forgotten"),
    ("car", "white hatchback", "global", "inferred", False, "active"),
]
FILLER_TOPICS = ["gym_timing", "newspaper", "milk_delivery", "favourite_team", "plant_care", "music", "commute",
                 "coffee", "tv_show", "parking_spot", "laundry_day", "grocery_store", "school_run", "yoga",
                 "book_club", "cricket", "weekend_plan", "tea", "dentist", "library", "society_meeting",
                 "power_backup", "water_tanker", "internet_plan", "maid_schedule", "recycling", "cycling",
                 "podcast", "running"]


def seed_memories(store: Store, user_id: str = "u1", start: datetime | None = None) -> int:
    start = start or datetime(2026, 6, 1, 9, 0)
    rng = random.Random(42)
    rows = list(SEED_MEMORIES)
    for i, topic in enumerate(FILLER_TOPICS):
        rows.append((topic, f"note #{i}: {topic.replace('_', ' ')} detail {rng.randint(100, 999)}", "global",
                     rng.choice(["explicit", "explicit", "inferred"]), rng.random() > 0.4,
                     rng.choice(["active", "active", "active", "superseded", "forgotten"])))
    n = 0
    for i, (key, value, scope, source, confirmed, status) in enumerate(rows):
        ts = (start + timedelta(days=i)).isoformat()
        store.upsert_memory({"id": f"M-seed-{i:03d}", "user_id": user_id, "key": key,
                             "value": "[forgotten]" if status == "forgotten" else value, "scope": scope,
                             "source": source, "confirmed": confirmed if source == "inferred" else True,
                             "status": status, "evidence": "seeded", "created_at": ts, "updated_at": ts})
        n += 1
    return n


def seed_conversation(store: Store, session_id: str, user_id: str = "u1", n_pairs: int = 150,
                      start: datetime | None = None) -> int:
    """A long prior conversation. Contains a forgotten fact (gate code) said in the past,
    which is redacted by MemoryManager.forget in the scenario that tests it."""
    start = start or datetime(2026, 7, 1, 10, 0)
    rng = random.Random(7)
    asks = ["Can you find a cleaner for Saturday?", "Is the AC guy coming tomorrow?", "Book a carpenter for the shelf",
            "What did the plumber charge last time?", "Remind me which electrician fixed the fan",
            "Any pest control slots next week?", "Move the cleaning to Sunday", "Cancel the carpenter please",
            "How much is deep cleaning usually?", "Find someone for the bathroom leak"]
    n = 0
    for i in range(n_pairs):
        ts = (start + timedelta(hours=7 * i)).isoformat()
        u = rng.choice(asks) + f" (ref {i})"
        if i == 3:
            u = "By the way the gate code is 4471, tell the technician."
        a = f"Sure — noted (ref {i}). " + rng.choice(["I found three options.", "That's done.", "I'll check.",
                                                      "Prices were between ₹800 and ₹1,400.",
                                                      "Let me know which slot works."]) + " " + "details " * rng.randint(5, 30)
        store.add_message(session_id, "user", u, ts, user_id)
        store.add_message(session_id, "assistant", a, ts, user_id)
        n += 2
    return n


def seed_all(store: Store, session_id: str, user_id: str = "u1", history_pairs: int = 150) -> dict[str, int]:
    """Seed history first, then memories; facts that are seeded as *forgotten* are also
    scrubbed from the history, exactly as MemoryManager.forget does at runtime."""
    n_msgs = seed_conversation(store, session_id, user_id, history_pairs) if history_pairs else 0
    n_mem = seed_memories(store, user_id)
    originals = {k: v for (k, v, _s, _src, _c, status) in SEED_MEMORIES if status == "forgotten"}
    for value in originals.values():
        store.redact_messages(user_id, value)
    return {"messages": n_msgs, "memories": n_mem}


FORGOTTEN_VALUES = [v for (_k, v, _s, _src, _c, status) in SEED_MEMORIES if status == "forgotten"]
