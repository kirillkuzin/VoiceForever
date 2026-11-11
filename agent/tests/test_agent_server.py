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

from vf_agent.client import Agent, AuthError, ConnState, Delivery, ReplacedError, Status  # noqa: E402
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


def make_agent(url, backend, token=TOKEN, focus_wait_s=0.3, on_event=None):
    cfg = AgentConfig(server_url=str(url).replace("http", "ws"), token=token, focus_wait_s=focus_wait_s)
    return Agent(cfg, backend, on_event)


def states(events):
    return [e.state for e in events if isinstance(e, Status)]



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


async def test_status_and_delivery_events():
    hub = AgentHub({1: TOKEN}, ack_timeout_s=3)
    events = []
    async with TestServer(hub.app()) as server:
        agent = make_agent(server.make_url("/ws"), FakeBackend(), on_event=events.append)
        task = asyncio.create_task(agent.run_forever())
        await wait_online(hub)
        assert states(events) == [ConnState.CONNECTING, ConnState.CONNECTED]
        assert agent.status.since is not None
        await hub.send(1, ChatMessage("guild", "", "Всем привет"), ["Всем привет"])
        deliveries = [e for e in events if isinstance(e, Delivery)]
        assert [(d.channel, d.text, d.status) for d in deliveries] == [("guild", "Всем привет", "ok")]
        await stop(task)
        assert states(events)[-1] is ConnState.DISCONNECTED


async def test_bad_token_reports_auth_failed():
    hub = AgentHub({1: TOKEN})
    events = []
    async with TestServer(hub.app()) as server:
        agent = make_agent(server.make_url("/ws"), FakeBackend(), token="wrong-token-0123456789", on_event=events.append)
        with pytest.raises(AuthError):
            await asyncio.wait_for(agent.run_forever(), timeout=5)
    assert states(events) == [ConnState.CONNECTING, ConnState.AUTH_FAILED]
    assert "401" in agent.status.detail


async def test_second_agent_replaces_the_first():
    hub = AgentHub({1: TOKEN})
    events = []
    async with TestServer(hub.app()) as server:
        first = make_agent(server.make_url("/ws"), FakeBackend(), on_event=events.append)
        first_task = asyncio.create_task(first.run_forever())
        await wait_online(hub)
        second_task = asyncio.create_task(make_agent(server.make_url("/ws"), FakeBackend()).run_forever())
        with pytest.raises(ReplacedError):
            await asyncio.wait_for(first_task, timeout=5)
        assert first.status.state is ConnState.REPLACED
        assert hub.is_online(1)  # the second agent stays connected and does not bounce back
        await stop(second_task)


async def test_unreachable_server_retries_with_reason():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    events = []
    agent = make_agent(f"ws://127.0.0.1:{port}/ws", FakeBackend(), on_event=events.append)
    task = asyncio.create_task(agent.run_forever())
    for _ in range(200):
        if agent.status.state is ConnState.RECONNECTING:
            break
        await asyncio.sleep(0.01)
    status = agent.status
    assert status.state is ConnState.RECONNECTING and status.attempt == 1
    assert status.detail and status.retry_at is not None
    await stop(task)
    assert agent.status.state is ConnState.DISCONNECTED
