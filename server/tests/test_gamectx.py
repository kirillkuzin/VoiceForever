"""Names and channels from the player's game: checking them, and using them to route and spell."""

import asyncio
import json
import time

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from vf_server.chat import Channel, ChatMessage
from vf_server.gamectx import RECENT_S, GameContext
from vf_server.hub import AgentHub
from vf_server.router import LLMRouter, RouteContext, rule_route
from vf_server.service import STT_MAX_NAMES, VoiceService
from vf_server.store import Store

from tests.test_hub_service import TOKEN, USER, FakeTranscriber, stop, wait_online
from tests.test_router import FakeClient, reply

NOW = 1_800_000_000.0

# What the agent sends (agent/vf_agent/context.py), copied from the game 4 s before.
SNAPSHOT = {
    "type": "context",
    "v": 1,
    "source": "live",
    "age_s": 4,
    "me": "Кирилл-Пламегор",
    "realm": "Пламегор",
    "channels": [
        {"n": 1, "name": "Общий - Оргриммар"},
        {"n": 2, "name": "Торговля - Город"},
        {"n": 5, "name": "Рейдеры"},
    ],
    "group": ["Грома-Гордунни", "Лиана"],
    "guild": [
        {"name": "Арторн", "online": True},
        {"name": "Спящий", "online": False},
        {"name": "Лиана", "online": True},
    ],
    "friends": [{"name": "Дру", "online": True}],
    "whispers": [{"name": "Варг", "dir": "in", "ago": 30}, {"name": "Старик", "dir": "in", "ago": RECENT_S + 10}],
    "speakers": [{"name": "Мила", "chan": "guild", "ago": 5}],
    "target": "",
}


def game(now: float = NOW, **changes) -> GameContext:
    return GameContext.parse(SNAPSHOT | changes, now=now)


def test_snapshot_is_checked():
    g = game()
    assert g.made_at == NOW - 4 and g.source == "live" and g.me == "Кирилл-Пламегор"
    assert [c.n for c in g.channels] == [1, 2, 5]
    for bad in ({"v": 2}, {"source": "telegram"}):
        with pytest.raises(ValueError):
            game(**bad)
    g = game(
        group=["ok", "|cffff0000Взлом|r", "x" * 65, 5],
        channels=[{"n": 0, "name": "Ноль"}, {"n": "3", "name": "Строка"}, {"n": 7, "name": "/run"}],
        guild=[{"name": f"Член{i}"} for i in range(400)],
    )
    assert g.group == ("ok",) and g.channels == () and len(g.guild) == 300


def test_names_likeliest_first():
    g = game(target="Цель")
    # The target, whispers of the last half hour, the group, speakers, then guild and friends online.
    assert g.names(NOW) == ["Цель", "Варг", "Грома-Гордунни", "Лиана", "Мила", "Арторн", "Дру", "Спящий"]
    assert g.last_whisperer(NOW) == "Варг"
    assert g.last_whisperer(NOW + RECENT_S) == ""


@pytest.mark.parametrize(
    "spoken, name",
    [
        ("арторн", "Арторн"),
        ("Арторну", "Арторн"),  # declined
        ("Арторна", "Арторн"),
        ("грома", "Грома-Гордунни"),  # without the realm
        ("Громе", "Грома-Гордунни"),
        ("Арторм", "Арторн"),  # misheard
        ("Незнакомец", ""),
        ("", ""),
    ],
)
def test_match_name(spoken, name):
    assert game().match_name(spoken, NOW) == name


@pytest.mark.parametrize(
    "spoken, number",
    [("Рейдеры", 5), ("рейдеры", 5), ("рейдерам", 5), ("торговля", 2), ("5", 5), ("7", None), ("гильдия", None)],
)
def test_channel_by_name_or_number(spoken, number):
    found = game().channel(spoken)
    assert (found.n if found else None) == number


@pytest.mark.parametrize(
    "phrase, channel, target, text",
    [
        ("в канал рейдеры сбор в восемь", "channel", "Рейдеры", "Сбор в восемь"),
        ("канал 5, кто на рейд", "channel", "5", "Кто на рейд"),
        ("шепни арторну я занят", "whisper", "Арторн", "Я занят"),
        ("ответь Варгу, сейчас буду", "whisper", "Варг", "Сейчас буду"),
        ("ответь ему сейчас буду", "reply", "", "Сейчас буду"),
        ("в общем я пошёл", "say", "", "В общем я пошёл"),  # "в общем" is not a channel
    ],
)
def test_rules_use_the_game(phrase, channel, target, text):
    r = rule_route(phrase, RouteContext(game=game()), now=NOW)
    assert (r.message.channel, r.message.target, r.message.text) == (channel, target, text)


def test_rules_without_the_game_keep_what_was_said():
    r = rule_route("в канал рейдеры сбор", RouteContext())
    assert (r.message.channel, r.message.target) == ("channel", "рейдеры")  # the addon finds it
    assert rule_route("ответь Варгу, сейчас", RouteContext()).message.channel == "reply"


async def test_claude_gets_the_game_and_its_answer_is_spelled_as_in_the_game():
    answers = iter([
        {"action": "send", "channel": "whisper", "target": "Арторну", "text": "Иду"},
        {"action": "send", "channel": "channel", "target": "рейдеры", "text": "Сбор"},
    ])
    client = FakeClient(lambda kw: reply(next(answers)))
    router = LLMRouter(client)
    ctx = RouteContext(game=game(now=time.time()))
    r = await router.route("скажи арторну иду", ctx)
    assert (r.message.channel, r.message.target) == ("whisper", "Арторн")
    r = await router.route("в рейдеры сбор", ctx)
    assert (r.message.channel, r.message.target) == ("channel", "Рейдеры")
    payload = json.loads(client.messages.calls[0]["messages"][0]["content"])
    assert payload["game"]["channels"][2] == {"number": 5, "name": "Рейдеры"}
    assert payload["game"]["recent_whispers"] == [{"name": "Варг", "from_player": False, "minutes_ago": 0}]
    assert payload["game"]["names"][0] == "Варг"
    assert "Рейдеры" in router.build_input("x", ctx)


def test_store_keeps_the_newest_in_memory():
    store = Store(":memory:")
    store.set_game(USER, game())
    store.set_game(USER, GameContext.parse(SNAPSHOT | {"age_s": 3600, "me": "Старый"}, now=NOW))
    assert store.game(USER).me == "Кирилл-Пламегор"  # an older snapshot came in late
    store.set_game(USER, None)
    assert store.game(USER) is None


def test_speech_hint_is_short():
    service = VoiceService(Store(":memory:"), AgentHub({}), FakeTranscriber(""), router=None, global_names=("Свой",))
    many = [{"name": f"Гильдиец{i}", "online": True} for i in range(100)]
    service.set_game_context(USER, SNAPSHOT | {"guild": many})
    names = service.stt_names(USER)
    assert names[:3] == ["Свой", "Варг", "Грома"]  # the player's own first, realms dropped
    assert len(names) <= STT_MAX_NAMES and len(", ".join(names)) <= 300
    service.set_game_context(USER, {"type": "context", "v": 9})  # unusable: the old ones stay
    assert service.store.game(USER) is not None
    service.set_game_context(USER, None)
    assert service.stt_names(USER) == ["Свой"]


async def agent_with_names(url, mode, seen):
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url, headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
            await ws.send_json({"type": "hello", "version": "0.4.0", "mode": mode})
            await ws.send_json(SNAPSHOT)
            async for frame in ws:
                data = json.loads(frame.data)
                seen.append(data)
                await ws.send_json({"type": "ack", "id": data["id"], "status": "ok"})


async def test_names_from_the_agent_reach_speech_recognition_and_delivery():
    hub = AgentHub({USER: TOKEN}, ack_timeout_s=2)
    transcriber = FakeTranscriber("в канал рейдеры сбор")
    service = VoiceService(Store(":memory:"), hub, transcriber, router=None)
    hub.context_handler = service.set_game_context
    seen = []
    async with TestServer(hub.app()) as server:
        task = asyncio.create_task(agent_with_names(server.make_url("/ws"), "addon", seen))
        await wait_online(hub)
        for _ in range(100):
            if service.store.game(USER) is not None:
                break
            await asyncio.sleep(0.01)
        text = await service.transcribe(USER, b"ogg", "voice.ogg")
        assert "Варг" in transcriber.prompts[0] and "Рейдеры" in transcriber.prompts[0]
        route = await service.route(USER, text)
        delivery = await service.deliver(USER, route.message)
        assert delivery.ack.ok and seen[0]["channel"] == "channel" and seen[0]["target"] == "Рейдеры"
        await stop(task)


async def test_plain_chat_mode_gets_the_channel_number():
    hub = AgentHub({USER: TOKEN}, ack_timeout_s=2)
    service = VoiceService(Store(":memory:"), hub, FakeTranscriber(""), router=None)
    hub.context_handler = service.set_game_context
    seen = []
    async with TestServer(hub.app()) as server:
        task = asyncio.create_task(agent_with_names(server.make_url("/ws"), "chat", seen))
        for _ in range(100):
            if hub.is_online(USER) and service.store.game(USER) is not None:
                break
            await asyncio.sleep(0.01)
        d = await service.deliver(USER, ChatMessage(Channel.CHANNEL, "Рейдеры", "Сбор"))
        assert d.ack.ok and seen[0]["target"] == "5"
        d = await service.deliver(USER, ChatMessage(Channel.CHANNEL, "Неизвестный", "Сбор"))
        assert d.ack.status == "error" and len(seen) == 1  # the chat box would take it for /say
        await stop(task)
