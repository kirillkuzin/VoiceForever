"""Agent <-> real server hub over a local WebSocket, with a fake keyboard."""

import asyncio
import contextlib
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from vf_server.chat import ChatMessage  # noqa: E402
from vf_server.hub import AgentHub  # noqa: E402

from vf_agent.client import Agent, AuthError  # noqa: E402
from vf_agent.config import AgentConfig  # noqa: E402

TOKEN = "agent-token-0123456789abcdef"


class FakeBackend:
    def __init__(self, focused=True):
        self.focused = focused
        self.runs = []

    def is_game_focused(self):
        return self.focused

    def run(self, actions):
        self.runs.append(actions)


def make_agent(url, backend, token=TOKEN, focus_wait_s=0.3):
    cfg = AgentConfig(server_url=str(url).replace("http", "ws"), token=token, focus_wait_s=focus_wait_s)
    return Agent(cfg, backend)



async def stop(task):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def wait_online(hub, user=1):
    for _ in range(200):
        if hub.is_online(user):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("agent did not connect")


async def test_message_is_typed_and_acked():
    hub = AgentHub({1: TOKEN}, ack_timeout_s=3)
    backend = FakeBackend()
    async with TestServer(hub.app()) as server:
        task = asyncio.create_task(make_agent(server.make_url("/ws"), backend).run_forever())
        await wait_online(hub)
        ack = await hub.send(1, ChatMessage("whisper", "Арторн", "Иду"), ["Иду"])
        assert ack.ok
        pastes = [a.value for a in backend.runs[0] if a.kind == "paste"]
        assert pastes == ["VF:whisper:Арторн:Иду"]
        assert hub.info(1).mode == "addon"
        await stop(task)


async def test_not_focused_is_reported():
    hub = AgentHub({1: TOKEN}, ack_timeout_s=3)
    backend = FakeBackend(focused=False)
    async with TestServer(hub.app()) as server:
        task = asyncio.create_task(make_agent(server.make_url("/ws"), backend).run_forever())
        await wait_online(hub)
        ack = await hub.send(1, ChatMessage("say", "", "hi"), ["hi"])
        assert ack.status == "not_focused" and backend.runs == []
        await stop(task)


async def test_bad_token_stops_agent():
    hub = AgentHub({1: TOKEN})
    async with TestServer(hub.app()) as server:
        agent = make_agent(server.make_url("/ws"), FakeBackend(), token="wrong-token-0123456789")
        with pytest.raises(AuthError):
            await asyncio.wait_for(agent.run_forever(), timeout=5)
