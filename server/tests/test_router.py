import json
import time
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from vf_server.chat import Channel
from vf_server.router import Decision, LLMRouter, RouteContext, SentRecord, rule_route


@pytest.mark.parametrize(
    "phrase, channel, target, text",
    [
        ("в пати иду на босса", "group", "", "Иду на босса"),
        ("В гильдию, всем привет", "guild", "", "Всем привет"),
        ("шепни Арторну что я занят", "whisper", "Арторну", "Я занят"),
        ("ответь ему сейчас буду", "reply", "", "Сейчас буду"),
        ("крикни помогите", "yell", "", "Помогите"),
        ("в трейд продам руду", "channel", "2", "Продам руду"),
        ("просто текст", "say", "", "Просто текст"),
    ],
)
def test_rule_route_prefixes(phrase, channel, target, text):
    r = rule_route(phrase, RouteContext())
    assert (r.message.channel, r.message.target, r.message.text) == (channel, target, text)
    assert r.source == "rules"


def test_rule_route_continues_recent_channel():
    now = time.time()
    ctx = RouteContext(recent=[SentRecord(now - 60, Channel.GUILD, "", "привет")])
    assert rule_route("как дела", ctx, now).message.channel == "guild"
    old = RouteContext(recent=[SentRecord(now - 600, Channel.GUILD, "", "привет")])
    assert rule_route("как дела", old, now).message.channel == "say"


class FakeMessages:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.behaviour(kwargs)


class FakeClient:
    def __init__(self, behaviour):
        self.messages = FakeMessages(behaviour)
        self.beta = SimpleNamespace(messages=self.messages)


def reply(data, stop="end_turn"):
    return SimpleNamespace(
        stop_reason=stop,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(data, ensure_ascii=False))],
    )


async def test_llm_route_uses_structured_output_and_fallbacks():
    client = FakeClient(lambda kw: reply({"action": "send", "channel": "group", "target": "", "text": "Отойду на 5 минут"}))
    router = LLMRouter(client, model="claude-opus-5-5")
    r = await router.route("скажи в пати что я отойду на пять минут", RouteContext())
    assert r.source == "llm" and r.message.channel == "group" and r.message.text == "Отойду на 5 минут"
    call = client.messages.calls[0]
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "low"
    payload = json.loads(call["messages"][0]["content"])
    assert payload["phrase"].startswith("скажи в пати")


async def test_llm_route_no_fallbacks_for_haiku():
    client = FakeClient(lambda kw: reply({"action": "send", "channel": "say", "target": "", "text": "Привет"}))
    router = LLMRouter(client, model="claude-haiku-5-5")
    await router.route("привет", RouteContext())
    assert "fallbacks" not in client.messages.calls[0]


async def test_llm_skip():
    client = FakeClient(lambda kw: reply({"action": "skip", "channel": "say", "target": "", "text": ""}))
    r = await LLMRouter(client).route("кхм кхм", RouteContext())
    assert r.decision is Decision.SKIP


async def test_llm_sanitises_output():
    client = FakeClient(lambda kw: reply({"action": "send", "channel": "say", "target": "", "text": "/run X()|r"}))
    r = await LLMRouter(client).route("x", RouteContext())
    assert r.message.text == "run X()/r"


async def test_llm_refusal_falls_back_to_rules():
    client = FakeClient(lambda kw: reply({}, stop="refusal"))
    r = await LLMRouter(client).route("в гильдию привет", RouteContext())
    assert r.source == "rules" and r.message.channel == "guild" and r.note


async def test_llm_connection_error_falls_back_to_rules():
    def boom(kw):
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))

    r = await LLMRouter(FakeClient(boom)).route("в пати го", RouteContext())
    assert r.source == "rules" and r.message.channel == "group"


async def test_transform_disabled_hides_style():
    client = FakeClient(lambda kw: reply({"action": "send", "channel": "say", "target": "", "text": "Привет"}))
    router = LLMRouter(client, allow_transform=False)
    await router.route("привет", RouteContext(rewrite_style="как орк"))
    payload = json.loads(client.messages.calls[0]["messages"][0]["content"])
    assert payload["rewrite_style"] == ""
