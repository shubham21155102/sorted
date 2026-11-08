"""Voice agent (LiveKit).

    python -m errand_agent.voice dev        # run the worker (needs the web server running)

Speech-to-text and text-to-speech run on LiveKit Inference. The "brain" is NOT a
separate LLM: each final user utterance is sent to the Errand web server's chat
API for the call's chat session, so a call uses exactly the same agent,
approvals, memory, limits and trace as typed chat, and the conversation is
saved in that chat's history. Proactive messages (e.g. a provider cancelling)
that land in the chat during a call are spoken out.

The browser joins a room via a token from the web server (/api/voice/token);
that token dispatches this worker (agent name "errand-voice") with the chat's
session id in the dispatch metadata.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from typing import Any, AsyncIterable

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, ModelSettings, cli, inference, llm

from .app import load_dotenv
from .voice_text import speakable

load_dotenv()
load_dotenv("secrets/livekit.env")

log = logging.getLogger("errand.voice")
AGENT_NAME = "errand-voice"
SERVER = os.environ.get("ERRAND_SERVER_URL", "http://127.0.0.1:8765").rstrip("/")
STT_MODEL = os.environ.get("ERRAND_STT", "deepgram/nova-3")
TTS_MODEL = os.environ.get("ERRAND_TTS", "cartesia/sonic-3")
TTS_VOICE = os.environ.get("ERRAND_TTS_VOICE") or None
ENDPOINT_DELAY = float(os.environ.get("ERRAND_ENDPOINT_DELAY", "1.0"))
GREETING = "Hi, this is Errand. What can I sort out for you?"


def _http(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 240) -> dict[str, Any]:
    req = urllib.request.Request(SERVER + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


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
    def __init__(self, session_id: str):
        super().__init__(instructions="Voice front-end for the Errand agent; replies come from the Errand server.")
        self.session_id = session_id
        self.turn_lock = asyncio.Lock()
        self.seen = 0  # messages already in the chat (so the poller only speaks new proactive ones)

    async def llm_node(self, chat_ctx: llm.ChatContext, tools: list[llm.Tool],
                       model_settings: ModelSettings) -> AsyncIterable[str]:
        user_text = next((m.text_content for m in reversed(chat_ctx.items)
                          if getattr(m, "role", None) == "user" and m.text_content), "")
        if not user_text.strip():
            return
        async with self.turn_lock:
            task = asyncio.create_task(asyncio.to_thread(
                _http, "POST", f"/api/sessions/{self.session_id}/chat", {"text": user_text, "channel": "voice"}))
            try:
                r = await asyncio.wait_for(asyncio.shield(task), timeout=2.5)
            except asyncio.TimeoutError:
                yield "One moment. "  # the agent is searching/holding; don't leave dead air
                try:
                    r = await task
                except Exception as e:
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
            r = await asyncio.to_thread(_http, "GET", f"/api/sessions/{agent.session_id}/messages?since={agent.seen}",
                                        None, 10)
        except Exception:
            continue
        agent.seen = r.get("count", agent.seen)
        for m in r.get("messages", []):
            if m["role"] == "assistant" and m.get("kind") in ("proactive", "recovery"):
                session.say(speakable(m["content"]), add_to_chat_ctx=True)


server = AgentServer()


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    meta = json.loads(ctx.job.metadata or "{}")
    session_id = meta.get("session_id") or ctx.room.name.removeprefix("errand-").rsplit("-", 1)[0]
    log.info("call for chat session %s in room %s", session_id, ctx.room.name)

    agent = ErrandVoice(session_id)
    try:
        agent.seen = (await asyncio.to_thread(_http, "GET", f"/api/sessions/{session_id}/messages", None, 10))["count"]
    except Exception:
        log.warning("errand server not reachable at %s", SERVER)

    tts_kwargs = {"voice": TTS_VOICE} if TTS_VOICE else {}
    session = AgentSession(
        stt=inference.STT(STT_MODEL, language="en"),
        llm=ErrandServerLLM(),
        tts=inference.TTS(TTS_MODEL, **tts_kwargs),
        vad=inference.VAD(),
        turn_handling={
            # A pause before deciding the caller has finished, so one sentence isn't split into several turns.
            "endpointing": {"mode": "fixed", "min_delay": ENDPOINT_DELAY, "max_delay": 4.0},
            # Must stay off: a "reply" here calls the Errand server, which can search and hold slots, so it
            # must never run speculatively on a half-finished sentence.
            "preemptive_generation": {"enabled": False},
        },
    )
    await session.start(agent=agent, room=ctx.room)
    await ctx.connect()
    session.say(GREETING, add_to_chat_ctx=True)
    poller = asyncio.create_task(speak_proactive(session, agent))
    ctx.add_shutdown_callback(lambda: poller.cancel())


if __name__ == "__main__":
    cli.run_app(server)
