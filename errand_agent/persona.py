"""Errand's voice persona: one character for browser calls and phone calls.

The persona takes the name of the Sarvam voice you pick (e.g. "Kavya from Errand"), so the voice and
the name always match. You choose the voice, speaking speed and style in the app (user menu → Voice);
they are saved on your profile and used for:
  * the spoken-reply instructions the agent gets on voice/phone turns (persona_note),
  * the greeting at the start of a call (greeting),
  * the LiveKit voice worker's text-to-speech (speaker + pace).
"""
from __future__ import annotations

from typing import Any

# Sarvam bulbul:v3 voices (docs.sarvam.ai → Text to speech). Names are lowercase on the API.
VOICES = ["kavya", "priya", "ritu", "neha", "pooja", "simran", "ishita", "shreya", "roopa", "tanya", "shruti",
          "suhani", "kavitha", "rupali", "shubh", "aditya", "rahul", "rohan", "amit", "dev", "ratan", "varun",
          "manan", "sumit", "kabir", "aayan", "ashutosh", "advait", "anand", "tarun", "sunny", "mani", "gokul",
          "vijay", "mohit", "rehan", "soham"]
STYLES = {
    "warm": ("Warm", "Friendly and reassuring, like a helpful neighbour who knows good local service people. "
                     "A short acknowledgement first (\"Sure\", \"Got it\"), then the useful part."),
    "brisk": ("Brisk", "Efficient and to the point. No small talk; lead with the answer."),
}
DEFAULT = {"speaker": "kavya", "pace": 1.0, "style": "warm"}
PACE_RANGE = (0.8, 1.3)


def settings(profile: dict[str, Any] | None) -> dict[str, Any]:
    """Validated persona settings from a profile row (falls back to the defaults)."""
    p = profile or {}
    speaker = p.get("voice_speaker") if p.get("voice_speaker") in VOICES else DEFAULT["speaker"]
    try:
        pace = min(max(float(p.get("voice_pace") or DEFAULT["pace"]), PACE_RANGE[0]), PACE_RANGE[1])
    except (TypeError, ValueError):
        pace = DEFAULT["pace"]
    style = p.get("voice_style") if p.get("voice_style") in STYLES else DEFAULT["style"]
    return {"speaker": speaker, "name": speaker.capitalize(), "pace": round(pace, 2), "style": style}


def validate(body: dict[str, Any]) -> dict[str, Any]:
    """Settings from an API request; raises ValueError with a user-facing message."""
    out: dict[str, Any] = {}
    if "speaker" in body:
        if body["speaker"] not in VOICES:
            raise ValueError("Unknown voice")
        out["voice_speaker"] = body["speaker"]
    if "pace" in body:
        try:
            pace = float(body["pace"])
        except (TypeError, ValueError):
            raise ValueError("Speed must be a number") from None
        if not PACE_RANGE[0] <= pace <= PACE_RANGE[1]:
            raise ValueError(f"Speed must be between {PACE_RANGE[0]} and {PACE_RANGE[1]}")
        out["voice_pace"] = round(pace, 2)
    if "style" in body:
        if body["style"] not in STYLES:
            raise ValueError("Unknown style")
        out["voice_style"] = body["style"]
    return out


def persona_note(p: dict[str, Any], user_name: str | None) -> str:
    """Who the agent is on a call, added to the spoken-reply instructions."""
    first = (user_name or "").split(" ")[0]
    return (f"On calls you are {p['name']}, Errand's voice assistant. {STYLES[p['style']][1]} "
            + (f"Call the user {first} now and then (not every turn). " if first else "")
            + "Sound natural and human in tone, but you are an AI assistant: if asked, say so plainly and never "
              "claim to be a person. When something goes wrong, say it simply and say what happens next.")


def greeting(p: dict[str, Any], user_name: str | None, *, outbound: bool = False) -> str:
    first = (user_name or "").split(" ")[0]
    hi = f"Hi {first}, " if first else "Hi, "
    if outbound:
        return f"{hi}it's {p['name']} from Errand."
    return f"{hi}{p['name']} here from Errand. What can I sort out for you?"


def preview_line(p: dict[str, Any], user_name: str | None) -> str:
    first = (user_name or "").split(" ")[0]
    return (f"Hi{(' ' + first) if first else ''}, I'm {p['name']} from Errand. Tell me what needs fixing at home, "
            "and I'll find someone good, at a time that suits you.")
