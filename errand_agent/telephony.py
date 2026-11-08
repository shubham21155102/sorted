"""Phone-call setup for the voice agent (LiveKit SIP).

You need a phone number from a SIP provider (Twilio, Plivo, Exotel, Telnyx, Vonage, ...)
whose SIP trunk points at your LiveKit project's SIP endpoint. Then:

    python -m errand_agent.telephony setup --number +91XXXXXXXXXX \\
        --sip-address yourtrunk.pstn.twilio.com --sip-username USER --sip-password PASS
    python -m errand_agent.telephony status
    python -m errand_agent.telephony call +91XXXXXXXXXX       # test an outbound call
    python -m errand_agent.telephony teardown                 # remove what setup created

`setup` creates (in your LiveKit project):
  * an inbound trunk for the number,
  * a dispatch rule that puts each caller in their own room and dispatches the
    "errand-voice" agent with {"mode": "inbound_phone"},
  * an outbound trunk (provider address + credentials) used for "Call me",
and writes the ids to secrets/telephony.env (gitignored), which the server and the
voice worker read. --inbound-only / --outbound-only set up one direction.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from livekit import api

from .app import load_dotenv
from .brand import SLUG

ENV_PATH = os.path.join("secrets", "telephony.env")
AGENT_NAME = f"{SLUG}-voice"


def _client() -> api.LiveKitAPI:
    load_dotenv(os.path.join("secrets", "livekit.env"))
    load_dotenv(ENV_PATH)
    for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        if not os.environ.get(k):
            sys.exit(f"{k} is missing (put the LiveKit credentials in secrets/livekit.env)")
    return api.LiveKitAPI(os.environ["LIVEKIT_URL"].replace("wss://", "https://"),
                          os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])


def _write_env(values: dict[str, str]) -> None:
    os.makedirs("secrets", exist_ok=True)
    current: dict[str, str] = {}
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                current[k] = v
    current.update({k: v for k, v in values.items() if v})
    with open(ENV_PATH, "w") as f:
        f.write("# Written by `python -m errand_agent.telephony setup`. Gitignored.\n")
        for k, v in current.items():
            f.write(f"{k}={v}\n")


async def setup(a: argparse.Namespace) -> None:
    lk = _client()
    out: dict[str, str] = {"ERRAND_INBOUND_NUMBER": a.number}
    try:
        if not a.outbound_only:
            trunk = await lk.sip.create_inbound_trunk(api.CreateSIPInboundTrunkRequest(trunk=api.SIPInboundTrunkInfo(
                name=f"{SLUG}-inbound", numbers=[a.number], krisp_enabled=True)))
            rule = await lk.sip.create_dispatch_rule(api.CreateSIPDispatchRuleRequest(
                name=f"{SLUG}-inbound", trunk_ids=[trunk.sip_trunk_id],
                rule=api.SIPDispatchRule(dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                    room_prefix=f"{SLUG}-phone-")),
                room_config=api.RoomConfiguration(agents=[api.RoomAgentDispatch(
                    agent_name=AGENT_NAME, metadata=json.dumps({"mode": "inbound_phone"}))])))
            out["ERRAND_SIP_INBOUND_TRUNK_ID"] = trunk.sip_trunk_id
            out["ERRAND_SIP_DISPATCH_RULE_ID"] = rule.sip_dispatch_rule_id
            print(f"inbound trunk {trunk.sip_trunk_id} + dispatch rule {rule.sip_dispatch_rule_id} created")
        if not a.inbound_only:
            if not a.sip_address:
                sys.exit("--sip-address is required for outbound calls (or pass --inbound-only)")
            trunk = await lk.sip.create_outbound_trunk(api.CreateSIPOutboundTrunkRequest(trunk=api.SIPOutboundTrunkInfo(
                name=f"{SLUG}-outbound", address=a.sip_address, numbers=[a.number],
                auth_username=a.sip_username or "", auth_password=a.sip_password or "")))
            out["ERRAND_SIP_OUTBOUND_TRUNK_ID"] = trunk.sip_trunk_id
            print(f"outbound trunk {trunk.sip_trunk_id} created")
    finally:
        await lk.aclose()
    _write_env(out)
    print(f"saved ids to {ENV_PATH}. Restart the server (with --voice) to pick them up.")


async def status(_: argparse.Namespace) -> None:
    lk = _client()
    try:
        ins = (await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())).items
        outs = (await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())).items
        rules = (await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())).items
    finally:
        await lk.aclose()
    print(f"inbound trunks:  {[(t.sip_trunk_id, t.name, list(t.numbers)) for t in ins] or 'none'}")
    print(f"outbound trunks: {[(t.sip_trunk_id, t.name, t.address, list(t.numbers)) for t in outs] or 'none'}")
    print(f"dispatch rules:  {[(r.sip_dispatch_rule_id, r.name, list(r.trunk_ids)) for r in rules] or 'none'}")
    print(f"configured here: outbound={os.environ.get('ERRAND_SIP_OUTBOUND_TRUNK_ID') or '-'} "
          f"inbound number={os.environ.get('ERRAND_INBOUND_NUMBER') or '-'}")


async def call(a: argparse.Namespace) -> None:
    """Test outbound call: dispatch the agent into a fresh room; it dials the number (needs the worker running)."""
    import uuid
    lk = _client()
    room = f"errand-{a.session}-out-{uuid.uuid4().hex[:6]}"
    try:
        d = await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
            agent_name=AGENT_NAME, room=room,
            metadata=json.dumps({"session_id": a.session, "mode": "outbound_phone", "phone": a.number})))
    finally:
        await lk.aclose()
    print(f"dispatched {d.id} to room {room}; the voice worker will dial {a.number}")


async def teardown(_: argparse.Namespace) -> None:
    lk = _client()
    try:
        for key in ("ERRAND_SIP_DISPATCH_RULE_ID",):
            if os.environ.get(key):
                await lk.sip.delete_dispatch_rule(api.DeleteSIPDispatchRuleRequest(sip_dispatch_rule_id=os.environ[key]))
                print(f"deleted dispatch rule {os.environ[key]}")
        for key in ("ERRAND_SIP_INBOUND_TRUNK_ID", "ERRAND_SIP_OUTBOUND_TRUNK_ID"):
            if os.environ.get(key):
                await lk.sip.delete_trunk(api.DeleteSIPTrunkRequest(sip_trunk_id=os.environ[key]))
                print(f"deleted trunk {os.environ[key]}")
    finally:
        await lk.aclose()
    if os.path.exists(ENV_PATH):
        os.remove(ENV_PATH)


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m errand_agent.telephony")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="create trunks + dispatch rule for a phone number")
    s.add_argument("--number", required=True, help="the app's phone number, E.164 (e.g. +918069XXXXXX)")
    s.add_argument("--sip-address", help="provider SIP host for outbound calls (e.g. xyz.pstn.twilio.com)")
    s.add_argument("--sip-username")
    s.add_argument("--sip-password")
    s.add_argument("--inbound-only", action="store_true")
    s.add_argument("--outbound-only", action="store_true")
    sub.add_parser("status", help="show trunks and dispatch rules")
    c = sub.add_parser("call", help="test an outbound call")
    c.add_argument("number")
    c.add_argument("--session", default="telephony-test", help="chat session to attach the call to")
    sub.add_parser("teardown", help="delete what setup created")
    a = ap.parse_args()
    asyncio.run({"setup": setup, "status": status, "call": call, "teardown": teardown}[a.cmd](a))


if __name__ == "__main__":
    main()
