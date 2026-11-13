"""Who may use the server and what each message may do.

The server asks an access point three things: who a token or a Telegram user is (`resolve`),
whether a message may be processed and with what (`check`, which may also name the language
the player speaks), and what a processed message cost (`usage`). A fourth call, `payment`,
lets the Telegram bot sell a subscription for Telegram Stars without keeping any billing here.

Two access points share one interface:
* LocalAccess - the people listed in VF_USERS, without limits (self-hosted, as before).
* RemoteAccess - an HTTP service at VF_ACCESS_URL that keeps accounts, subscriptions and
  limits (see docs/access-api.md). If it does not answer in time, nothing is processed.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import aiohttp

log = logging.getLogger(__name__)


class Allow(StrEnum):
    NO = "no"
    TRANSCRIBE = "transcribe"  # speech recognition and the keyword rules, no Claude
    CLAUDE = "claude"


class Source(StrEnum):
    AGENT = "agent"
    TELEGRAM = "telegram"


class Outcome(StrEnum):
    """What became of a checked message, reported with `usage`."""

    DELIVERED = "delivered"  # typed into the game
    ROUTED = "routed"  # shown to the player as a card or with buttons; sent on their tap
    FAILED = "failed"  # the agent did not type it (offline, WoW not focused, timeout)
    SKIPPED = "skipped"  # not a chat message
    EMPTY = "empty"  # no words heard
    STT_ERROR = "stt_error"  # speech recognition failed


class Reason(StrEnum):
    """Why something was refused. The texts for players are in the locales (access.<reason>)."""

    UNKNOWN = "unknown"  # no account for this token or Telegram user
    REVOKED = "revoked"  # the agent token was revoked
    NO_ACCESS = "no_access"  # the plan does not include this
    LIMIT = "limit"  # today's limit is used up
    RATE = "rate"  # too many messages in a minute
    UNAVAILABLE = "unavailable"  # the access point did not answer


@dataclass(frozen=True, slots=True)
class Refusal:
    reason: str
    link: str = ""  # where the player can fix it: the account page


@dataclass(frozen=True, slots=True)
class Player:
    user_id: int
    # A summary for the agent window (plan, messages left today...), passed through as is.
    account: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Check:
    allow: Allow
    check_id: str = ""
    reason: str = ""
    link: str = ""
    account: Mapping[str, Any] | None = None
    # The language the player speaks, set on their account (an ISO 639-1 code such as "de").
    # Speech recognition is locked to it; "" leaves it to the server's VF_STT_LANGUAGE.
    speech_language: str = ""

    @property
    def ok(self) -> bool:
        return self.allow is not Allow.NO


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What one Claude request used, from response.usage."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Usage:
    check_id: str
    user_id: int
    source: Source
    outcome: Outcome
    ai: bool = False  # Claude decided the channel and the text
    audio_s: float = 0.0
    tokens: TokenUsage | None = None

    def body(self) -> dict[str, Any]:
        tokens = self.tokens or TokenUsage("")
        return {
            "check_id": self.check_id,
            "user_id": self.user_id,
            "source": str(self.source),
            "outcome": str(self.outcome),
            "ai": self.ai,
            "audio_s": round(self.audio_s, 2),
            "model": tokens.model,
            "input_tokens": tokens.input_tokens,
            "output_tokens": tokens.output_tokens,
            "cache_read_tokens": tokens.cache_read_tokens,
            "cache_write_tokens": tokens.cache_write_tokens,
        }


@dataclass(frozen=True, slots=True)
class Offer:
    """A subscription the bot can sell for Telegram Stars."""

    title: str
    description: str
    stars: int
    payload: str  # the service's order id, comes back with the payment
    period_s: int = 30 * 24 * 3600  # Telegram renews Star subscriptions every 30 days


@dataclass(frozen=True, slots=True)
class StarPayment:
    """A successful_payment from Telegram, as the bot received it. Renewals of a Star
    subscription arrive the same way, with is_recurring set."""

    telegram_id: int  # who paid; the player id may be unknown if resolve failed just then
    payload: str
    currency: str
    total_amount: int
    telegram_payment_charge_id: str
    is_recurring: bool = False
    is_first_recurring: bool = False
    subscription_expiration_date: int = 0

    def body(self) -> dict[str, Any]:
        return {
            "telegram_id": self.telegram_id,
            "payload": self.payload,
            "currency": self.currency,
            "total_amount": self.total_amount,
            "telegram_payment_charge_id": self.telegram_payment_charge_id,
            "is_recurring": self.is_recurring,
            "is_first_recurring": self.is_first_recurring,
            "subscription_expiration_date": self.subscription_expiration_date,
        }


class Access(Protocol):
    # True if the bot may offer /buy: the access point takes payments.
    payments: bool

    async def resolve_agent(self, token: str) -> Player | Refusal: ...

    async def resolve_telegram(self, telegram_id: int, lang: str = "") -> Player | Refusal: ...

    async def check(
        self, user_id: int, source: Source, *, voice: bool, audio_s: float = 0.0
    ) -> Check: ...

    async def usage(self, report: Usage) -> Mapping[str, Any] | None: ...

    async def offer(self, user_id: int, lang: str = "") -> Offer | Refusal: ...

    async def pre_checkout(
        self, user_id: int, payload: str, currency: str, total_amount: int
    ) -> Refusal | None: ...

    async def paid(self, user_id: int, payment: StarPayment) -> Mapping[str, Any] | None:
        """user_id is 0 if the player could not be resolved; payload and telegram_id say whose."""
        ...

    async def close(self) -> None: ...


class LocalAccess:
    """VF_USERS: a fixed list of player ids and agent tokens, no limits, nothing to buy.

    The player id is the number before the token. With the Telegram bot it is the player's
    Telegram ID, so the bot and the agent meet under one id.
    """

    payments = False

    def __init__(self, users: Mapping[int, str]) -> None:
        self._tokens = tuple(users.items())  # (user id, token)
        self.users = frozenset(users)

    async def resolve_agent(self, token: str) -> Player | Refusal:
        # Compare against every token so timing does not reveal which one matched.
        found = None
        for user_id, expected in self._tokens:
            if hmac.compare_digest(token.encode(), expected.encode()):
                found = user_id
        return Player(found) if found is not None else Refusal(Reason.UNKNOWN)

    async def resolve_telegram(self, telegram_id: int, lang: str = "") -> Player | Refusal:
        return Player(telegram_id) if telegram_id in self.users else Refusal(Reason.UNKNOWN)

    async def check(
        self, user_id: int, source: Source, *, voice: bool, audio_s: float = 0.0
    ) -> Check:
        return Check(Allow.CLAUDE)

    async def usage(self, report: Usage) -> Mapping[str, Any] | None:
        if report.tokens is not None:
            t = report.tokens
            log.info(
                "usage user %s %s: %s in %d out %d cache %d/%d",
                report.user_id, report.outcome, t.model, t.input_tokens, t.output_tokens,
                t.cache_read_tokens, t.cache_write_tokens,
            )
        return None

    async def offer(self, user_id: int, lang: str = "") -> Offer | Refusal:
        return Refusal(Reason.NO_ACCESS)

    async def pre_checkout(
        self, user_id: int, payload: str, currency: str, total_amount: int
    ) -> Refusal | None:
        return Refusal(Reason.NO_ACCESS)

    async def paid(self, user_id: int, payment: StarPayment) -> Mapping[str, Any] | None:
        return None

    async def close(self) -> None:
        return None


class AccessError(Exception):
    pass


@dataclass(slots=True)
class _Cache:
    """Telegram user -> player for a short while, so every update does not cost a request."""

    ttl_s: float
    items: dict[int, tuple[float, Player]] = field(default_factory=dict)

    def get(self, key: int) -> Player | None:
        match self.items.get(key):
            case (at, player) if time.monotonic() - at < self.ttl_s:
                return player
            case _:
                return None

    def put(self, key: int, player: Player) -> None:
        if len(self.items) > 10_000:
            self.items.clear()
        self.items[key] = (time.monotonic(), player)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over "<timestamp>.<body>", hex, as sent in X-VF-Signature."""
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


class RemoteAccess:
    """The access API over HTTP. Every request is a signed JSON POST to <url>/<call>."""

    def __init__(
        self,
        url: str,
        secret: str,
        timeout_s: float = 2.0,
        payments: bool = True,
        telegram_cache_s: float = 60.0,
        retry_s: float = 1.0,
    ) -> None:
        if not secret:
            raise ValueError("VF_ACCESS_SECRET is required with VF_ACCESS_URL")
        self.url = url.rstrip("/")
        self.secret = secret
        self.timeout_s = timeout_s
        self.payments = payments
        self._session: aiohttp.ClientSession | None = None
        self._telegram = _Cache(telegram_cache_s)
        self.retry_s = retry_s  # first pause before a lost payment report is sent again

    async def _post(self, call: str, data: dict[str, Any]) -> dict[str, Any]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-VF-Timestamp": timestamp,
            "X-VF-Signature": sign(self.secret, timestamp, body),
        }
        try:
            async with asyncio.timeout(self.timeout_s):
                async with self._session.post(f"{self.url}/{call}", data=body, headers=headers) as r:
                    if r.status != 200:
                        raise AccessError(f"{call}: HTTP {r.status}")
                    answer = await r.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as e:
            raise AccessError(f"{call}: {e!r}") from e
        if not isinstance(answer, dict):
            raise AccessError(f"{call}: the answer is not a JSON object")
        return answer

    async def _resolve(self, data: dict[str, Any]) -> Player | Refusal:
        try:
            answer = await self._post("resolve", data)
        except AccessError as e:
            log.warning("access point unavailable: %s", e)
            return Refusal(Reason.UNAVAILABLE)
        match answer:
            case {"user_id": int(user_id)} if not isinstance(user_id, bool):
                return Player(user_id, _mapping(answer.get("account")))
            case _:
                return Refusal(str(answer.get("error") or Reason.UNKNOWN), str(answer.get("link", "")))

    async def resolve_agent(self, token: str) -> Player | Refusal:
        return await self._resolve({"agent_token": token})

    async def resolve_telegram(self, telegram_id: int, lang: str = "") -> Player | Refusal:
        if (player := self._telegram.get(telegram_id)) is not None:
            return player
        found = await self._resolve({"telegram_id": telegram_id, "lang": lang})
        if isinstance(found, Player):
            self._telegram.put(telegram_id, found)
        return found

    async def check(
        self, user_id: int, source: Source, *, voice: bool, audio_s: float = 0.0
    ) -> Check:
        data = {
            "user_id": user_id,
            "source": str(source),
            "kind": "voice" if voice else "text",
            "audio_s": round(audio_s, 2),
        }
        try:
            answer = await self._post("check", data)
        except AccessError as e:
            log.warning("access point unavailable: %s", e)
            return Check(Allow.NO, reason=Reason.UNAVAILABLE)
        try:
            allow = Allow(answer.get("allow", Allow.NO))
        except ValueError:
            allow = Allow.NO
        return Check(
            allow,
            check_id=str(answer.get("check_id", "")),
            reason=str(answer.get("reason", "") or ("" if allow is not Allow.NO else Reason.NO_ACCESS)),
            link=str(answer.get("link", "")),
            account=_mapping(answer.get("account")),
            speech_language=language_code(answer.get("speech_language")),
        )

    async def usage(self, report: Usage) -> Mapping[str, Any] | None:
        try:
            answer = await self._post("usage", report.body())
        except AccessError as e:
            log.warning("usage report lost: %s", e)
            return None
        return _mapping(answer.get("account"))

    async def offer(self, user_id: int, lang: str = "") -> Offer | Refusal:
        try:
            answer = await self._post("payment", {"step": "offer", "user_id": user_id, "lang": lang})
        except AccessError as e:
            log.warning("access point unavailable: %s", e)
            return Refusal(Reason.UNAVAILABLE)
        match answer:
            case {"title": str(title), "stars": int(stars), "payload": str(payload)} if stars > 0:
                return Offer(title, str(answer.get("description", "")), stars, payload)
            case _:
                return Refusal(str(answer.get("error") or Reason.NO_ACCESS), str(answer.get("link", "")))

    async def pre_checkout(
        self, user_id: int, payload: str, currency: str, total_amount: int
    ) -> Refusal | None:
        data = {
            "step": "pre_checkout",
            "user_id": user_id,
            "payload": payload,
            "currency": currency,
            "total_amount": total_amount,
        }
        try:
            answer = await self._post("payment", data)
        except AccessError as e:
            log.warning("access point unavailable: %s", e)
            return Refusal(Reason.UNAVAILABLE)
        return None if answer.get("ok") is True else Refusal(str(answer.get("error") or Reason.NO_ACCESS))

    async def paid(self, user_id: int, payment: StarPayment) -> Mapping[str, Any] | None:
        """Report a successful payment. Telegram has the money already, so a lost report is
        retried a few times and then left in the log for a manual fix."""
        data = {"step": "paid", "user_id": user_id, **payment.body()}
        for attempt in range(4):
            try:
                answer = await self._post("payment", data)
            except AccessError as e:
                log.warning("payment report failed (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(self.retry_s * 2**attempt)
                continue
            return _mapping(answer.get("account"))
        log.error("payment NOT recorded, fix by hand: user %s %s", user_id, json.dumps(data))
        return None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, dict) else None


_LANGUAGE_CODE = re.compile(r"[a-z]{2,3}")


def language_code(value: object) -> str:
    """A language code for speech recognition ("de", "RU " -> "ru"); "" for anything else."""
    code = value.strip().lower() if isinstance(value, str) else ""
    return code if _LANGUAGE_CODE.fullmatch(code) else ""
