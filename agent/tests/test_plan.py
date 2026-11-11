import pytest

from vf_agent.plan import Action, Kind, Mode, SendCommand, addon_line, build_actions, chat_line


def cmd(channel="group", target="", parts=("Привет",)):
    return SendCommand("1", channel, target, tuple(parts))


def test_addon_mode_actions():
    actions = build_actions(cmd(), Mode.ADDON, "ctrl+shift+f12")
    assert actions == [
        Action(Kind.KEY, "ctrl+shift+f12"),
        Action(Kind.SLEEP, 40),
        Action(Kind.PASTE, "VF:group::Привет"),
        Action(Kind.KEY, "enter"),
    ]


def test_chat_mode_actions_and_parts():
    actions = build_actions(cmd("guild", parts=("a", "b")), Mode.CHAT, "x", part_delay_ms=300)
    pastes = [a.value for a in actions if a.kind is Kind.PASTE]
    assert pastes == ["/g a", "/g b"]
    assert Action(Kind.SLEEP, 300) in actions
    assert actions[0] == Action(Kind.KEY, "enter")


@pytest.mark.parametrize(
    "channel, target, expected",
    [
        ("whisper", "Арторн", "/w Арторн hi"),
        ("reply", "", "/r hi"),
        ("channel", "2", "/2 hi"),
        ("group", "", "/p hi"),
        ("unknown", "", "/s hi"),
    ],
)
def test_chat_line(channel, target, expected):
    assert chat_line(channel, target, "hi") == expected


def test_addon_line_strips_colons_from_target():
    assert addon_line("whisper", "A:b", "x:y") == "VF:whisper:Ab:x:y"


def test_from_json_cleans_parts():
    c = SendCommand.from_json({"id": 5, "channel": "say", "parts": ["/run x", "a\nb", "  "]})
    assert c.parts == ("run x", "a b")
    with pytest.raises(ValueError):
        SendCommand.from_json({"id": 1, "parts": "text"})


def test_config_load(tmp_path):
    from vf_agent.config import TEMPLATE, AgentConfig

    path = tmp_path / "config.toml"
    path.write_text(TEMPLATE.replace("paste-agent-token-here", "x" * 20).replace('mode = "addon"', 'mode = "chat"'))
    cfg = AgentConfig.load(path)
    assert cfg.mode is Mode.CHAT and cfg.window_titles == ("World of Warcraft",)
    path.write_text(TEMPLATE)
    with pytest.raises(ValueError):
        AgentConfig.load(path)
