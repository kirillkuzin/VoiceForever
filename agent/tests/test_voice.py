"""Push-to-talk end to end: fake pad and microphone, the real server hub, a fake keyboard."""

import asyncio
import re
import sys
import time
from concurrent.futures import Future
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from vf_server.chat import Channel, ChatMessage  # noqa: E402
from vf_server.hub import AgentHub  # noqa: E402

from vf_agent import audio  # noqa: E402
from vf_agent import controller as controller_module  # noqa: E402
from vf_agent.client import BindCheck, ConnState, PadState, RecState, Recording, VoiceResult  # noqa: E402
from vf_agent.config import AgentConfig  # noqa: E402
from vf_agent.controller import Controller  # noqa: E402
from vf_agent.devices import Pad, Press  # noqa: E402
from vf_agent.plan import GameState, Mode, SendCommand  # noqa: E402
from vf_agent.ptt import PttMode  # noqa: E402

TOKEN = "agent-token-0123456789abcdef"
SECOND = audio.tone([(300, 1.0)], rate=audio.RATE)


def done(value):
    future = Future()
    future.set_result(value)
    return future


class FakeDevices:
    error = None

    def __init__(self, on_press, on_pad, on_level):
        self.on_press, self.on_pad, self.on_level = on_press, on_pad, on_level
        self.open = None  # (device, keep) while the microphone is open
        self.beeps = []

    def start(self):
        self.on_pad(Pad("Xbox Controller", False))
        return True

    def stop(self):
        pass

    def microphones(self):
        return done(["Микрофон (USB)", "Headset"])

    def start_recording(self, device, keep=True):
        self.open = (device, keep)
        self.on_level(0.5)
        return done(device)

    def stop_recording(self):
        self.open = None
        return done(SECOND)

    def beep(self, name):
        self.beeps.append(name)

    def press(self, button, down=True):
        self.on_press(Press(button, down, Pad("Xbox Controller", False), time.monotonic()))


class FakeBackend:
    def __init__(self, exe=None):
        self.game = GameState.ACTIVE
        self.exe = exe
        self.runs = []

    def is_game_focused(self):
        return self.game is GameState.ACTIVE

    def game_state(self):
        return self.game

    def game_exe(self):
        return self.exe

    def run(self, actions):
        self.runs.append(actions)

    def pastes(self):
        return [a.value for run in self.runs for a in run if a.kind == "paste"]


async def until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.fixture
async def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(controller_module, "GAME_POLL_S", 0.05)
    hub = AgentHub({1: TOKEN}, ack_timeout_s=3)
    replies = {"status": None}

    async def voice_handler(user_id, upload):
        assert upload.format == "wav" and upload.duration_ms == 1000
        if replies["status"] is not None:
            return {"status": replies["status"], "detail": "x"}
        message = ChatMessage(Channel.GUILD, "", "Всем привет")
        ack = await hub.send(user_id, message, ["Всем привет"], upload.id)
        return {"status": str(ack.status), "channel": "guild", "label": "Гильдия",
                "text": "Всем привет", "transcript": "в гильдию всем привет"}

    hub.voice_handler = voice_handler
    exe = tmp_path / "_forever_" / "Wow.exe"
    backend = FakeBackend(exe)
    devices = []
    events = []

    def make_devices(*callbacks):
        devices.append(FakeDevices(*callbacks))
        return devices[0]

    c = Controller(lambda config: backend, lambda seq, e: events.append(e), make_devices=make_devices)
    async with TestServer(hub.app()) as server:
        url = str(server.make_url("/ws")).replace("http", "ws")
        c.start()

        def configure(**values):
            config = AgentConfig(server_url=url, token=TOKEN, focus_wait_s=1, **values)
            c.configure(config)
            return config

        await asyncio.to_thread(configure)
        c.connect()
        await until(lambda: c.snapshot()["status"].state is ConnState.CONNECTED)
        await until(lambda: devices)
        yield c, devices[0], backend, events, replies, configure
        await asyncio.to_thread(c.shutdown)


def of(events, kind):
    return [e for e in events if isinstance(e, kind)]


async def test_toggle_records_and_types(setup):
    c, pad, backend, events, _, _ = setup
    assert c.snapshot()["pad"] == PadState(True, "Xbox Controller", False)
    pad.press("rightstick")
    await until(lambda: c.snapshot()["recording"].state is RecState.RECORDING)
    assert pad.open == ("", True)
    pad.press("rightstick", down=False)  # release does nothing in toggle mode
    await asyncio.sleep(0.35)
    pad.press("rightstick")
    await until(lambda: of(events, VoiceResult))
    [result] = of(events, VoiceResult)
    assert (result.status, result.label, result.text) == ("ok", "Гильдия", "Всем привет")
    assert backend.pastes() == ["VF:guild::Всем привет"]
    assert pad.beeps == ["start", "stop"] and pad.open is None
    await until(lambda: c.snapshot()["recording"].state is RecState.IDLE)
    assert c.snapshot()["history"][0] == result
    assert [d.voice_id for d in c.snapshot()["history"][1:]] == [result.id]


async def test_hold_mode_and_other_buttons(setup):
    c, pad, backend, events, _, configure = setup
    await asyncio.to_thread(configure, ptt_mode=PttMode.HOLD, ptt_button="leftstick", mic_device="Headset")
    pad.press("rightstick")  # not the push-to-talk button any more
    await asyncio.sleep(0.1)
    assert pad.open is None
    pad.press("leftstick")
    await until(lambda: pad.open == ("Headset", True))
    await asyncio.sleep(0.35)
    pad.press("leftstick", down=False)
    await until(lambda: of(events, VoiceResult))
    assert backend.pastes() == ["VF:guild::Всем привет"]


async def test_short_press_is_dropped(setup):
    c, pad, backend, events, _, _ = setup
    pad.press("rightstick")
    await until(lambda: pad.open)
    pad.press("rightstick")
    await until(lambda: pad.beeps == ["start", "cancel"])
    await asyncio.sleep(0.2)
    assert of(events, VoiceResult) == [] and backend.pastes() == []


async def test_nothing_starts_without_the_game_in_front(setup):
    c, pad, backend, events, _, _ = setup
    backend.game = GameState.BACKGROUND
    pad.press("rightstick")
    await asyncio.sleep(0.2)
    assert pad.open is None and of(events, Recording) == []


async def test_refused_voice_tells_the_addon(setup):
    c, pad, backend, events, replies, _ = setup
    replies["status"] = "empty"
    pad.press("rightstick")
    await until(lambda: pad.open)
    await asyncio.sleep(0.35)
    pad.press("rightstick")
    await until(lambda: backend.pastes())
    assert backend.pastes() == ["VF:status:empty:Не расслышал ни слова"]
    assert of(events, VoiceResult)[0].status == "empty"
    assert pad.beeps == ["start", "stop", "error"]


async def test_mic_test_and_microphones(setup):
    c, pad, *_ = setup
    assert await asyncio.to_thread(c.microphones) == ["Микрофон (USB)", "Headset"]
    await asyncio.to_thread(c.mic_test, True, "Headset")
    assert pad.open == ("Headset", False)
    await asyncio.to_thread(c.mic_test, False)
    assert pad.open is None


async def test_capture_button(setup):
    c, pad, *_ = setup
    waiter = asyncio.create_task(asyncio.to_thread(c.capture_button, 5))
    await asyncio.sleep(0.1)
    pad.press("misc1")
    press = await waiter
    assert press.button == "misc1" and press.pad.name == "Xbox Controller"
    assert pad.open is None  # the captured press does not record


async def test_bind_check(setup):
    c, pad, backend, events, _, _ = setup
    c.check_binding()
    await until(lambda: backend.pastes())
    [line] = backend.pastes()
    m = re.fullmatch(r"VF:ptt:setup:toggle 30 ([0-9a-f]{8})", line)
    assert m, line
    await until(lambda: [b.step for b in of(events, BindCheck)][-1:] == ["sent"])
    pad.press("rightstick")  # in the game, the addon catches this press
    await until(lambda: of(events, BindCheck)[-1].step == "reloading")
    saved = backend.exe.parent / "WTF" / "Account" / "ACC" / "SavedVariables" / "VoiceForever.lua"
    saved.parent.mkdir(parents=True)
    saved.write_text(
        'VoiceForeverDB = {\n["ptt"] = {\n["key"] = "PADRSTICK",\n["previous"] = "TOGGLEPINGLISTENER",\n'
        f'["mode"] = "toggle",\n["max"] = 30,\n["nonce"] = "{m[1]}",\n}},\n}}\n',
        encoding="utf-8",
    )
    await until(lambda: of(events, BindCheck)[-1].step == "done")
    result = of(events, BindCheck)[-1]
    assert (result.key, result.previous, result.taken) == ("PADRSTICK", "TOGGLEPINGLISTENER", True)
    assert [b.step for b in of(events, BindCheck)] == ["waiting_game", "waiting_focus", "sent", "reloading", "done"]
    assert pad.open is None


async def test_bind_check_needs_addon_mode(setup):
    c, pad, backend, events, _, configure = setup
    await asyncio.to_thread(configure, mode=Mode.CHAT)
    c.check_binding()
    await until(lambda: of(events, BindCheck))
    assert of(events, BindCheck)[-1].error == "chat_mode"


async def test_addon_gets_a_changed_mode(setup):
    c, pad, backend, events, _, configure = setup
    saved = backend.exe.parent / "WTF" / "Account" / "ACC" / "SavedVariables" / "VoiceForever.lua"
    saved.parent.mkdir(parents=True)
    saved.write_text('VoiceForeverDB = { ["ptt"] = { ["key"] = "PADRSTICK", ["mode"] = "toggle", ["max"] = 30 } }')
    await asyncio.sleep(0.2)
    assert backend.pastes() == []  # the addon already has these settings
    await asyncio.to_thread(configure, ptt_mode=PttMode.HOLD, max_record_s=20)
    await until(lambda: backend.pastes())
    assert backend.pastes() == ["VF:ptt:config:hold 20"]


def test_server_cannot_send_control_lines():
    with pytest.raises(ValueError):
        SendCommand.from_json({"id": "1", "channel": "ptt", "parts": ["setup"]})
