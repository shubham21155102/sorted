"""Voice agent (LiveKit): browser calls, inbound phone calls and outbound phone calls.

    python -m errand_agent.voice dev        # run the worker (the web server must be running)

Speech: Sarvam (saaras speech-to-text, bulbul text-to-speech; good with Indian English,
Hindi and Hinglish) when SARVAM_API_KEY is set, otherwise LiveKit Inference
(Deepgram + Cartesia). Voice-activity detection: Silero via LiveKit Inference.

The "brain" is NOT a separate LLM: each finished utterance is sent to the Errand web
server's chat API for the call's chat session. Calls therefore use exactly the same
agent, model, approvals, idempotent booking, memory, limits and trace as typed chat,
and every call is saved as chat history.

Call types (from the dispatch metadata):
  web             browser call; the chat session comes from the token request
  outbound_phone  the agent dials the user's phone (SIP outbound trunk) and continues
                  the chat the user was in, opening with a briefing of where it stands
  inbound_phone   the user rang the Errand number (SIP inbound trunk + dispatch rule);
                  only the user's registered number is accepted, and the call gets a new chat
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from typing import Any, AsyncIterable

from livekit import api
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, ModelSettings, cli, inference, llm

from .app import load_dotenv
from .voice_text import speakable
from .brand import NAME, SLUG

load_dotenv()
load_dotenv("secrets/livekit.env")
load_dotenv("secrets/sarvam.env")
load_dotenv("secrets/telephony.env")

log = logging.getLogger("errand.voice")
AGENT_NAME = f"{SLUG}-voice"
SERVER = os.environ.get("ERRAND_SERVER_URL", "http://127.0.0.1:8765").rstrip("/")
PROVIDER = os.environ.get("ERRAND_VOICE_PROVIDER") or ("sarvam" if os.environ.get("SARVAM_API_KEY") else "livekit")
LANGUAGE = os.environ.get("ERRAND_VOICE_LANGUAGE", "en-IN")
SARVAM_SPEAKER = os.environ.get("ERRAND_SARVAM_SPEAKER", "kavya")
STT_MODEL = os.environ.get("ERRAND_STT", "deepgram/nova-3")
TTS_MODEL = os.environ.get("ERRAND_TTS", "cartesia/sonic-3")
TTS_VOICE = os.environ.get("ERRAND_TTS_VOICE") or None
ENDPOINT_DELAY = float(os.environ.get("ERRAND_ENDPOINT_DELAY", "1.0"))
OUTBOUND_TRUNK = os.environ.get("ERRAND_SIP_OUTBOUND_TRUNK_ID")


def _http(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 240) -> dict[str, Any]:
    req = urllib.request.Request(SERVER + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


async def server_call(method: str, path: str, body: dict[str, Any] | None = None,
                      timeout: float = 240) -> dict[str, Any]:
    return await asyncio.to_thread(_http, method, path, body, timeout)


def speech_stack(persona: dict[str, Any] | None = None) -> tuple[Any, Any]:
    """persona: the user's voice choice from the server (speaker, pace); env settings are the fallback."""
    if PROVIDER == "sarvam":
        from livekit.plugins import sarvam
        p = persona or {}
        return (sarvam.STT(language=LANGUAGE, model="saaras:v4"),
                sarvam.TTS(target_language_code=LANGUAGE, model="bulbul:v3",
                           speaker=p.get("speaker") or SARVAM_SPEAKER, pace=float(p.get("pace") or 1.0)))
    return (inference.STT(STT_MODEL, language="en"),
            inference.TTS(TTS_MODEL, **({"voice": TTS_VOICE} if TTS_VOICE else {})))


class ErrandServerLLM(llm.LLM):
    """Marker LLM. LiveKit only generates replies when the session has an LLM, but every turn is
    answered by ErrandVoice.llm_node (the Errand server), so chat() is never used."""

    @property
    def model(self) -> str:
        return "errand-server"

    @property
    def provider(self) -> str:
        return "errand"

    def chat(self, **kwargs: Any) -> llm.LLMStream:
        raise RuntimeError("ErrandServerLLM.chat should never be called: llm_node is overridden")


class ErrandVoice(Agent):
    def __init__(self, session_id: str, channel: str):
        super().__init__(instructions=f"Voice front-end for the {NAME} agent; replies come from the {NAME} server.")
        self.session_id = session_id
        self.channel = channel  # "voice" (browser) or "phone"
        self.turn_lock = asyncio.Lock()
        self.seen = 0  # messages already in the chat (so the poller only speaks new proactive ones)

    async def llm_node(self, chat_ctx: llm.ChatContext, tools: list[llm.Tool],
                       model_settings: ModelSettings) -> AsyncIterable[str]:
        user_text = next((m.text_content for m in reversed(chat_ctx.items)
                          if getattr(m, "role", None) == "user" and m.text_content), "")
        if not user_text.strip():
            return
        async with self.turn_lock:
            task = asyncio.create_task(server_call("POST", f"/api/sessions/{self.session_id}/chat",
                                                   {"text": user_text, "channel": self.channel}))
            try:
                r = await asyncio.wait_for(asyncio.shield(task), timeout=2.5)
            except asyncio.TimeoutError:
                yield "One moment. "  # the agent is searching/holding; don't leave dead air
                try:
                    r = await task
                except Exception:
                    log.exception("chat call failed")
                    yield "Sorry, I couldn't reach the errand service. Nothing was booked."
                    return
            except Exception:
                log.exception("chat call failed")
                yield "Sorry, I couldn't reach the errand service. Nothing was booked."
                return
            self.seen = r.get("count", self.seen)
            yield speakable(r.get("reply", ""))


async def speak_proactive(session: AgentSession, agent: ErrandVoice) -> None:
    """Speak messages that arrive in the chat without the caller asking (provider cancellation, recovery)."""
    while True:
        await asyncio.sleep(3)
        if agent.turn_lock.locked():
            continue
        try:
            r = await server_call("GET", f"/api/sessions/{agent.session_id}/messages?since={agent.seen}", None, 10)
        except Exception:
            continue
        agent.seen = r.get("count", agent.seen)
        for m in r.get("messages", []):
            if m["role"] == "assistant" and m.get("kind") in ("proactive", "recovery"):
                session.say(speakable(m["content"]), add_to_chat_ctx=True)


async def note(session_id: str, text: str) -> None:
    """Write a call marker (started / connected / ended / unreachable) into the chat history."""
    try:
        await server_call("POST", f"/api/sessions/{session_id}/call_event", {"text": text}, 10)
    except Exception:
        log.warning("could not write call event to chat %s", session_id)


def make_session(persona: dict[str, Any] | None = None) -> AgentSession:
    stt, tts = speech_stack(persona)
    return AgentSession(
        stt=stt, llm=ErrandServerLLM(), tts=tts, vad=inference.VAD(),
        turn_handling={
            # A pause before deciding the caller has finished, so one sentence isn't split into several turns.
            "endpointing": {"mode": "fixed", "min_delay": ENDPOINT_DELAY, "max_delay": 4.0},
            # Must stay off: a "reply" here calls the Errand server, which can search and hold slots, so it
            # must never run speculatively on a half-finished sentence.
            "preemptive_generation": {"enabled": False},
        },
    )


async def hang_up(ctx: JobContext) -> None:
    try:
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
    except Exception:
        ctx.shutdown()


def mask(phone: str) -> str:
    return phone[:3] + "•••" + phone[-3:] if len(phone) > 6 else phone


server = AgentServer()


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    meta = json.loads(ctx.job.metadata or "{}")
    mode = meta.get("mode", "web")
    started = time.time()
    log.info("call: mode=%s room=%s speech=%s", mode, ctx.room.name, PROVIDER)
    await ctx.connect()
    try:  # the user's voice persona (voice, speed, name used in greetings)
        persona = (await server_call("GET", "/api/voice/config", None, 10)).get("persona") or {}
    except Exception:
        persona = {}

    # ---------------------------------------------------------- whose call is this?
    if mode == "inbound_phone":
        caller = await ctx.wait_for_participant()
        number = caller.attributes.get("sip.phoneNumber", "")
        r = await server_call("POST", "/api/phone/inbound", {"from": number}, 15)
        if not r.get("allowed"):
            log.warning("inbound call from an unregistered number was rejected")
            session = make_session()
            await session.start(agent=ErrandVoice("", "phone"), room=ctx.room)
            await session.say(f"Sorry, this line only serves registered {NAME} users. Goodbye.",
                              allow_interruptions=False)
            await hang_up(ctx)
            return
        session_id, channel = r["session_id"], "phone"
        greeting = persona.get("greeting") or f"Hi {r.get('user_name', '')}, this is {NAME}. What needs doing at home?"
        await note(session_id, "📞 Incoming phone call started")
    else:
        session_id = meta.get("session_id") or ctx.room.name.removeprefix("errand-").split("-out-")[0].rsplit("-", 1)[0]
        channel = "phone" if mode == "outbound_phone" else "voice"
        greeting = persona.get("greeting") or f"Hi, this is {NAME}. What needs doing at home?"
        if mode == "outbound_phone":
            try:
                b = await server_call("GET", f"/api/sessions/{session_id}/brief", None, 30)
                greeting = (persona.get("outbound_greeting") or f"Hi {b.get('user_name', '')}, this is {NAME}.") + \
                    " " + speakable(b.get("text", ""))
            except Exception:
                greeting = f"Hi, this is {NAME}, calling about your request. How can I help?"
        else:
            await note(session_id, "🎙 Voice call started")

    agent = ErrandVoice(session_id, channel)
    try:
        agent.seen = (await server_call("GET", f"/api/sessions/{session_id}/messages", None, 10))["count"]
    except Exception:
        log.warning("errand server not reachable at %s", SERVER)

    session = make_session(persona)
    await session.start(agent=agent, room=ctx.room)

    # -------------------------------------------------------------- outbound dialing
    if mode == "outbound_phone":
        phone = meta.get("phone", "")
        await note(session_id, f"📞 Calling {mask(phone)}…")
        try:
            await ctx.api.sip.create_sip_participant(api.CreateSIPParticipantRequest(
                room_name=ctx.room.name, sip_trunk_id=OUTBOUND_TRUNK or "", sip_call_to=phone,
                participant_identity=f"phone-{session_id}", participant_name="Phone",
                wait_until_answered=True))
        except Exception as e:
            log.warning("outbound call failed: %s", e)
            status = (getattr(e, "metadata", None) or {}).get("sip_status", "")
            await note(session_id, f"📞 Couldn't reach {mask(phone)}{f' ({status})' if status else ''}. "
                                   "You can keep chatting here or try the call again.")
            await hang_up(ctx)
            return
        agent.seen = (await server_call("GET", f"/api/sessions/{session_id}/messages", None, 10))["count"]

    session.say(greeting, add_to_chat_ctx=True)
    poller = asyncio.create_task(speak_proactive(session, agent))
    ended = asyncio.Event()

    async def on_end() -> None:
        if ended.is_set():
            return
        ended.set()
        poller.cancel()
        mins, secs = divmod(int(time.time() - started), 60)
        icon, what = ("📞", "Phone call") if channel == "phone" else ("🎙", "Voice call")
        await note(session_id, f"{icon} {what} ended · {mins}m {secs:02d}s")

    # The session closes when the caller hangs up, but the job keeps running until the room is gone:
    # record the end of the call then, and release the job.
    @session.on("close")
    def _closed(_ev: Any) -> None:
        async def finish() -> None:
            await on_end()
            ctx.shutdown(reason="call ended")
        asyncio.create_task(finish())

    ctx.add_shutdown_callback(on_end)


if __name__ == "__main__":
    cli.run_app(server)
