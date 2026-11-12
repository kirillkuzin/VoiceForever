"""Access points: who may use the server, what each message may do, what it cost, and the
Telegram Stars subscription the bot passes on to an access service."""

import asyncio
import json
import time
from types import SimpleNamespace

import aiohttp
import pytest
from aiogram import Bot, Dispatcher
from aiohttp import web
from aiohttp.test_utils import TestServer

from vf_server.access import (
    Allow,
    Check,
    LocalAccess,
    Offer,
    Outcome,
    Player,
    Reason,
    Refusal,
    RemoteAccess,
    Source,
    StarPayment,
    TokenUsage,
    Usage,
    sign,
)
from vf_server.bot import set_commands, setup_bot
from vf_server.chat import Channel, ChatMessage
from vf_server.config import Settings
from vf_server.hub import AgentHub
from vf_server.i18n import t
from vf_server.router import Decision, RouteResult
from vf_server.service import RateLimit, VoiceService
from vf_server.store import Store
from tests.test_i18n import RecordingSession
from tests.test_voice import FakeTranscriber, frame, wav
from tests.test_widgets import FakeHub

TOKEN = "test-token-0123456789abcdef"
USER = 42  # the player's account id at the access service
TG = 777  # their Telegram ID
LINK = "https://vf.example/account"
SECRET = "test-secret"


class FakeAccess:
    """An access service in memory: one player with an agent token and a Telegram account."""

    payments = True

    def __init__(self, allow=Allow.CLAUDE, reason=""):
        self.allow, self.reason = allow, reason
        self.checks, self.usages, self.paid_calls = [], [], []

    async def resolve_agent(self, token):
        match token:
            case "revoked-token-0123456789":
                return Refusal(Reason.REVOKED, LINK)
            case "down-token-0123456789":
                return Refusal(Reason.UNAVAILABLE)
            case _ if token == TOKEN:
                return Player(USER, {"plan": "full", "ai_left": 99})
        return Refusal(Reason.UNKNOWN, LINK)

    async def resolve_telegram(self, telegram_id, lang=""):
        return Player(USER) if telegram_id == TG else Refusal(Reason.UNKNOWN, LINK)

    async def check(self, user_id, source, *, voice, audio_s=0.0):
        self.checks.append((user_id, str(source), voice, audio_s))
        link = LINK if self.allow is Allow.NO else ""
        return Check(self.allow, check_id=f"c{len(self.checks)}", reason=self.reason, link=link)

    async def usage(self, report):
        self.usages.append(report)
        return {"plan": "full", "ai_left": 98}

    async def offer(self, user_id, lang=""):
        return Offer("VoiceForever Full", "100 AI messages a day", 330, "order-1")

    async def pre_checkout(self, user_id, payload, currency, total_amount):
        return None if payload == "order-1" and total_amount == 330 else Refusal(Reason.NO_ACCESS)

    async def paid(self, user_id, payment):
        self.paid_calls.append((user_id, payment))
        return {"plan": "full", "until": payment.subscription_expiration_date}

    async def close(self):
        pass


class Claude:
    """A router that is Claude: always routes to the guild and reports what it used."""

    allow_transform = True

    def __init__(self):
        self.calls = 0

    async def route(self, transcript, ctx):
        self.calls += 1
        message = ChatMessage(Channel.GUILD, "", transcript.capitalize())
        return RouteResult(Decision.SEND, message, "llm", usage=TokenUsage("claude-haiku-5-5", 900, 40, 800))


# ---------------------------------------------------------------------- local


async def test_local_access_is_the_user_list_without_limits():
    access = LocalAccess({USER: TOKEN})
    assert await access.resolve_agent(TOKEN) == Player(USER)
    assert await access.resolve_agent("x" * 26) == Refusal(Reason.UNKNOWN)
    assert await access.resolve_telegram(USER) == Player(USER)
    assert await access.resolve_telegram(TG) == Refusal(Reason.UNKNOWN)
    check = await access.check(USER, Source.AGENT, voice=True, audio_s=3)
    assert check.allow is Allow.CLAUDE and check.ok
    assert not access.payments and isinstance(await access.offer(USER), Refusal)


def test_rate_limit_is_a_sliding_minute_per_player():
    rate = RateLimit(2)
    assert rate.allow(1, now=0) and rate.allow(1, now=10)
    assert not rate.allow(1, now=20)
    assert rate.allow(2, now=20)
    assert rate.allow(1, now=60.5)  # the first one left the window
    assert all(RateLimit(0).allow(1, now=0) for _ in range(100))


def test_settings_take_an_access_service_instead_of_the_user_list():
    base = {"VF_ACCESS_URL": "http://cloud:8080/access", "VF_ACCESS_SECRET": SECRET}
    settings = Settings.from_env(base)
    assert settings.access_url == "http://cloud:8080/access" and not settings.users
    assert settings.telegram_token == ""  # agents only
    with pytest.raises(ValueError, match="VF_ACCESS_SECRET"):
        Settings.from_env({"VF_ACCESS_URL": "http://cloud"})
    with pytest.raises(ValueError, match="not both"):
        Settings.from_env(base | {"VF_USERS": f"1:{TOKEN}"})
    with pytest.raises(ValueError, match="VF_USERS"):
        Settings.from_env({"VF_TELEGRAM_TOKEN": "1:x"})


# ---------------------------------------------------------------------- remote


class AccessServer:
    """The HTTP side of an access service, checking every signature."""

    def __init__(self):
        self.answers: dict = {}
        self.seen: list = []
        self.delay = 0.0
        self.status = 200

    def app(self):
        app = web.Application()
        app.router.add_post("/access/{call}", self.handle)
        return app

    async def handle(self, request):
        body = await request.read()
        timestamp = request.headers["X-VF-Timestamp"]
        assert request.headers["X-VF-Signature"] == sign(SECRET, timestamp, body)
        assert abs(int(timestamp) - time.time()) < 5
        call, data = request.match_info["call"], json.loads(body)
        self.seen.append((call, data))
        await asyncio.sleep(self.delay)
        status = self.status if isinstance(self.status, int) else self.status.pop(0)
        if status != 200:
            return web.Response(status=status)
        answer = self.answers[call]
        return web.json_response(answer(data) if callable(answer) else answer)


@pytest.fixture
async def remote():
    server = AccessServer()
    async with TestServer(server.app()) as http:
        access = RemoteAccess(str(http.make_url("/access")), SECRET, timeout_s=0.5, retry_s=0)
        yield server, access
        await access.close()


def resolve_answer(data):
    if data.get("agent_token") == TOKEN or data.get("telegram_id") == TG:
        return {"user_id": USER, "account": {"plan": "free", "trial_left": 20}}
    return {"error": "unknown", "link": LINK}


async def test_remote_resolve_is_signed_and_telegram_is_cached(remote):
    server, access = remote
    server.answers["resolve"] = resolve_answer
    assert await access.resolve_agent(TOKEN) == Player(USER, {"plan": "free", "trial_left": 20})
    assert await access.resolve_agent("wrong-token-0123456789") == Refusal("unknown", LINK)
    assert await access.resolve_telegram(TG, "de") == Player(USER, {"plan": "free", "trial_left": 20})
    assert await access.resolve_telegram(TG) == Player(USER, {"plan": "free", "trial_left": 20})
    assert await access.resolve_telegram(5) == Refusal("unknown", LINK)
    assert server.seen == [
        ("resolve", {"agent_token": TOKEN}),
        ("resolve", {"agent_token": "wrong-token-0123456789"}),
        ("resolve", {"telegram_id": TG, "lang": "de"}),  # the second one came from the cache
        ("resolve", {"telegram_id": 5, "lang": ""}),
    ]


async def test_remote_check_and_usage(remote):
    server, access = remote
    server.answers["check"] = {"allow": "transcribe", "check_id": "c9", "reason": "limit",
                               "account": {"ai_left": 0}}
    server.answers["usage"] = {"account": {"ai_left": 0, "audio_s_today": 61}}
    check = await access.check(USER, Source.TELEGRAM, voice=True, audio_s=3.456)
    assert check == Check(Allow.TRANSCRIBE, "c9", "limit", "", {"ai_left": 0})
    assert server.seen[-1] == ("check", {"user_id": USER, "source": "telegram", "kind": "voice", "audio_s": 3.46})

    report = Usage("c9", USER, Source.AGENT, Outcome.DELIVERED, ai=True, audio_s=2.5,
                   tokens=TokenUsage("claude-haiku-5-5", 900, 40, 800, 0))
    assert await access.usage(report) == {"ai_left": 0, "audio_s_today": 61}
    assert server.seen[-1] == ("usage", {
        "check_id": "c9", "user_id": USER, "source": "agent", "outcome": "delivered", "ai": True,
        "audio_s": 2.5, "model": "claude-haiku-5-5", "input_tokens": 900, "output_tokens": 40,
        "cache_read_tokens": 800, "cache_write_tokens": 0,
    })

    server.answers["check"] = {"allow": "no"}
    assert await access.check(USER, Source.AGENT, voice=False) == Check(Allow.NO, reason=Reason.NO_ACCESS)
    server.answers["check"] = {"allow": "everything"}
    assert (await access.check(USER, Source.AGENT, voice=False)).allow is Allow.NO


@pytest.mark.parametrize("trouble", ["slow", "500", "not an object"])
async def test_remote_fails_closed(remote, trouble):
    server, access = remote
    server.answers = {"resolve": resolve_answer, "check": ["allow", "claude"], "usage": {}}
    match trouble:
        case "slow":
            server.delay = 1.0
        case "500":
            server.status = 500
    if trouble == "not an object":
        server.answers["resolve"] = [USER]
    assert await access.resolve_agent(TOKEN) == Refusal(Reason.UNAVAILABLE)
    assert await access.check(USER, Source.AGENT, voice=True) == Check(Allow.NO, reason=Reason.UNAVAILABLE)
    assert await access.usage(Usage("c", USER, Source.AGENT, Outcome.EMPTY)) is None


async def test_remote_payment_steps(remote):
    server, access = remote
    server.answers["payment"] = lambda data: {
        "offer": {"title": "VoiceForever Full", "description": "100 AI", "stars": 330, "payload": "o-1"},
        "pre_checkout": {"ok": data.get("payload") == "o-1"},
        "paid": {"account": {"plan": "full"}},
    }[data["step"]]
    assert await access.offer(USER, "ru") == Offer("VoiceForever Full", "100 AI", 330, "o-1")
    assert await access.pre_checkout(USER, "o-1", "XTR", 330) is None
    assert await access.pre_checkout(USER, "o-2", "XTR", 330) == Refusal(Reason.NO_ACCESS)

    payment = StarPayment(TG, "o-1", "XTR", 330, "charge-1", True, True, 1_800_000_000)
    server.status = [503, 500, 200]  # Telegram has the money: the report is sent again
    assert await access.paid(USER, payment) == {"plan": "full"}
    paid = [data for call, data in server.seen if data.get("step") == "paid"]
    assert len(paid) == 3 and paid[-1] == {
        "step": "paid", "user_id": USER, "telegram_id": TG, "payload": "o-1", "currency": "XTR",
        "total_amount": 330, "telegram_payment_charge_id": "charge-1", "is_recurring": True,
        "is_first_recurring": True, "subscription_expiration_date": 1_800_000_000,
    }


# ---------------------------------------------------------------------- agent


async def connect(hub, service, token=TOKEN, uploads=(), until=lambda got: False):
    """A fake agent: sends recordings, acks typing, collects everything until `until`."""
    hub.voice_handler = service.handle_agent_voice
    got = []
    async with TestServer(hub.app()) as server, aiohttp.ClientSession() as s:
        async with asyncio.timeout(5):
            async with s.ws_connect(server.make_url("/ws"), headers={"Authorization": f"Bearer {token}"}) as ws:
                await ws.send_json({"type": "hello", "version": "0.4.0", "mode": "addon"})
                for data in uploads:
                    await ws.send_bytes(data)
                async for msg in ws:
                    data = json.loads(msg.data)
                    got.append(data)
                    if data["type"] == "send":
                        await ws.send_json({"type": "ack", "id": data["id"], "status": "ok"})
                    if until(got):
                        break
                return got, ws.close_code


def make_service(access, transcriber=None, router=None, rate_per_min=20):
    hub = AgentHub(access, ack_timeout_s=2)
    stt = transcriber or FakeTranscriber("всем привет")
    return hub, VoiceService(Store(":memory:"), hub, stt, router=router, rate_per_min=rate_per_min), stt


def results(got):
    return [m for m in got if m["type"] == "voice_result"]


@pytest.mark.parametrize("token, status, error", [
    ("wrong-token-0123456789", 401, "unknown"),
    ("revoked-token-0123456789", 401, "revoked"),
    ("down-token-0123456789", 503, "unavailable"),
])
async def test_refused_agent_token_gets_the_reason(token, status, error):
    hub, _, _ = make_service(FakeAccess())
    async with TestServer(hub.app()) as server, aiohttp.ClientSession() as s:
        async with s.get(server.make_url("/ws"), headers={"Authorization": f"Bearer {token}"}) as r:
            assert r.status == status
            assert (await r.json())["error"] == error


async def test_agent_gets_its_account_and_claude_usage_is_reported():
    access, claude = FakeAccess(), Claude()
    hub, service, _ = make_service(access, router=claude)
    got, _ = await connect(hub, service, uploads=[frame("v1", wav(1.5))],
                           until=lambda got: results(got) and got[-1].get("ai_left") == 98)
    assert got[0] == {"type": "account", "plan": "full", "ai_left": 99}
    [result] = results(got)
    assert result["status"] == "ok" and result["ai"] is True and result["channel"] == "guild"
    await service.drain()
    assert access.checks == [(USER, "agent", True, 1.5)]
    [usage] = access.usages
    assert (usage.check_id, usage.outcome, usage.ai, usage.audio_s) == ("c1", Outcome.DELIVERED, True, 1.5)
    assert usage.tokens == TokenUsage("claude-haiku-5-5", 900, 40, 800)


async def test_denied_recording_is_not_transcribed():
    access = FakeAccess(Allow.NO, Reason.LIMIT)
    hub, service, stt = make_service(access)
    got, _ = await connect(hub, service, uploads=[frame("v1", wav(1))], until=results)
    [result] = results(got)
    assert (result["status"], result["reason"], result["link"]) == ("denied", "limit", LINK)
    assert stt.files == [] and access.usages == []


async def test_revoked_token_ends_the_connection():
    hub, service, _ = make_service(FakeAccess(Allow.NO, Reason.REVOKED))
    got, code = await connect(hub, service, uploads=[frame("v1", wav(1))])
    assert results(got)[0]["reason"] == "revoked" and "disconnect" not in results(got)[0]
    assert code == 4001


async def test_after_the_daily_limit_the_rules_route_without_claude():
    access, claude = FakeAccess(Allow.TRANSCRIBE, Reason.LIMIT), Claude()
    hub, service, _ = make_service(access, FakeTranscriber("в гильдию всем привет"), router=claude)
    got, _ = await connect(hub, service, uploads=[frame("v1", wav(1))], until=results)
    [result] = results(got)
    assert result["status"] == "ok" and result["channel"] == "guild"
    assert (result["ai"], result["note"]) == (False, "ai_limit")
    assert claude.calls == 0
    await service.drain()
    assert access.usages[0].ai is False and access.usages[0].tokens is None


async def test_too_many_recordings_in_a_minute_are_refused():
    access = FakeAccess()
    hub, service, stt = make_service(access, rate_per_min=1)
    got, _ = await connect(hub, service, uploads=[frame("v1", wav(1)), frame("v2", wav(1))],
                           until=lambda got: len(results(got)) == 2)
    assert sorted(r["status"] for r in results(got)) == ["denied", "ok"]
    assert [r["reason"] for r in results(got) if r["status"] == "denied"] == ["rate"]
    assert len(stt.files) == 1 and len(access.checks) == 1


# ---------------------------------------------------------------------- bot


class PaymentSession(RecordingSession):
    async def make_request(self, bot, method, timeout=None):
        await super().make_request(bot, method, timeout)
        match method.__api_method__:
            case "createInvoiceLink":
                return "https://t.me/$invoice-1"
            case "sendWidget":
                return SimpleNamespace(message_id=500)
        return True


@pytest.fixture
def paid_bot():
    access = FakeAccess()
    hub = FakeHub(access)
    service = VoiceService(Store(":memory:"), hub, FakeTranscriber("всем привет"), router=None)
    dp = Dispatcher()
    setup_bot(dp, service, terms_url="https://vf.example/terms", pay_support="@vf_support")
    session = PaymentSession()
    bot = Bot("123456:" + "A" * 35, session=session)
    updates = iter(range(1, 1000))

    def user(uid):
        return {"id": uid, "is_bot": False, "first_name": "K", "language_code": "en"}

    async def feed(kind, payload):
        before = len(session.requests)
        await dp.feed_raw_update(bot, {"update_id": next(updates), kind: payload})
        await service.drain()
        return session.requests[before:]

    async def message(uid=TG, text=None, **extra):
        payload = {"message_id": 1, "date": 0, "chat": {"id": uid, "type": "private"}, "from": user(uid)}
        if text is not None:
            payload["text"] = text
            if text.startswith("/"):
                payload["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        return await feed("message", payload | extra)

    return access, hub, service, message, feed, user


def only(requests, method):
    [found] = [data for name, data in requests if name == method]
    return found


async def test_strangers_get_a_link_to_the_account_page(paid_bot):
    _, _, _, message, _, _ = paid_bot
    for text in ("/start", "guild hi"):
        reply = only(await message(uid=5, text=text), "sendMessage")
        assert reply["text"] == t("en", "access.unknown")
        [[button]] = reply["reply_markup"]["inline_keyboard"]
        assert button["url"] == LINK


async def test_telegram_message_is_checked_typed_and_reported(paid_bot):
    access, hub, service, message, _, _ = paid_bot
    service.store.update_ui(USER, widgets=False)
    await message(text="в гильдию всем привет")
    assert access.checks == [(USER, "telegram", False, 0.0)]
    assert hub.sent == [ChatMessage(Channel.GUILD, "", "Всем привет")]
    [usage] = access.usages
    assert (usage.user_id, usage.source, usage.outcome) == (USER, Source.TELEGRAM, Outcome.DELIVERED)
    assert hub.accounts[-1] == (USER, {"plan": "full", "ai_left": 98})


async def test_telegram_refusals(paid_bot):
    access, hub, _, message, _, _ = paid_bot
    hub.online = False  # no agent: nothing is checked or recognised
    assert only(await message(text="привет"), "sendMessage")["text"] == t("en", "ack.offline")
    assert access.checks == []

    hub.online, access.allow, access.reason = True, Allow.NO, Reason.NO_ACCESS
    reply = only(await message(text="привет"), "sendMessage")
    assert reply["text"] == t("en", "access.no_access")
    assert reply["reply_markup"]["inline_keyboard"][0][0]["url"] == LINK
    assert hub.sent == [] and access.usages == []


async def test_panel_goes_to_the_players_telegram_chat(paid_bot):
    _, _, service, message, _, _ = paid_bot
    [widget] = [d for name, d in await message(text="/panel") if name == "sendWidget"]
    assert widget["chat_id"] == TG
    assert service.store.telegram_chat(USER) == TG


async def test_stars_subscription(paid_bot):
    access, hub, _, message, feed, user = paid_bot
    requests = await message(text="/buy")
    invoice = only(requests, "createInvoiceLink")
    assert (invoice["currency"], invoice["payload"], invoice["subscription_period"]) == ("XTR", "order-1", 2592000)
    assert invoice["prices"] == [{"label": "VoiceForever Full", "amount": 330}]
    offer = only(requests, "sendMessage")
    assert offer["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://t.me/$invoice-1"

    async def pre_checkout(payload, uid=TG):
        query = {"id": "p1", "from": user(uid), "currency": "XTR", "total_amount": 330, "invoice_payload": payload}
        return only(await feed("pre_checkout_query", query), "answerPreCheckoutQuery")

    assert (await pre_checkout("order-1"))["ok"] is True
    refused = await pre_checkout("order-2")
    assert refused["ok"] is False and refused["error_message"] == t("en", "access.no_access")
    assert (await pre_checkout("order-1", uid=5))["ok"] is False

    paid = {"currency": "XTR", "total_amount": 330, "invoice_payload": "order-1",
            "telegram_payment_charge_id": "charge-1", "provider_payment_charge_id": "",
            "subscription_expiration_date": 1_800_000_000, "is_recurring": True, "is_first_recurring": True}
    reply = only(await message(successful_payment=paid), "sendMessage")
    assert reply["text"] == t("en", "bot.paid_until", date="2027-01-15")
    assert access.paid_calls == [(USER, StarPayment(TG, "order-1", "XTR", 330, "charge-1", True, True, 1_800_000_000))]
    assert hub.accounts[-1] == (USER, {"plan": "full", "until": 1_800_000_000})

    assert only(await message(text="/terms"), "sendMessage")["text"] == t("en", "bot.terms", url="https://vf.example/terms")
    assert "@vf_support" in only(await message(text="/paysupport"), "sendMessage")["text"]


async def test_self_hosted_bot_sells_nothing():
    service = VoiceService(Store(":memory:"), FakeHub(), transcriber=None, router=None)
    dp = Dispatcher()
    setup_bot(dp, service)
    session = RecordingSession()
    bot = Bot("123456:" + "A" * 35, session=session)
    await dp.feed_raw_update(bot, {"update_id": 1, "message": {
        "message_id": 1, "date": 0, "chat": {"id": 42, "type": "private"},
        "from": {"id": 42, "is_bot": False, "first_name": "K"}, "text": "/buy",
        "entities": [{"type": "bot_command", "offset": 0, "length": 4}],
    }})
    assert session.requests == []

    await set_commands(bot, payments=True)
    menus = [data["commands"] for name, data in session.requests if name == "setMyCommands"]
    assert [c["command"] for c in menus[0]][-3:] == ["buy", "terms", "paysupport"]
