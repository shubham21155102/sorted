"""Phone calls through Sarvam Voice Agents (Samvaad), on a number rented from Sarvam.

How it fits together:
  * A Sarvam voice agent handles the phone line (telephony, speech, Indian languages).
    It is a thin relay: on every caller turn it calls ONE HTTP tool, `errand_turn`,
    which POSTs to this app's /api/sarvam/turn, and it speaks back the `say` field.
    The Errand agent behind that endpoint does the real work, so phone calls get the
    same approvals, idempotent booking, memory, limits, trace and chat history.
  * Inbound: an inbound deployment links the Sarvam number to the agent. The caller's
    number must be the user's registered number; each call becomes a new chat.
  * Outbound ("Call me"): this app calls Sarvam's instant-outbound API with the chat's
    session id as an agent variable and a briefing as the agent's first line, so the call
    continues that chat.
  * A call-completion webhook writes the "call ended" marker into the chat.

Sarvam reaches this app over a public HTTPS URL (ERRAND_PUBLIC_URL, e.g. a cloudflared
tunnel). Requests from outside are limited to /api/sarvam/* and need ERRAND_SARVAM_TOKEN.

CLI:
    python -m errand_agent.sarvam status
    python -m errand_agent.sarvam deploy-inbound      # link the number to the agent + webhook
    python -m errand_agent.sarvam call +91XXXXXXXXXX  # test an outbound call
    python -m errand_agent.sarvam agent-spec          # print the agent prompt + tool to create on Sarvam
    python -m errand_agent.sarvam set-version <deployment_id> <n>   # move the live line to agent version n
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from .app import load_dotenv
from .brand import NAME

BASE = {"deployments": "https://apps.sarvam.ai/api/app-authoring",
        "outbounds": "https://apps.sarvam.ai/api/outbounds"}

AGENT_PROMPT = f"""You are the phone line for {NAME}, an assistant that arranges home-service errands \
(plumbers, electricians, AC servicing, cleaning, carpentry, pest control) in Bangalore.

You do not decide anything yourself. {NAME}'s own system does all the work: it searches providers, \
holds slots, asks for approval and books.

On EVERY turn, as soon as the caller finishes speaking, call tool:errand_turn with `text` set to \
exactly what the caller just said (in their own words; translate to English only if they spoke another \
language). Then say the tool's reply to the caller exactly as returned, without adding, removing or \
changing details, prices, dates or names. If the reply asks them to confirm, wait for their answer and \
pass it to errand_turn as well; never confirm, book or cancel anything yourself.

If the tool fails, say: "Sorry, I couldn't reach {NAME} just now. Nothing was booked. Please try again \
in a minute." If the tool reply contains end_call true, say the reply and end the call."""

TOOL_SPEC = {
    "name": "errand_turn",
    "description": f"Send the caller's latest words to {NAME} and get back what to say. Call on every turn.",
    "when": "run (during conversation)",
    "method": "POST",
    "url": "{ERRAND_PUBLIC_URL}/api/sarvam/turn",
    "auth": "bearer token = ERRAND_SARVAM_TOKEN (store it as a Sarvam workspace secret)",
    "body": {"text": "<LLM fills: the caller's latest words>",
             "caller": "@User Identifier", "interaction_id": "@Interaction ID",
             "session_id": "@session_id (agent variable, set on outbound calls)"},
    "response_template": "{{say}}",
    "max_wait_seconds": 30,
    "on_failure": f"Sorry, I couldn't reach {NAME} just now. Nothing was booked.",
}


def env() -> dict[str, str | None]:
    load_dotenv()
    load_dotenv(os.path.join("secrets", "sarvam.env"))
    return {k: os.environ.get(k) for k in (
        "SARVAM_VA_API_KEY", "SARVAM_SAMVAD_API_KEY", "SARVAM_API_KEY", "SARVAM_ORG_ID", "SARVAM_WORKSPACE_ID", "SARVAM_APP_ID",
        "SARVAM_APP_VERSION", "SARVAM_CONNECTION_ID", "SARVAM_AGENT_NUMBER", "ERRAND_PUBLIC_URL",
        "ERRAND_SARVAM_TOKEN")}


def configured(for_outbound: bool = True) -> list[str]:
    """Names of settings still missing for Sarvam phone calls."""
    e = env()
    need = ["SARVAM_ORG_ID", "SARVAM_WORKSPACE_ID", "SARVAM_APP_ID", "SARVAM_APP_VERSION", "SARVAM_CONNECTION_ID",
            "SARVAM_AGENT_NUMBER", "ERRAND_PUBLIC_URL", "ERRAND_SARVAM_TOKEN"]
    missing = [k for k in need if not e.get(k)]
    if not voice_agents_key():
        missing.insert(0, "SARVAM_VA_API_KEY")
    return missing


def voice_agents_key() -> str | None:
    """Voice Agents (Samvaad) key. The speech-API key (SARVAM_API_KEY) is a different key type and is
    only used as a last resort."""
    e = env()
    return e["SARVAM_VA_API_KEY"] or e["SARVAM_SAMVAD_API_KEY"] or e["SARVAM_API_KEY"]


def _request(service: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    e = env()
    key = voice_agents_key()
    url = f"{BASE[service]}/v1/orgs/{e['SARVAM_ORG_ID']}/workspaces/{e['SARVAM_WORKSPACE_ID']}{path}"
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-API-Key": key or "", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Sarvam API {err.code}: {detail}") from None


def connection() -> dict[str, str]:
    e = env()
    return {"connection_id": e["SARVAM_CONNECTION_ID"] or "", "agent_phone_number": e["SARVAM_AGENT_NUMBER"] or ""}


def webhook(session_id: str | None = None) -> dict[str, Any]:
    e = env()
    meta: dict[str, Any] = {"token": e["ERRAND_SARVAM_TOKEN"]}
    if session_id:
        meta["session_id"] = session_id
    return {"url": f"{(e['ERRAND_PUBLIC_URL'] or '').rstrip('/')}/api/sarvam/webhook", "metadata": meta}


def place_call(phone: str, session_id: str, first_line: str) -> str:
    """Instant outbound call that continues chat `session_id`. Returns Sarvam's attempt_id."""
    e = env()
    body = {
        "app_config": {"app_id": e["SARVAM_APP_ID"], "app_version": int(e["SARVAM_APP_VERSION"] or 1),
                       "connection_config": connection(),
                       "agent_variables": {"session_id": session_id},
                       "app_overrides": {"initial_bot_message": first_line[:500]}},
        "user_config": {"user_phone_number": phone},
        "webhook_config": webhook(session_id),
    }
    return _request("outbounds", "POST", "/outbounds", body)["attempt_id"]


def deploy_inbound() -> dict[str, Any]:
    e = env()
    return _request("deployments", "POST", "/deployments", {
        "name": f"{NAME} inbound", "app_id": e["SARVAM_APP_ID"], "app_version": int(e["SARVAM_APP_VERSION"] or 1),
        # The live API wants a list of numbers per connection here (the docs show agent_phone_number).
        "connection_configs": [{"connection_id": connection()["connection_id"],
                                "phone_numbers": [connection()["agent_phone_number"]]}],
        "description": f"{NAME}: inbound line, every turn handled by the {NAME} server",
        "inbound_config": {"start_time": "00:00", "end_time": "23:59", "timezone": "Asia/Kolkata",
                           "allowed_days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
                                            "Sunday"]},
        "webhook_config": webhook(),
    })


def set_deployment_version(deployment_id: str, version: int) -> dict[str, Any]:
    """Point a live deployment at another committed agent version (and the current name).
    Sarvam only edits paused deployments, so: pause -> edit -> resume (resume runs even if the edit fails)."""
    status = lambda action: _request("deployments", "PUT", f"/deployments/{deployment_id}/status", {"action": action})
    status("pause")
    try:
        _request("deployments", "PATCH", f"/deployments/{deployment_id}", {
            "app_version": version, "name": f"{NAME} inbound",
            "description": f"{NAME}: inbound line, every turn handled by the {NAME} server"})
    finally:
        status("resume")
    return _request("deployments", "GET", f"/deployments/{deployment_id}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "agent-spec":
        print("=== System prompt ===\n" + AGENT_PROMPT + "\n\n=== HTTP tool ===\n" + json.dumps(TOOL_SPEC, indent=2))
        return
    missing = configured()
    if cmd == "status":
        e = env()
        print("missing settings:", missing or "none")
        print("agent number:", e["SARVAM_AGENT_NUMBER"] or "-", "| app:", e["SARVAM_APP_ID"] or "-",
              "v", e["SARVAM_APP_VERSION"] or "-", "| public url:", e["ERRAND_PUBLIC_URL"] or "-")
        if not missing:
            print("deployments:", json.dumps(_request("deployments", "GET", "/deployments"), indent=1)[:1500])
        return
    if missing:
        sys.exit(f"missing settings: {', '.join(missing)} (see README → Phone calls via Sarvam)")
    if cmd == "deploy-inbound":
        print(json.dumps(deploy_inbound(), indent=1))
    elif cmd == "set-version":  # python -m errand_agent.sarvam set-version <deployment_id> <version>
        d = set_deployment_version(sys.argv[2], int(sys.argv[3]))
        print({k: d.get(k) for k in ("name", "app_version", "status")})
    elif cmd == "call":
        print("attempt_id:", place_call(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "sarvam-test",
                                        f"Hi, this is {NAME}. What needs doing at home?"))
    else:
        sys.exit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
