"""Decide where a spoken phrase goes in WoW chat and clean up its text.

Two routers share one interface:
* LLMRouter - Claude reads the phrase plus recent context, picks the channel, removes
  the routing words, fixes punctuation and (optionally) rewrites the message.
* rule_route - keyword fallback used when the LLM is off, slow or fails.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

import anthropic

from .access import TokenUsage
from .chat import Channel, ChatMessage, validate

log = logging.getLogger(__name__)

RECENT_WINDOW_S = 300  # a channel used within 5 minutes is the default for the next phrase


class Decision(StrEnum):
    SEND = "send"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class SentRecord:
    at: float
    channel: Channel
    target: str
    text: str


@dataclass(slots=True)
class RouteContext:
    default_channel: Channel = Channel.SAY
    rewrite_style: str = ""  # empty = punctuation fixes only
    known_names: list[str] = field(default_factory=list)
    recent: list[SentRecord] = field(default_factory=list)

    def last_channel(self, now: float | None = None) -> tuple[Channel, str] | None:
        now = time.time() if now is None else now
        match self.recent:
            case [*_, last] if now - last.at <= RECENT_WINDOW_S:
                return last.channel, last.target
            case _:
                return None


@dataclass(frozen=True, slots=True)
class RouteResult:
    decision: Decision
    message: ChatMessage
    source: str  # "llm" or "rules"
    note: str = ""  # why the rules were used: a key under note.* in the locales, e.g. "llm_down"
    note_code: int = 0  # the LLM's HTTP status for note "llm_error"
    usage: TokenUsage | None = None  # what the Claude request used, if one was made


# --------------------------------------------------------------------------- rules

_PREFIXES: tuple[tuple[str, Channel, str], ...] = (
    (r"(?:в\s+)?(?:пати|группу|группе|party)", Channel.GROUP, ""),
    (r"(?:объяви\s+рейду|рейд\s+варн(?:инг)?)", Channel.RAID_WARNING, ""),
    (r"(?:в\s+)?(?:рейд|рейду|raid)", Channel.RAID, ""),
    (r"(?:в\s+)?(?:инст|данж|подземелье)", Channel.INSTANCE, ""),
    (r"(?:в\s+)?(?:гильдию|гильдии|гильдия|ги|guild)", Channel.GUILD, ""),
    (r"(?:офицерам|в\s+офицерский)", Channel.OFFICER, ""),
    (r"(?:крикни|прокричи)", Channel.YELL, ""),
    (r"(?:скажи\s+вслух|вслух|say)", Channel.SAY, ""),
    (r"(?:эмоция|эмоут)", Channel.EMOTE, ""),
    (r"(?:ответь|ответ)(?:\s+(?:ему|ей|им))?", Channel.REPLY, ""),
    (r"(?:в\s+)?(?:торговлю|трейд|торговый)", Channel.CHANNEL, "2"),
    (r"(?:в\s+)?(?:общий(?:\s+канал)?)", Channel.CHANNEL, "1"),
)
_WHISPER_RE = re.compile(
    r"^(?:шепни|шёпот|шепот|напиши|whisper)\s+([^\s,.:!?]+)[\s,.:!?—-]*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_THAT_RE = re.compile(r"^(?:скажи\s+|напиши\s+)?(?:что|чтобы)\s+", re.IGNORECASE)


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def _body(text: str) -> str:
    return _capitalize(_THAT_RE.sub("", text.strip()))


def rule_route(transcript: str, ctx: RouteContext, now: float | None = None) -> RouteResult:
    text = transcript.strip()

    if m := _WHISPER_RE.match(text):
        msg = validate(Channel.WHISPER, m[1].capitalize(), _body(m[2]), ctx.default_channel)
        return RouteResult(Decision.SEND, msg, "rules")

    for pattern, channel, target in _PREFIXES:
        if m := re.match(rf"^{pattern}\b[\s,.:!?—-]*(.*)$", text, re.IGNORECASE | re.DOTALL):
            msg = validate(channel, target, _body(m[1]), ctx.default_channel)
            return RouteResult(Decision.SEND, msg, "rules")

    channel, target = ctx.last_channel(now) or (ctx.default_channel, "")
    msg = validate(channel, target, _capitalize(text), ctx.default_channel)
    return RouteResult(Decision.SEND, msg, "rules")


# --------------------------------------------------------------------------- LLM

SYSTEM_PROMPT = """You route a World of Warcraft player's dictated phrase into the game chat.
The player plays with a gamepad and speaks instead of typing. The phrase comes from \
speech recognition, so it may have recognition errors and no punctuation.

Decide:
1. channel - where the message goes:
   say, yell, emote, group (party/raid/instance chat - the game addon picks the right one), \
party, raid, raid_warning, instance, guild, officer, whisper (needs target = character name), \
reply (answer the last person who whispered the player), channel (numbered chat channel; \
target = its number, 1 = General, 2 = Trade).
   - An explicit routing phrase wins: "в пати", "в рейд", "в гильдию", "офицерам", \
"крикни", "шепни Арторн", "ответь ему", "в трейд".
   - "ответь", "ответь ему/ей" without a name means reply.
   - Without a routing phrase, continue the conversation the player is in: use the channel \
of recent messages if the last one was under 5 minutes ago and the phrase fits it; \
otherwise use default_channel.
   - Prefer "group" over "party"/"raid"/"instance" unless the player names one of those.
2. target - character name for whisper (match known_names when the spoken name sounds \
like one of them, keep the game spelling), channel number for channel, otherwise "".
3. text - what to post:
   - Remove the routing words ("скажи в пати", "шепни Арторну", "напиши в гильдию").
   - Turn reported speech into the message itself: "скажи в пати, что я отойду на пять \
минут" -> "Отойду на 5 минут".
   - Fix punctuation, capitalisation and obvious recognition errors; write numbers as digits; \
keep the player's wording, slang and language. Do not add content, greetings or emoji.
   - If rewrite_style is set, rewrite the text in that style (it may ask for a translation \
or a role-play voice) while keeping the meaning.
   - Never start the text with "/" or ".", never include "|" or line breaks.
4. action - "skip" only when the phrase is clearly not meant for chat (noise, a cough, \
"отмена", "не отправляй", talking to someone in the room); otherwise "send".
"""

ROUTE_SCHEMA: frozendict[str, Any] = frozendict({
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [d.value for d in Decision]},
        "channel": {"type": "string", "enum": [c.value for c in Channel]},
        "target": {"type": "string"},
        "text": {"type": "string"},
    },
    "required": ["action", "channel", "target", "text"],
    "additionalProperties": False,
})

# Models that accept server-side refusal fallbacks (fallbacks="default").
_FALLBACK_PREFIXES = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")


class RouterOutputError(Exception):
    def __init__(self, reason: str, usage: TokenUsage | None = None) -> None:
        super().__init__(reason)
        self.usage = usage


def token_usage(response: Any) -> TokenUsage | None:
    """The tokens a response used, for the cost report; None if it carries no usage."""
    if (u := getattr(response, "usage", None)) is None:
        return None
    return TokenUsage(
        model=str(getattr(response, "model", "") or ""),
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )


class LLMRouter:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        model: str = "claude-opus-5-5",
        effort: str = "low",
        allow_transform: bool = True,
    ) -> None:
        self.client = client
        self.model = model
        self.effort = effort
        self.allow_transform = allow_transform

    def build_input(self, transcript: str, ctx: RouteContext, now: float | None = None) -> str:
        now = time.time() if now is None else now
        payload = {
            "phrase": transcript,
            "default_channel": ctx.default_channel,
            "rewrite_style": ctx.rewrite_style if self.allow_transform else "",
            "known_names": ctx.known_names,
            "recent_messages": [
                {
                    "seconds_ago": int(now - r.at),
                    "channel": r.channel,
                    "target": r.target,
                    "text": r.text,
                }
                for r in ctx.recent[-8:]
            ],
        }
        return json.dumps(payload, ensure_ascii=False)

    async def route(self, transcript: str, ctx: RouteContext) -> RouteResult:
        def fallback(note: str, code: int = 0, usage: TokenUsage | None = None) -> RouteResult:
            return replace(rule_route(transcript, ctx), note=note, note_code=code, usage=usage)

        try:
            data, usage = await self._ask(self.build_input(transcript, ctx))
        except anthropic.RateLimitError:
            log.warning("LLM rate limited, using rules")
            return fallback("llm_limit")
        except anthropic.APIStatusError as e:
            log.error("LLM error %s: %s", e.status_code, e.message)
            return fallback("llm_error", e.status_code)
        except anthropic.APIConnectionError as e:  # includes APITimeoutError
            log.warning("LLM unreachable, using rules: %s", e)
            return fallback("llm_down")
        except RouterOutputError as e:
            log.warning("LLM output unusable (%s), using rules", e)
            return fallback("llm_format", usage=e.usage)

        msg = validate(data["channel"], data["target"], data["text"], ctx.default_channel)
        if data["action"] == Decision.SKIP or not msg.text:
            return RouteResult(Decision.SKIP, msg, "llm", usage=usage)
        return RouteResult(Decision.SEND, msg, "llm", usage=usage)

    async def _ask(self, user_input: str) -> tuple[dict[str, str], TokenUsage | None]:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_input}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": dict(ROUTE_SCHEMA)},
            },
        }
        if self.model.startswith(_FALLBACK_PREFIXES):
            response = await self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request
            )
        else:
            response = await self.client.messages.create(**request)

        usage = token_usage(response)
        match response.stop_reason:
            case "refusal" | "max_tokens" as reason:
                raise RouterOutputError(reason, usage)
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise RouterOutputError(f"bad json: {e}", usage) from e
        match data:
            case {"action": str(), "channel": str(), "target": str(), "text": str()}:
                return data, usage
            case _:
                raise RouterOutputError("missing fields", usage)
