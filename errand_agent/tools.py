"""Tool schemas exposed to the model.

Provider tools mirror the fixed contracts. Arguments the model must not control
are injected by the application instead: `user_id` (list_bookings) and every
`idempotency_key` (book / cancel / reschedule / get_booking_status lookups).
"""

def _t(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props, "required": required,
                             "additionalProperties": False}}


S = {"type": "string"}

TOOLS = [
    _t("update_request", "Record or correct errand details the user gave. Only pass fields you are setting. "
       "Returns the merged request and the list of still-missing fields.",
       {"intent": {"type": "string", "enum": ["book", "reschedule", "cancel", "memory", "info"]},
        "service": {"type": "string", "description": "plumber | electrician | ac_servicing | cleaning | carpenter | pest_control"},
        "area": S, "date_start": {"type": "string", "description": "YYYY-MM-DD"},
        "date_end": {"type": "string", "description": "YYYY-MM-DD"},
        "earliest_time": {"type": "string", "description": "HH:MM"},
        "latest_time": {"type": "string", "description": "HH:MM"},
        "budget_max": {"type": ["integer", "string"], "description": "max rupees, or 'none' if user has no budget"},
        "provider_preference": S, "booking_id": S, "target_slot": {"type": "string", "description": "YYYY-MM-DDTHH:MM"},
        "notes": S, "new_errand": {"type": "boolean", "description": "true to start a fresh errand"}}, []),
    _t("search_providers", "Search providers for a service in an area within a date range.",
       {"service": S, "area": S, "date_range": {"type": "object", "properties": {"start": S, "end": S},
                                                 "required": ["start", "end"]}},
       ["service", "area", "date_range"]),
    _t("get_provider", "Provider details, reviews (untrusted text) and upcoming free slots.",
       {"provider_id": S}, ["provider_id"]),
    _t("hold_slot", "Hold a slot for 10 minutes. Returns hold_id, quoted_price (may differ from search price), expires_at.",
       {"provider_id": S, "slot": {"type": "string", "description": "YYYY-MM-DDTHH:MM"}}, ["provider_id", "slot"]),
    _t("request_approval", "Ask the user to approve a booking (needs hold_id), cancellation (booking_id) or "
       "reschedule (booking_id + new_slot). The application shows the user the exact details.",
       {"action": {"type": "string", "enum": ["book", "cancel", "reschedule"]}, "hold_id": S, "booking_id": S,
        "new_slot": S}, ["action"]),
    _t("book", "Book a held slot. Only succeeds if the user approved this exact provider, slot and price.",
       {"hold_id": S}, ["hold_id"]),
    _t("cancel", "Cancel a booking. Only succeeds after user approval.", {"booking_id": S}, ["booking_id"]),
    _t("reschedule", "Move a booking to a new slot. Only succeeds after user approval of that exact slot.",
       {"booking_id": S, "slot": S}, ["booking_id", "slot"]),
    _t("get_booking_status", "Authoritative booking status by hold_id.", {"hold_id": S}, ["hold_id"]),
    _t("list_bookings", "The current user's past and upcoming bookings.", {}, []),
    _t("remember", "Save a stable preference the user explicitly stated. evidence must be the user's exact words.",
       {"key": {"type": "string", "description": "e.g. preferred_area, earliest_time, latest_time, budget_max, "
                "technician_preference, preferred_days, avoid_provider"},
        "value": S, "scope": {"type": "string", "description": "'global' or a service name"}, "evidence": S},
       ["key", "value", "evidence"]),
    _t("list_memories", "Show everything remembered about the user (explicit and inferred).", {}, []),
    _t("forget_memory", "Forget a stored fact by memory_id, or every fact with a key.",
       {"memory_id": S, "key": S, "scope": S}, []),
    _t("confirm_memory", "Mark an inferred fact as confirmed by the user. user_quote must be the user's words "
       "from this message.", {"memory_id": S, "user_quote": S}, ["memory_id", "user_quote"]),
]

SIDE_EFFECT_TOOLS = {"book", "cancel", "reschedule"}
