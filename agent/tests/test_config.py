"""config.toml: validation messages for the window, round trips, drafts with problems."""

import tomllib

import pytest

from vf_agent.config import (
    PLACEHOLDER_TOKEN,
    PLACEHOLDER_URL,
    TEMPLATE,
    AgentConfig,
    ConfigError,
    defaults,
    load_draft,
    render,
)
from vf_agent.plan import Mode

GOOD = {"server_url": "wss://vf.example.com/ws", "token": "agent-token-0123456789"}


def errors_of(**values):
    with pytest.raises(ConfigError) as e:
        AgentConfig.from_dict(GOOD | values)
    return e.value.errors


def test_defaults_are_complete_and_empty():
    values = defaults()
    assert values["server_url"] == "" and values["token"] == ""
    assert values["mode"] == "addon" and isinstance(values["window_titles"], list)
    assert values["connect_on_start"] is True and values["autostart"] is False


def test_from_dict_cleans_values():
    cfg = AgentConfig.from_dict(
        GOOD
        | {
            "server_url": "  wss://vf.example.com/ws ",
            "mode": "chat",
            "addon_hotkey": "CTRL-Shift-F11",
            "window_titles": ["World of Warcraft", " World of Warcraft ", ""],
            "open_delay_ms": 40.4,
            "unknown_setting": 1,
        }
    )
    assert cfg.server_url == "wss://vf.example.com/ws"
    assert cfg.mode is Mode.CHAT
    assert cfg.addon_hotkey == "ctrl+shift+f11"
    assert cfg.window_titles == ("World of Warcraft",)
    assert cfg.open_delay_ms == 40


@pytest.mark.parametrize(
    "values, field",
    [
        ({"server_url": ""}, "server_url"),
        ({"server_url": PLACEHOLDER_URL}, "server_url"),
        ({"server_url": "https://vf.example.com/ws"}, "server_url"),
        ({"server_url": "wss:///ws"}, "server_url"),
        ({"token": PLACEHOLDER_TOKEN}, "token"),
        ({"token": "short"}, "token"),
        ({"token": "has a space in the middle"}, "token"),
        ({"mode": "voice"}, "mode"),
        ({"addon_hotkey": "ctrl+shift"}, "addon_hotkey"),
        ({"addon_hotkey": "ctrl+banana"}, "addon_hotkey"),
        ({"window_titles": "World of Warcraft"}, "window_titles"),
        ({"window_titles": [], "process_names": []}, "process_names"),
        ({"focus_wait_s": 0}, "focus_wait_s"),
        ({"part_delay_ms": "350"}, "part_delay_ms"),
        ({"key_delay_ms": True}, "key_delay_ms"),
        ({"autostart": "yes"}, "autostart"),
    ],
)
def test_each_bad_setting_is_reported(values, field):
    assert field in errors_of(**values)


def test_all_problems_are_reported_at_once():
    errors = errors_of(server_url="", token="", focus_wait_s=500)
    assert errors.keys() == {"server_url", "token", "focus_wait_s"}
    assert all(isinstance(message, str) and message for message in errors.values())


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "sub" / "config.toml"
    cfg = AgentConfig.from_dict(
        GOOD | {"window_titles": ['Wow "Forever"', "Мир"], "focus_wait_s": 7.5, "autostart": True}
    )
    cfg.save(path)
    assert AgentConfig.load(path) == cfg
    assert not path.with_name("config.toml.tmp").exists()
    text = path.read_text(encoding="utf-8")
    assert "# Address of your server" in text and "Мир" in text


def test_render_writes_valid_toml_for_template():
    data = tomllib.loads(TEMPLATE)
    assert data["server_url"] == PLACEHOLDER_URL and data["token"] == PLACEHOLDER_TOKEN
    assert tomllib.loads(render(defaults())) == defaults()


def test_load_reports_broken_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("server_url = ", encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        AgentConfig.load(path)
    assert "file" in e.value.errors


def test_load_accepts_bom(tmp_path):
    path = tmp_path / "config.toml"
    AgentConfig.from_dict(GOOD).save(path)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert AgentConfig.load(path).token == GOOD["token"]


def test_draft_for_missing_file(tmp_path):
    config, draft, errors = load_draft(tmp_path / "config.toml")
    assert config is None and draft == defaults() and errors == {}


def test_draft_for_template_hides_placeholders(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(TEMPLATE.replace('mode = "addon"', 'mode = "chat"'), encoding="utf-8")
    config, draft, errors = load_draft(path)
    assert config is None
    assert draft["server_url"] == "" and draft["token"] == "" and draft["mode"] == "chat"
    assert errors.keys() == {"server_url", "token"}


def test_draft_for_valid_file(tmp_path):
    path = tmp_path / "config.toml"
    AgentConfig.from_dict(GOOD).save(path)
    config, draft, errors = load_draft(path)
    assert config is not None and draft == config.to_dict() and errors == {}


def test_draft_for_broken_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[[[", encoding="utf-8")
    config, draft, errors = load_draft(path)
    assert config is None and draft == defaults() and "file" in errors
