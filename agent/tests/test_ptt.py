"""Push-to-talk rules, button names and the push-to-talk settings."""

import pytest

from vf_agent.config import AgentConfig, ConfigError, render
from vf_agent.ptt import BY_NAME, BY_SDL, MIN_RECORD_S, PAD_BUTTONS, Cmd, PttMachine, PttMode, button_label

TOKEN = "agent-token-0123456789abcdef"


def test_toggle_press_starts_and_second_press_sends():
    m = PttMachine(PttMode.TOGGLE, max_s=30)
    assert m.press(0.0, game_focused=True) is Cmd.START
    assert m.recording
    assert m.release(0.1) is None  # releasing does nothing in toggle mode
    assert m.press(2.0, game_focused=True) is Cmd.SEND
    assert not m.recording


def test_hold_release_sends():
    m = PttMachine(PttMode.HOLD, max_s=30)
    assert m.press(0.0, True) is Cmd.START
    assert m.press(0.5, True) is None
    assert m.release(1.5) is Cmd.SEND
    assert m.release(1.6) is None


@pytest.mark.parametrize("mode", list(PttMode))
def test_short_press_is_dropped(mode):
    m = PttMachine(mode)
    m.press(0.0, True)
    end = m.press if mode is PttMode.TOGGLE else lambda now, _: m.release(now)
    assert end(MIN_RECORD_S / 2, True) is Cmd.CANCEL


def test_needs_the_game_in_front_to_start_but_not_to_stop():
    m = PttMachine(PttMode.TOGGLE)
    assert m.press(0.0, game_focused=False) is None
    assert m.press(0.0, game_focused=True) is Cmd.START
    assert m.press(5.0, game_focused=False) is Cmd.SEND


def test_max_length_sends():
    m = PttMachine(PttMode.TOGGLE, max_s=10)
    m.press(0.0, True)
    assert m.tick(9.9) is None
    assert m.tick(10.0) is Cmd.SEND
    assert m.tick(11.0) is None
    # The next press starts a new recording, like the addon's badge does.
    assert m.press(12.0, True) is Cmd.START


def test_abort():
    m = PttMachine()
    assert m.abort() is None
    m.press(0.0, True)
    assert m.abort() is Cmd.CANCEL and not m.recording


def test_button_tables():
    assert len(BY_NAME) == len(BY_SDL) == len(PAD_BUTTONS)
    assert 5 not in BY_SDL  # Guide / PS button belongs to Windows and Steam
    assert BY_NAME["rightstick"].sdl == 8
    assert button_label("rightstick") == "Правый стик (RS)"
    assert button_label("rightstick", playstation=True) == "Правый стик (R3)"
    assert button_label("back", playstation=True) == "Create"


def test_ptt_settings_defaults_and_validation():
    cfg = AgentConfig.from_dict({"server_url": "wss://vf.example.com/ws", "token": TOKEN})
    assert (cfg.ptt_enabled, cfg.ptt_button, cfg.ptt_mode, cfg.mic_device) == (True, "rightstick", PttMode.TOGGLE, "")
    cfg = AgentConfig.from_dict({
        "server_url": "wss://vf.example.com/ws", "token": TOKEN,
        "ptt_button": " RightStick ", "ptt_mode": "hold", "mic_device": " Headset Mic ", "max_record_s": 45,
    })
    assert (cfg.ptt_button, cfg.ptt_mode, cfg.mic_device, cfg.max_record_s) == ("rightstick", PttMode.HOLD, "Headset Mic", 45)
    with pytest.raises(ConfigError) as e:
        AgentConfig.from_dict({
            "server_url": "wss://vf.example.com/ws", "token": TOKEN,
            "ptt_button": "guide", "ptt_mode": "push", "max_record_s": 500, "ptt_sounds": "yes", "mic_device": 3,
        })
    assert {"ptt_button", "ptt_mode", "max_record_s", "ptt_sounds", "mic_device"} <= e.value.errors.keys()


def test_ptt_settings_round_trip_through_toml(tmp_path):
    cfg = AgentConfig.from_dict({
        "server_url": "wss://vf.example.com/ws", "token": TOKEN, "ptt_mode": "hold", "mic_device": "Микрофон (USB)",
    })
    path = tmp_path / "config.toml"
    cfg.save(path)
    assert AgentConfig.load(path) == cfg
    assert 'ptt_mode = "hold"' in render(cfg.to_dict())
