"""The voice -> text -> route -> agent pipeline, independent of Telegram.

Every message is checked with the access point before anything that costs money (speech
recognition, Claude) and reported to it afterwards, see access.py.
"""

import asyncio
import io
import logging
import time
import wave
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from typing import Any

from .access import Access, Allow, Check, Outcome, Reason, Source, Usage
from .chat import Channel, ChatMessage, split_message, validate
from .gamectx import GameContext
from .hub import Ack, AckStatus, AgentHub, VoiceUpload
from .i18n import LANGUAGES, t
from .router import Decision, LLMRouter, RouteContext, RouteResult, rule_route
from .store import Store
from .stt import Transcriber, TranscriptionError

MIN_VOICE_MS = 300  # shorter clips are accidental presses
# Nicknames in the speech recognition hint: whisper models read at most 224 tokens of prompt
# and keep its end, so the hint stays short and the likeliest names come first.
STT_MAX_NAMES = 30
STT_MAX_NAMES_CHARS = 300

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Delivery:
    ack: Ack
    message: ChatMessage
    parts: list[str]


class RateLimit:
    """At most `per_minute` messages per player in any 60 seconds; 0 = no limit."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._times: defaultdict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int, now: float | None = None) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic() if now is None else now
        times = self._times[user_id]
        while times and now - times[0] >= 60:
            times.popleft()
        if len(times) >= self.per_minute:
            return False
        times.append(now)
        return True


class VoiceService:
    def __init__(
        self,
        store: Store,
        hub: AgentHub,
        transcriber: Transcriber,
        router: LLMRouter | None,
        max_message_bytes: int = 255,
        global_names: tuple[str, ...] = (),
        max_voice_s: float = 60.0,
        access: Access | None = None,
        rate_per_min: int = 20,
    ) -> None:
        self.store = store
        self.hub = hub
        self.transcriber = transcriber
        self.router = router
        self.max_message_bytes = max_message_bytes
        self.global_names = list(global_names)
        self.max_voice_s = max_voice_s
        self.access: Access = access if access is not None else hub.access
        self.rate = RateLimit(rate_per_min)
        self._reports: set[asyncio.Task[None]] = set()

    def context(self, user_id: int) -> RouteContext:
        s = self.store.get(user_id)
        return RouteContext(
            default_channel=s.default_channel,
            rewrite_style=s.rewrite_style,
            known_names=list(dict.fromkeys([*self.global_names, *s.names()])),
            recent=self.store.recent(user_id),
            game=self.store.game(user_id),
        )

    def set_game_context(self, user_id: int, data: dict[str, Any] | None) -> None:
        """Names and channels from the player's game, from the agent; None forgets them."""
        if data is None:
            self.store.set_game(user_id, None)
            log.info("user %s: names from the game turned off", user_id)
            return
        try:
            context = GameContext.parse(data)
        except ValueError as e:
            log.warning("user %s: unusable names from the game: %s", user_id, e)
            return
        self.store.set_game(user_id, context)
        log.info("user %s: names from the game (%s): %s", user_id, context.source, context.counts())

    def stt_names(self, user_id: int) -> list[str]:
        """Nicknames for the speech recognition hint: the player's own (/names), then the game's."""
        ctx = self.context(user_id)
        game = ctx.game.names() if ctx.game is not None else []
        channels = [c.name.split(" - ")[0] for c in ctx.game.channels] if ctx.game is not None else []
        names, size = [], 0
        for name in dict.fromkeys([*ctx.known_names, *(n.split("-")[0] for n in game), *channels]):
            if len(names) >= STT_MAX_NAMES or size + len(name) + 2 > STT_MAX_NAMES_CHARS:
                break
            names.append(name)
            size += len(name) + 2
        return names

    def speech_language(self, user_id: int) -> str:
        """The language of the hints sent with the audio: the one speech recognition is
        locked to if it is one of the bot's, else the player's (see Store.ui().language())."""
        locked = getattr(self.transcriber, "language", "")
        return locked if locked in LANGUAGES else self.store.ui(user_id).language()

    async def transcribe(self, user_id: int, audio: bytes, filename: str) -> str:
        lang = self.speech_language(user_id)
        names = self.stt_names(user_id)
        hint = t(lang, "stt.names", names=", ".join(names)) if names else ""
        return await self.transcriber.transcribe(audio, filename, extra_prompt=hint, lang=lang)

    async def admit(
        self, user_id: int, source: Source, *, voice: bool, audio_s: float = 0.0
    ) -> Check:
        """May this message be processed, and with Claude or by the rules only?"""
        if not self.rate.allow(user_id):
            return Check(Allow.NO, reason=Reason.RATE)
        check = await self.access.check(user_id, source, voice=voice, audio_s=audio_s)
        if check.account is not None:
            await self.hub.send_account(user_id, check.account)
        return check

    async def route(self, user_id: int, transcript: str, allow: Allow = Allow.CLAUDE) -> RouteResult:
        ctx = self.context(user_id)
        if self.router is None:
            return rule_route(transcript, ctx)
        if allow is not Allow.CLAUDE:
            return rule_route(transcript, ctx)
        return await self.router.route(transcript, ctx)

    def report(
        self,
        check: Check,
        user_id: int,
        source: Source,
        outcome: Outcome,
        audio_s: float = 0.0,
        result: RouteResult | None = None,
    ) -> None:
        """Tell the access point what a checked message became and what it cost.
        In the background: the player does not wait for the bookkeeping."""
        usage = Usage(
            check_id=check.check_id,
            user_id=user_id,
            source=source,
            outcome=outcome,
            ai=result is not None and result.source == "llm",
            audio_s=audio_s,
            tokens=result.usage if result is not None else None,
        )
        task = asyncio.create_task(self._report(usage))
        self._reports.add(task)
        task.add_done_callback(self._reports.discard)

    async def _report(self, usage: Usage) -> None:
        try:
            account = await self.access.usage(usage)
        except Exception:
            log.exception("usage report for user %s failed", usage.user_id)
            return
        if account is not None:
            await self.hub.send_account(usage.user_id, account)

    async def drain(self) -> None:
        """Wait for the usage reports still on their way (shutdown, tests)."""
        while self._reports:
            await asyncio.gather(*self._reports, return_exceptions=True)

    async def deliver(self, user_id: int, message: ChatMessage, voice_id: str = "") -> Delivery:
        default = self.store.get(user_id).default_channel
        message = validate(message.channel, message.target, message.text, default)
        if not (parts := split_message(message.text, self.max_message_bytes)):
            return Delivery(Ack(AckStatus.ERROR, "empty message"), message, [])
        if message.channel is Channel.CHANNEL and not message.target.isdigit():
            # The addon finds a channel by name; the plain chat box only takes its number.
            info = self.hub.info(user_id)
            if info is not None and info.mode == "chat":
                game = self.store.game(user_id)
                if (found := game.channel(message.target) if game else None) is None:
                    return Delivery(Ack(AckStatus.ERROR, "a channel by name needs the addon"), message, [])
                message = ChatMessage(message.channel, str(found.n), message.text)
        ack = await self.hub.send(user_id, message, parts, voice_id)
        if ack.ok:
            self.store.remember(user_id, message.channel, message.target, message.text)
        log.info("user %s -> %s: %s", user_id, message.channel, ack.status)
        return Delivery(ack, message, parts)

    async def handle_agent_voice(self, user_id: int, upload: VoiceUpload) -> dict[str, Any]:
        """Push-to-talk from the agent: the same pipeline as a Telegram voice message.

        The "confirm before sending" setting is for Telegram buttons and does not apply:
        the player sees the result in the game and in the agent window.
        """
        if upload.format != "wav":
            return {"status": "bad_audio", "detail": f"format {upload.format} is not supported"}
        try:
            duration_s = wav_duration_s(upload.audio)
        except (wave.Error, EOFError) as e:
            return {"status": "bad_audio", "detail": f"not a WAV file: {e}"}
        if duration_s * 1000 < MIN_VOICE_MS:
            return {"status": "empty", "detail": "the recording is too short"}
        if duration_s > self.max_voice_s:
            return {"status": "too_long", "detail": f"the recording is longer than {self.max_voice_s:g} s"}
        check = await self.admit(user_id, Source.AGENT, voice=True, audio_s=duration_s)
        if not check.ok:
            return denied(check)

        def report(outcome: Outcome, result: RouteResult | None = None) -> None:
            self.report(check, user_id, Source.AGENT, outcome, duration_s, result)

        try:
            transcript = await self.transcribe(user_id, upload.audio, "voice.wav")
        except TranscriptionError as e:
            log.error("STT failed for agent voice: %s", e)
            report(Outcome.STT_ERROR)
            return {"status": "stt_error", "detail": "speech recognition failed"}
        if not transcript:
            report(Outcome.EMPTY)
            return {"status": "empty", "detail": "no words heard"}
        result = with_access_note(await self.route(user_id, transcript, check.allow), check)
        if result.decision is Decision.SKIP:
            report(Outcome.SKIPPED, result)
            return {
                "status": "skip",
                "detail": "does not look like a chat message",
                "transcript": transcript,
            } | message_fields(result.message) | ai_fields(result)
        delivery = await self.deliver(user_id, result.message, upload.id)
        report(Outcome.DELIVERED if delivery.ack.ok else Outcome.FAILED, result)
        return {
            "status": str(delivery.ack.status),
            "detail": delivery.ack.detail,
            "transcript": transcript,
            "parts": len(delivery.parts),
        } | message_fields(delivery.message) | ai_fields(result)


def wav_duration_s(audio: bytes) -> float:
    with wave.open(io.BytesIO(audio), "rb") as w:
        if w.getsampwidth() == 0 or w.getframerate() <= 0:
            raise wave.Error("empty header")
        return w.getnframes() / w.getframerate()


def denied(check: Check) -> dict[str, Any]:
    """The voice_result for a refused recording. A revoked token also ends the connection."""
    result: dict[str, Any] = {"status": "denied", "reason": check.reason, "detail": check.reason}
    if check.link:
        result["link"] = check.link
    if check.reason == Reason.REVOKED:
        result["disconnect"] = True
    return result


def with_access_note(result: RouteResult, check: Check) -> RouteResult:
    """When today's AI messages are used up, say so where the rules picked the channel."""
    if result.source == "rules" and check.allow is Allow.TRANSCRIBE and check.reason == Reason.LIMIT:
        return replace(result, note="ai_limit")
    return result


def ai_fields(result: RouteResult) -> dict[str, Any]:
    """Whether Claude handled the phrase; if not and there is a reason, the note key."""
    fields: dict[str, Any] = {"ai": result.source == "llm"}
    if result.note:
        fields["note"] = result.note
    return fields


def message_fields(message: ChatMessage) -> dict[str, str]:
    return {
        "channel": str(message.channel),
        "target": message.target,
        "label": message.label(),
        "text": message.text,
    }
