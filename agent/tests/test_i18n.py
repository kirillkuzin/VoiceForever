"""The window's languages: complete tables, every key the page and Python ask for, detection."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from vf_agent import i18n
from vf_agent.config import AgentConfig, ConfigError, defaults, peek_language, render

UI = Path(i18n.__file__).resolve().parent / "ui"
SOURCES = Path(i18n.__file__).resolve().parent
GOOD = {"server_url": "wss://vf.example.com/ws", "token": "agent-token-0123456789"}
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def table(lang):
    return json.loads((i18n.LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("lang", list(i18n.LANGUAGES))
def test_every_language_has_every_string(lang):
    source, other = table("en"), table(lang)
    assert other.keys() == source.keys()
    for key, text in other.items():
        assert isinstance(text, str) and text.strip(), key
        assert set(PLACEHOLDER.findall(text)) == set(PLACEHOLDER.findall(source[key])), key


def page_keys():
    html = (UI / "index.html").read_text(encoding="utf-8")
    js = (UI / "app.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'data-i18n(?:-tip)?="([^"]+)"', html))
    for attrs in re.findall(r'data-i18n-attr="([^"]+)"', html):
        keys |= {pair.split(":")[1] for pair in attrs.split(";")}
    keys |= set(re.findall(r"'([a-z]+\.[a-z0-9_.]+)'", js))
    # Keys app.js builds from a state name.
    keys |= {f"conn.{s}" for s in ("connected", "connecting", "reconnecting", "disconnected", "auth_failed", "replaced")}
    keys |= {f"game.{g}" for g in ("active", "background", "closed")}
    keys |= {"unit.s", "unit.ms"}
    keys |= {f"voice.{v}" for v in ("stt_error", "bad_audio", "too_long", "unsupported", "offline", "timeout", "error")}
    keys |= {f"wizard.check_{c}" for c in ("checking", "ok", "auth", "failed", "replaced")}
    keys |= {f"bind.row_{r}" for r in ("game", "focus", "sent", "reload", "done")}
    keys |= {f"bind.row_{r}_now" for r in ("game", "focus", "sent", "reload")}
    keys |= {f"bind.err_{e}" for e in ("addon_missing", "no_game_folder", "chat_mode", "not_connected")}
    keys |= {f"bind.err_{e}_hint" for e in ("addon_missing", "no_game_folder")}
    return keys


def python_keys():
    keys = set()
    for path in SOURCES.rglob("*.py"):
        keys |= set(re.findall(r"""\bt\(\s*["']([a-z]+\.[a-z0-9_.]+)["']""", path.read_text(encoding="utf-8")))
    keys |= {f"tray.{s}" for s in ("connected", "connecting", "reconnecting", "disconnected", "auth_failed", "replaced", "recording")}
    from vf_agent.controller import BADGE_STATUSES
    keys |= {f"badge.{s}" for s in BADGE_STATUSES}
    from vf_agent.ptt import PAD_BUTTONS
    keys |= {f"padbtn.{b.name}" for b in PAD_BUTTONS}
    return keys


def test_page_and_python_ask_only_for_strings_that_exist():
    source = table("en")
    assert sorted(page_keys() - source.keys()) == []
    assert sorted(python_keys() - source.keys()) == []


def test_every_string_is_used():
    from vf_agent.ptt import PAD_BUTTONS
    playstation = {f"padbtn.{b.name}.ps" for b in PAD_BUTTONS}  # optional, looked up by name
    assert sorted(table("en").keys() - page_keys() - python_keys() - playstation) == []


def test_t_fills_placeholders_and_falls_back_to_the_key():
    assert i18n.t("app.version", version="0.3.0") == "agent 0.3.0"
    assert i18n.t("no.such.key") == "no.such.key"
    assert i18n.fill("{a} and {b}", {"a": 1}) == "1 and {b}"
    i18n.set_language("de")
    assert i18n.t("conn.connected") == "Verbunden"


@pytest.mark.parametrize(
    "tag, lang",
    [("ru_RU.UTF-8", "ru"), ("de-AT", "de"), ("es", "es"), ("fr_CA", "fr"), ("en_GB", "en"),
     ("pt_BR", None), ("C", None), ("", None), (None, None)],
)
def test_from_tag(tag, lang):
    assert i18n.from_tag(tag) == lang


@pytest.mark.parametrize(
    "langid, lang",
    [(0x0419, "ru"), (0x0409, "en"), (0x0809, "en"), (0x0407, "de"), (0x0C07, "de"), (0x0C0A, "es"),
     (0x080A, "es"), (0x040C, "fr"), (0x0C0C, "fr"), (0x0415, "en"), (0x0422, "en")],
)
def test_windows_display_language(monkeypatch, langid, lang):
    kernel32 = SimpleNamespace(GetUserDefaultUILanguage=lambda: langid)
    monkeypatch.setattr(i18n.sys, "platform", "win32")
    monkeypatch.setattr(i18n, "ctypes", SimpleNamespace(windll=SimpleNamespace(kernel32=kernel32)), raising=False)
    assert i18n.system_language() == lang


def test_other_systems_read_the_locale(monkeypatch):
    monkeypatch.setattr(i18n.sys, "platform", "linux")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    assert i18n.system_language() == "fr"
    assert i18n.resolve("") == "fr" and i18n.resolve("es") == "es" and i18n.resolve("xx") == "fr"


def test_language_setting_in_config(tmp_path):
    assert defaults()["language"] == ""
    assert AgentConfig.from_dict(GOOD | {"language": " RU "}).language == "ru"
    with pytest.raises(ConfigError) as e:
        AgentConfig.from_dict(GOOD | {"language": "pt"})
    assert "en, ru, de, es, fr" in e.value.errors["language"]
    text = render(AgentConfig.from_dict(GOOD | {"language": "de"}).to_dict())
    assert 'language = "de"' in text and "# Window language" in text
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    assert peek_language(path) == "de"
    assert peek_language(tmp_path / "missing.toml") == ""


def test_config_problems_come_in_the_window_language():
    i18n.set_language("ru")
    with pytest.raises(ConfigError) as e:
        AgentConfig.from_dict(GOOD | {"server_url": ""})
    assert e.value.errors["server_url"].startswith("Укажи адрес сервера")
    i18n.set_language("es")
    with pytest.raises(ConfigError) as e:
        AgentConfig.from_dict(GOOD | {"focus_wait_s": 999})
    assert e.value.errors["focus_wait_s"] == "Se admite de 1 a 120"
