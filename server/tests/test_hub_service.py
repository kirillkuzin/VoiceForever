import asyncio
import contextlib
import json

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from vf_server.chat import ChatMessage
from vf_server.hub import AgentHub
from vf_server.service import VoiceService
from vf_server.store import Store

TOKEN = "test-token-0123456789abcdef"
USER = 42


class FakeTranscriber:
    def __init__(self, text):
        self.text = text
        self.prompts = []

    async def transcribe(self, audio, filename, extra_prompt="", lang="en", language=""):
        self.prompts.append(extra_prompt)
        return self.text


async def fake_agent(url, status="ok", seen=None):
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url, headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
            await ws.send_json({"type": "hello", "version": "0.1.0", "mode": "addon"})
            async for frame in ws:
                data = json.loads(frame.data)
                if seen is not None:
                    seen.append(data)
                await ws.send_json({"type": "ack", "id": data["id"], "status": status})



async def stop(task):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def wait_online(hub):
    for _ in range(100):
        if hub.is_online(USER) and hub.info(USER).mode == "addon":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("agent did not connect")


async def test_bad_token_rejected():
    hub = AgentHub({USER: TOKEN})
    async with TestServer(hub.app()) as server:
        async with aiohttp.ClientSession() as s:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await s.ws_connect(server.make_url("/ws"), headers={"Authorization": "Bearer nope"})


async def test_end_to_end_delivery_and_history():
    hub = AgentHub({USER: TOKEN}, ack_timeout_s=2)
    store = Store(":memory:")
    store.update(USER, known_names="Арторн")
    service = VoiceService(store, hub, FakeTranscriber("в гильдию всем привет"), router=None)
    seen = []
    async with TestServer(hub.app()) as server:
        task = asyncio.create_task(fake_agent(server.make_url("/ws"), seen=seen))
        await wait_online(hub)

        text = await service.transcribe(USER, b"ogg", "voice.ogg")
        assert "Арторн" in service.transcriber.prompts[0]
        route = await service.route(USER, text)
        delivery = await service.deliver(USER, route.message)

        assert delivery.ack.ok
        assert seen[0]["channel"] == "guild" and seen[0]["parts"] == ["Всем привет"]
        assert store.recent(USER)[-1].channel == "guild"
        await stop(task)


async def test_offline_and_not_focused():
    hub = AgentHub({USER: TOKEN}, ack_timeout_s=2)
    store = Store(":memory:")
    service = VoiceService(store, hub, FakeTranscriber(""), router=None)
    d = await service.deliver(USER, ChatMessage("say", "", "hi"))
    assert d.ack.status == "offline"

    async with TestServer(hub.app()) as server:
        task = asyncio.create_task(fake_agent(server.make_url("/ws"), status="not_focused"))
        await wait_online(hub)
        d = await service.deliver(USER, ChatMessage("say", "", "hi"))
        assert d.ack.status == "not_focused"
        assert store.recent(USER) == []
        await stop(task)


async def test_long_message_is_split():
    hub = AgentHub({USER: TOKEN}, ack_timeout_s=2)
    service = VoiceService(Store(":memory:"), hub, FakeTranscriber(""), router=None, max_message_bytes=50)
    seen = []
    async with TestServer(hub.app()) as server:
        task = asyncio.create_task(fake_agent(server.make_url("/ws"), seen=seen))
        await wait_online(hub)
        d = await service.deliver(USER, ChatMessage("group", "", "очень длинное сообщение " * 5))
        assert d.ack.ok and len(seen[0]["parts"]) > 1
        assert all(len(p.encode()) <= 50 for p in seen[0]["parts"])
        await stop(task)


def test_store_settings_roundtrip(tmp_path):
    store = Store(str(tmp_path / "vf.db"))
    store.update(USER, default_channel="guild", confirm=True, rewrite_style="как орк")
    again = Store(str(tmp_path / "vf.db")).get(USER)
    assert (again.default_channel, again.confirm, again.rewrite_style) == ("guild", True, "как орк")
