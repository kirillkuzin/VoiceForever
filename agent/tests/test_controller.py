"""Controller (agent on its own loop thread) and the window API, against the real server hub."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from vf_server.chat import ChatMessage  # noqa: E402
from vf_server.hub import AgentHub  # noqa: E402

from vf_agent import controller as controller_module  # noqa: E402
from vf_agent import i18n  # noqa: E402
from vf_agent.client import ConnState, Delivery, Game, Names, PadState, RecState, Recording, Status  # noqa: E402
from vf_agent.config import AgentConfig, defaults  # noqa: E402
from vf_agent.controller import Controller  # noqa: E402
from vf_agent.gui.api import Api, event_json  # noqa: E402
from vf_agent.plan import GameState  # noqa: E402

TOKEN = "agent-token-0123456789abcdef"


class FakeBackend:
    def __init__(self, game=GameState.ACTIVE):
        self.game = game
        self.runs = []

    def is_game_focused(self):
        return self.game is GameState.ACTIVE

    def game_state(self):
        return self.game

    def run(self, actions):
        self.runs.append(actions)


def config_for(server, **values):
    url = str(server.make_url("/ws")).replace("http", "ws")
    return AgentConfig(server_url=url, token=TOKEN, focus_wait_s=1, **values)


async def until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.fixture
async def ctrl(monkeypatch):
    monkeypatch.setattr(controller_module, "GAME_POLL_S", 0.05)
    backends = []

    def make_backend(config):
        backends.append(FakeBackend())
        return backends[-1]

    events = []
    c = Controller(make_backend, lambda seq, event: events.append((seq, event)))
    c.events = events
    c.backends = backends
    c.start()
    yield c
    # Blocking calls go through a thread: the hub runs on this test's loop.
    await asyncio.to_thread(c.shutdown)


def state(c):
    return c.snapshot()["status"].state


async def test_connect_type_and_disconnect(ctrl):
    hub = AgentHub({1: TOKEN}, ack_timeout_s=3)
    async with TestServer(hub.app()) as server:
        assert await asyncio.to_thread(ctrl.configure, config_for(server)) is False
        assert state(ctrl) is ConnState.DISCONNECTED  # configure alone does not connect
        ctrl.connect()
        await until(lambda: state(ctrl) is ConnState.CONNECTED and hub.is_online(1))

        ack = await hub.send(1, ChatMessage("party", "", "Иду"), ["Иду"])
        assert ack.ok
        await until(lambda: ctrl.snapshot()["history"])
        snap = ctrl.snapshot()
        assert snap["history"][0].text == "Иду" and snap["history"][0].status == "ok"
        assert snap["seq"] == ctrl.events[-1][0] == len(ctrl.events)

        await asyncio.to_thread(ctrl.disconnect)
        assert state(ctrl) is ConnState.DISCONNECTED
        await until(lambda: not hub.is_online(1))
        assert not ctrl.running


async def test_input_settings_keep_the_connection(ctrl):
    hub = AgentHub({1: TOKEN})
    async with TestServer(hub.app()) as server:
        await asyncio.to_thread(ctrl.configure, config_for(server))
        ctrl.connect()
        await until(lambda: state(ctrl) is ConnState.CONNECTED)
        connected_events = len(ctrl.events)

        assert await asyncio.to_thread(ctrl.configure, config_for(server, part_delay_ms=900)) is False
        assert state(ctrl) is ConnState.CONNECTED and len(ctrl.events) == connected_events
        assert ctrl._agent.config.part_delay_ms == 900 and ctrl._agent.backend is ctrl.backends[-1]
        await asyncio.to_thread(ctrl.disconnect)  # before the test server stops


async def test_new_token_reconnects_and_auth_failure_is_retried_after_fix(ctrl):
    hub = AgentHub({1: TOKEN})
    async with TestServer(hub.app()) as server:
        await asyncio.to_thread(ctrl.configure, config_for(server))
        ctrl.connect()
        await until(lambda: state(ctrl) is ConnState.CONNECTED)

        wrong = AgentConfig(server_url=config_for(server).server_url, token="wrong-token-0123456789", focus_wait_s=1)
        assert await asyncio.to_thread(ctrl.configure, wrong) is True
        await until(lambda: state(ctrl) is ConnState.AUTH_FAILED)
        assert not ctrl.running

        assert await asyncio.to_thread(ctrl.configure, config_for(server)) is True
        await until(lambda: state(ctrl) is ConnState.CONNECTED and hub.is_online(1))
        await asyncio.to_thread(ctrl.disconnect)


async def test_reconnect_and_replaced(ctrl):
    hub = AgentHub({1: TOKEN})
    async with TestServer(hub.app()) as server:
        await asyncio.to_thread(ctrl.configure, config_for(server))
        ctrl.connect()
        await until(lambda: state(ctrl) is ConnState.CONNECTED)
        ctrl.reconnect()
        await until(lambda: [e.state for _, e in ctrl.events if isinstance(e, Status)][-3:]
                    == [ConnState.DISCONNECTED, ConnState.CONNECTING, ConnState.CONNECTED])

        other = Controller(lambda config: FakeBackend())
        other.start()
        try:
            await asyncio.to_thread(other.configure, config_for(server))
            other.connect()
            await until(lambda: state(ctrl) is ConnState.REPLACED)
            assert state(other) is ConnState.CONNECTED and not ctrl.running
        finally:
            await asyncio.to_thread(other.shutdown)


async def test_send_test_and_game_state(ctrl):
    hub = AgentHub({1: TOKEN})
    async with TestServer(hub.app()) as server:
        await asyncio.to_thread(ctrl.configure, config_for(server))
        result = await asyncio.wrap_future(ctrl.send_test())
        assert result["status"] == "ok"
        pastes = [a.value for a in ctrl.backends[-1].runs[0] if a.kind == "paste"]
        assert pastes == ["VF:say::VoiceForever test"]

        ctrl.backends[-1].game = GameState.BACKGROUND
        await until(lambda: ctrl.snapshot()["game"] is GameState.BACKGROUND)
        assert any(isinstance(e, Game) and e.state is GameState.BACKGROUND for _, e in ctrl.events)


# ---------------------------------------------------------------- window API


class StubController:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {
            "seq": 3,
            "status": Status(ConnState.DISCONNECTED),
            "game": GameState.CLOSED,
            "history": [],
            "pad": PadState(False),
            "recording": Recording(RecState.IDLE),
            "bind_check": None,
            "names": Names(),
        }

    def connect(self):
        self.calls.append("connect")


class FakeHost:
    def __init__(self, tmp_path, config=None, errors=None):
        self.controller = StubController()
        self.config = config
        self.config_path = tmp_path / "config.toml"
        self.log_path = tmp_path / "agent.log"
        self.draft = defaults()
        self.errors = errors or {}
        self.applied = []

    def apply_config(self, config):
        self.config = config
        self.applied.append(config)
        return True

    def set_language(self, setting):
        i18n.set_language(setting)
        self.draft["language"] = setting

    def file_errors(self):
        return {k: v for k, v in self.errors.items() if k == "file"}


def test_get_state_first_run(tmp_path):
    state = Api(FakeHost(tmp_path, errors={"token": "x"})).get_state()
    assert state["first_run"] and not state["configured"]
    assert state["config"] == defaults() and state["seq"] == 3
    assert state["status"]["state"] == "disconnected" and state["game"] == "closed"
    json.dumps(state)  # everything must cross into the page


def test_get_state_sends_other_problems_to_settings(tmp_path):
    state = Api(FakeHost(tmp_path, errors={"focus_wait_s": "x"})).get_state()
    assert not state["first_run"]


def test_save_config_reports_errors_without_writing(tmp_path):
    host = FakeHost(tmp_path)
    result = Api(host).save_config(defaults())
    assert not result["ok"] and {"server_url", "token"} <= result["errors"].keys()
    assert not host.config_path.exists() and host.applied == []


def test_save_config_writes_and_applies(tmp_path):
    host = FakeHost(tmp_path)
    values = defaults() | {"server_url": "wss://vf.example.com/ws", "token": TOKEN, "mode": "chat"}
    result = Api(host).save_config(values)
    assert result["ok"] and result["reconnecting"] is True
    assert result["config"]["mode"] == "chat"
    assert AgentConfig.load(host.config_path) == host.applied[0]
    Api(host).connect()
    assert host.controller.calls == ["connect"]


def test_get_state_carries_the_language(tmp_path):
    state = Api(FakeHost(tmp_path)).get_state()
    assert state["language"] == "" and state["lang"] == "en"
    assert [l["code"] for l in state["languages"]] == ["en", "ru", "de", "es", "fr"]
    assert state["strings"]["tabs.settings"] == "Settings"
    assert state["pad_buttons"][0] == {"name": "rightstick", "xbox": "Right stick (RS)", "playstation": "Right stick (R3)"}


def test_set_language_switches_strings_and_buttons(tmp_path):
    host = FakeHost(tmp_path)
    api = Api(host)
    result = api.set_language("fr")
    assert result["language"] == "fr" and result["lang"] == "fr" and host.draft["language"] == "fr"
    assert result["strings"]["tabs.settings"] == "Réglages"
    assert result["pad_buttons"][0]["xbox"] == "Stick droit (RS)"
    assert api.check_config(defaults())["server_url"].startswith("Indique l'adresse")
    assert api.set_language("klingon")["language"] == ""  # unknown: back to the system language


def test_send_test_needs_settings(tmp_path):
    assert Api(FakeHost(tmp_path)).send_test()["status"] == "error"


def test_event_json_shapes():
    assert event_json(1, Status(ConnState.CONNECTED, since=5.0))["status"]["state"] == "connected"
    delivery = event_json(2, Delivery("whisper", "Арторн", "hi", "ok"))
    assert delivery["type"] == "delivery" and delivery["delivery"]["target"] == "Арторн"
    assert event_json(3, Game(GameState.ACTIVE)) == {"type": "game", "seq": 3, "game": "active"}
