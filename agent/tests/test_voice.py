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
from vf_agent.client import BindCheck, ConnState, Names, PadState, RecState, Recording, VoiceResult  # noqa: E402
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
        self.answer = None  # what the addon puts in its box when asked for names: nonce -> text

    def is_game_focused(self):
        return self.game is GameState.ACTIVE

    def game_state(self):
        return self.game

    def game_exe(self):
        return self.exe

    def run(self, actions):
        self.runs.append(actions)
        for a in actions:
            if a.kind == "copy":
                nonce = a.value.split()[1]
                return self.answer(nonce) if self.answer else None
        return None

    def exports(self):
        return [a.value for run in self.runs for a in run if a.kind == "paste" and a.value.startswith("VF:export:")]

    def pastes(self):
        return [a.value for run in self.runs for a in run if a.kind == "paste" and not a.value.startswith("VF:export:")]


async def until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.fixture
async def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(controller_module, "GAME_POLL_S", 0.05)
    monkeypatch.setattr(controller_module, "SAVED_NAMES_EVERY_S", 0.05)
    hub = AgentHub({1: TOKEN}, ack_timeout_s=3)
    replies = {"status": None, "contexts": [], "contexts_at_voice": None}
    hub.context_handler = lambda user_id, data: replies["contexts"].append(data)

    async def voice_handler(user_id, upload):
        assert upload.format == "wav" and upload.duration_ms == 1000
        replies["contexts_at_voice"] = len(replies["contexts"])
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
    assert backend.pastes() == ["VF:status:empty:Didn't catch a word"]
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
    write_result(backend, m[1])
    await until(lambda: of(events, BindCheck)[-1].step == "done")
    result = of(events, BindCheck)[-1]
    assert (result.key, result.previous, result.taken) == ("PADRSTICK", "TOGGLEPINGLISTENER", True)
    assert result.previous_name == "Метка цели"
    assert [b.step for b in of(events, BindCheck)] == ["waiting_game", "waiting_focus", "sent", "reloading", "done"]
    assert pad.open is None


def write_result(backend, nonce: str, key: str = "PADRSTICK") -> None:
    """What the addon saves after the player pressed the button in the game and it reloaded."""
    saved = backend.exe.parent / "WTF" / "Account" / "ACC" / "SavedVariables" / "VoiceForever.lua"
    saved.parent.mkdir(parents=True, exist_ok=True)
    name = "".join(f"\\{b}" for b in "Метка цели".encode())  # the game writes UTF-8 as \ddd
    saved.write_text(
        f'VoiceForeverDB = {{\n["ptt"] = {{\n["key"] = "{key}",\n["previous"] = "TOGGLEPINGLISTENER",\n'
        f'["previousName"] = "{name}",\n["mode"] = "toggle",\n["max"] = 30,\n["nonce"] = "{nonce}",\n}},\n}}\n',
        encoding="utf-8",
    )


async def test_bind_check_notices_another_button(setup):
    c, pad, backend, events, _, _ = setup
    c.check_binding()
    await until(lambda: backend.pastes())
    nonce = backend.pastes()[0].rsplit(" ", 1)[1]
    await until(lambda: of(events, BindCheck)[-1].step == "sent")
    pad.press("leftstick")  # the agent records on the right stick
    await until(lambda: of(events, BindCheck)[-1].step == "reloading")
    write_result(backend, nonce, key="PADLSTICK")
    await until(lambda: of(events, BindCheck)[-1].step == "error")
    result = of(events, BindCheck)[-1]
    assert result.error == "wrong_button"
    assert "Left stick" in result.detail and "Right stick" in result.detail


async def test_capture_can_be_cancelled(setup):
    c, *_ = setup
    waiter = asyncio.create_task(asyncio.to_thread(c.capture_button, 5))
    await asyncio.sleep(0.1)
    c.cancel_capture()
    assert await asyncio.wait_for(waiter, 2) is None


async def test_bind_check_needs_addon_mode(setup):
    c, pad, backend, events, _, configure = setup
    await asyncio.to_thread(configure, mode=Mode.CHAT)
    c.check_binding()
    await until(lambda: of(events, BindCheck))
    assert of(events, BindCheck)[-1].error == "chat_mode"


async def test_addon_gets_a_changed_mode(setup, monkeypatch):
    monkeypatch.setattr(controller_module, "ADDON_REREAD_S", 0.1)  # the agent may look before the file is written
    c, pad, backend, events, _, configure = setup
    saved = backend.exe.parent / "WTF" / "Account" / "ACC" / "SavedVariables" / "VoiceForever.lua"
    saved.parent.mkdir(parents=True)
    saved.write_text('VoiceForeverDB = { ["ptt"] = { ["key"] = "PADRSTICK", ["mode"] = "toggle", ["max"] = 30 } }')
    await asyncio.sleep(0.4)
    assert backend.pastes() == []  # the addon already has these settings
    await asyncio.to_thread(configure, ptt_mode=PttMode.HOLD, max_record_s=20)
    await until(lambda: backend.pastes())
    assert backend.pastes() == ["VF:ptt:config:hold 20"]


def test_server_cannot_send_control_lines():
    with pytest.raises(ValueError):
        SendCommand.from_json({"id": "1", "channel": "ptt", "parts": ["setup"]})


# ---------------------------------------------------------------- names from the game
EXPORT = (
    '{"at":1762950000,"channels":[{"n":5,"name":"Рейдеры"}],"friends":[],"group":["Арторн"],'
    '"guild":[{"name":"Лиана","online":true}],"me":"Кирилл-Пламегор","speakers":[],"v":1,'
    '"whispers":[{"ago":3,"dir":"in","name":"Варг"}]}'
)


async def record_once(pad, events):
    count = len(of(events, VoiceResult))
    pad.press("rightstick")
    await until(lambda: pad.open)
    await asyncio.sleep(0.35)
    pad.press("rightstick")
    await until(lambda: len(of(events, VoiceResult)) > count)


async def test_fresh_names_reach_the_server_before_the_recording(setup):
    c, pad, backend, events, replies, _ = setup
    backend.answer = lambda nonce: f"VFX1 {nonce} {EXPORT}"
    await record_once(pad, events)
    [export] = backend.exports()
    assert re.fullmatch(r"VF:export:[0-9a-f]{8}:", export)
    [context] = replies["contexts"]
    assert replies["contexts_at_voice"] == 1  # the server had the names for the recording
    assert context["type"] == "context" and context["source"] == "live" and context["age_s"] == 0
    assert context["channels"] == [{"n": 5, "name": "Рейдеры"}] and context["group"] == ["Арторн"]
    # The copy worked, so Enter closes the addon's box; then the message is typed.
    export_run = next(i for i, run in enumerate(backend.runs) if any(a.value == export for a in run))
    assert [a.value for a in backend.runs[export_run + 1]] == ["enter"]
    assert backend.pastes() == ["VF:guild::Всем привет"]
    names = c.snapshot()["names"]
    assert names.source == "live" and names.me == "Кирилл-Пламегор" and names.counts["guild_online"] == 1
    assert of(events, Names)[-1] == names
    await record_once(pad, events)  # a moment later: no second copy yet
    assert len(backend.exports()) == 1


async def test_old_addon_does_not_answer(setup):
    c, pad, backend, events, replies, _ = setup
    await record_once(pad, events)
    assert len(backend.exports()) == 1 and replies["contexts"] == []
    # Nothing was copied, so no Enter: it would open the game's chat box.
    assert not any([a.value for a in run] == ["enter"] for run in backend.runs)
    assert c.snapshot()["names"] == Names()


async def test_names_from_the_saved_file_and_turning_them_off(setup):
    c, pad, backend, events, replies, configure = setup
    saved = backend.exe.parent / "WTF" / "Account" / "ACC" / "SavedVariables" / "VoiceForever.lua"
    saved.parent.mkdir(parents=True)
    saved.write_text(
        'VoiceForeverDB = { ["chars"] = { ["Кирилл-Пламегор"] = { ["v"] = 1, ["at"] = 1762950000, '
        '["channels"] = { { ["n"] = 5, ["name"] = "Рейдеры" } }, ["guild"] = {} } } }',
        encoding="utf-8",
    )
    await until(lambda: replies["contexts"])
    [context] = replies["contexts"]
    assert context["source"] == "file" and context["channels"] == [{"n": 5, "name": "Рейдеры"}]
    assert context["age_s"] > 0
    await until(lambda: c.snapshot()["names"].source == "file")
    await asyncio.to_thread(configure, game_context=False)
    await until(lambda: len(replies["contexts"]) == 2)
    assert replies["contexts"][-1] is None  # the server forgets them
    assert c.snapshot()["names"] == Names()
    await record_once(pad, events)
    assert backend.exports() == []  # and nothing is copied from the game
