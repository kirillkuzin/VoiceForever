"""Render the screenshots in this folder from the real agent window and Telegram widgets.

The agent page (agent/vf_agent/ui) runs against a fake window.pywebview.api, and each widget
(server/vf_server/html) against a fake window.TelegramWidget. No Windows, game, server or
Telegram is needed, and the pictures always match the current UI.

    uv run --no-project --python 3.13 --with playwright playwright install chromium   # once
    uv run --no-project --python 3.13 --with playwright python docs/images/render.py

Set VF_CHROMIUM to use a Chromium binary that is already installed, and VF_LANG to render
another language (en, ru, de, es, fr; default en). The UI words come from the same
locales/<lang>.json tables the app and the bot use; only the sample messages below are ours.
"""

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
AGENT_PAGE = ROOT / "agent" / "vf_agent" / "ui" / "index.html"
AGENT_LOCALES = ROOT / "agent" / "vf_agent" / "ui" / "locales"
WIDGETS = ROOT / "server" / "vf_server" / "html"
SERVER_LOCALES = ROOT / "server" / "vf_server" / "locales"
# The version the window, the panel and the setup checklist show: the agent's own.
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "agent" / "vf_agent" / "__init__.py").read_text()).group(1)

SOURCE = "en"
LANGUAGES = {"en": "English", "ru": "Русский", "de": "Deutsch", "es": "Español", "fr": "Français"}
LANG = os.environ.get("VF_LANG", SOURCE)

NOW = datetime(2026, 11, 11, 21, 30, tzinfo=UTC)
T = NOW.timestamp()
SCALE = 2
WINDOW = {"width": 480, "height": 720}  # gui/app.py WIDTH, HEIGHT
WIDGET_WIDTH = 340  # widgets.py WIDGET_WIDTH


def strings(folder: Path, lang: str) -> dict[str, str]:
    """Like i18n.strings in the agent and the server: English, overlaid with `lang`."""
    table = json.loads((folder / f"{SOURCE}.json").read_text(encoding="utf-8"))
    if lang != SOURCE:
        table |= json.loads((folder / f"{lang}.json").read_text(encoding="utf-8"))
    return table


# Sample messages: (spoken phrase, channel, target, text in chat). Names are made up.
SAMPLES = {
    "en": {
        "names": ["Arthorn", "Liana", "Morwen"],
        "party": ("party boss in one minute", "Boss in 1 minute"),
        "whisper": ("whisper arthorn where are we meeting", "Where are we meeting?"),
        "guild": ("tell the guild i'll be back in five minutes", "Back in 5 minutes"),
        "skip": "wait hold on a second",
        "trade": ("trade selling copper ore", "Selling copper ore"),
    },
    "ru": {
        "names": ["Арторн", "Лиана", "Морвен"],
        "party": ("в пати иду на босса через минуту", "Иду на босса через минуту"),
        "whisper": ("шепни арторну где встречаемся", "Где встречаемся?"),
        "guild": ("скажи в гильдию что я отойду на пять минут", "Отойду на 5 минут"),
        "skip": "так подожди я сейчас",
        "trade": ("в трейд продам медную руду", "Продам медную руду"),
    },
}
SAMPLE = SAMPLES.get(LANG, SAMPLES[SOURCE])

# --------------------------------------------------------------------------- the agent

CONFIG = {
    "server_url": "wss://voiceforever.crazybrain.space/ws",  # config.py OFFICIAL_URL
    "token": "k3Jx9wQ2-example-agent-token",
    "mode": "addon",
    "addon_hotkey": "ctrl+shift+f12",
    "window_titles": ["World of Warcraft"],
    "process_names": ["Wow.exe", "WowClassic.exe", "WowForever.exe", "Wow-64.exe"],
    "focus_wait_s": 15,
    "open_delay_ms": 40,
    "part_delay_ms": 350,
    "key_delay_ms": 15,
    "connect_on_start": True,
    "close_to_tray": True,
    "autostart": False,
    "language": LANG,
    "ptt_enabled": True,
    "ptt_button": "rightstick",
    "ptt_mode": "toggle",
    "mic_device": "",
    "max_record_s": 30,
    "ptt_sounds": True,
    "game_context": True,
    "live_context": True,
}

AGENT_STRINGS = strings(AGENT_LOCALES, LANG)
PAD_BUTTONS = [
    {"name": name, "xbox": AGENT_STRINGS[f"padbtn.{name}"],
     "playstation": AGENT_STRINGS.get(f"padbtn.{name}.ps") or AGENT_STRINGS[f"padbtn.{name}"]}
    for name in ("rightstick", "leftstick")
]

PAD = {"connected": True, "name": "Xbox Wireless Controller", "playstation": False, "error": ""}
# Names and channels from the game, as the server has them.
NAMES = {"source": "live", "at": T - 95, "me": "Kirill-Flamegor",
         "counts": {"channels": 4, "group": 4, "guild": 37, "guild_online": 12, "friends": 8, "recent": 5}}
# The plan box on the status tab, as a server with accounts sends it (client.Account).
ACCOUNT = {"plan": "Full access", "ai_left": 87, "ai_limit": 100, "until": T + 24 * 86400,
           "link": "https://voiceforever.crazybrain.space/account"}
MICS = ["Headset Microphone (Xbox Wireless Headset)", "Microphone (Realtek High Definition Audio)"]


def voice(at: float, channel: str, target: str, label: str, text: str, transcript: str, status: str = "ok") -> dict:
    return {"kind": "voice", "id": f"v{at:.0f}", "status": status, "channel": channel, "target": target,
            "label": label, "text": text, "transcript": transcript, "detail": "", "at": at}


def typed(at: float, channel: str, target: str, text: str, status: str = "ok") -> dict:
    return {"kind": "delivery", "channel": channel, "target": target, "text": text, "status": status,
            "detail": "", "at": at, "voice_id": ""}


# Newest first, as the agent keeps them. The page draws the channel names itself.
HISTORY = [
    voice(T - 40, "group", "", "", SAMPLE["party"][1], SAMPLE["party"][0]),
    voice(T - 260, "whisper", SAMPLE["names"][0], "", SAMPLE["whisper"][1], SAMPLE["whisper"][0]),
    typed(T - 610, "guild", "", SAMPLE["guild"][1]),
    voice(T - 900, "", "", "", "", SAMPLE["skip"], status="skip"),
    voice(T - 1500, "channel", "2", "", SAMPLE["trade"][1], SAMPLE["trade"][0]),
]


def agent_state(first_run: bool) -> dict:
    return {
        "language": LANG,
        "lang": LANG,
        "system_lang": LANG,
        "strings": AGENT_STRINGS,
        "languages": [{"code": code, "name": name} for code, name in LANGUAGES.items()],
        "version": VERSION,
        "configured": not first_run,
        "first_run": first_run,
        "config": CONFIG,
        "errors": {},
        "defaults": CONFIG | {"server_url": "", "token": "", "language": ""},
        "config_path": r"C:\Users\Player\AppData\Roaming\VoiceForever\config.toml",
        "seq": 1,
        "status": ({"state": "disconnected", "detail": "", "attempt": 0, "retry_at": None, "since": None}
                   if first_run else
                   {"state": "connected", "detail": "", "attempt": 0, "retry_at": None, "since": T - 3723}),
        "game": "closed" if first_run else "active",
        "history": [] if first_run else HISTORY,
        "pad": PAD,
        "recording": {"state": "idle", "started_at": None, "max_s": 30},
        "bind_check": None,
        "names": NAMES if not first_run else {"source": "", "at": None, "me": "", "counts": {}},
        "account": None if first_run else ACCOUNT,
        "pad_buttons": PAD_BUTTONS,
    }


FAKE_API = """
(() => {
  const state = %s;
  const mics = %s;
  const impl = {
    get_state: async () => state,
    save_config: async (values) => ({ ok: true, errors: {}, config: values, reconnecting: true }),
    check_config: async () => ({}),
    list_mics: async () => ({ mics, error: '' }),
    mic_test: async () => ({ error: '' }),
    send_test: async () => ({ status: 'ok', detail: '' }),
    capture_pad_button: async () => ({ button: '', error: 'timeout' }),
    detect_game_window: async () => ({}),
  };
  // Any other call (connect, open_log, ...) does nothing.
  const api = new Proxy(impl, { get: (target, name) => target[name] || (async () => null) });
  window.pywebview = { api };
})();
"""


def open_agent(page: Page, first_run: bool = False) -> None:
    page.add_init_script(FAKE_API % (json.dumps(agent_state(first_run)), json.dumps(MICS)))
    page.goto(AGENT_PAGE.as_uri())
    page.wait_for_function("document.querySelector('#version').textContent !== ''")
    page.evaluate("document.fonts.ready")


def push(page: Page, event: dict) -> None:
    page.evaluate("(e) => window.vf.onEvent(e)", event)


def agent_status(page: Page) -> None:
    open_agent(page)
    page.wait_for_timeout(300)


def agent_wizard_voice(page: Page) -> None:
    """The third step of the first-run wizard: microphone and gamepad button."""
    open_agent(page, first_run=True)
    page.click("#w-next")  # step 1: save and connect
    push(page, {"type": "status", "seq": 2,
                "status": {"state": "connected", "detail": "", "attempt": 0, "retry_at": None, "since": T}})
    page.wait_for_selector("#view-wizard [data-page='2']:not([hidden])")
    page.click("#w-next")  # step 2: input mode
    page.wait_for_selector("#view-wizard [data-page='3']:not([hidden])")
    page.click("#view-wizard [data-page='3'] [data-mic-test]")
    push(page, {"type": "mic", "seq": 3, "level": 0.46})
    page.wait_for_timeout(300)


def agent_settings(page: Page) -> None:
    open_agent(page)
    page.click(".tab[data-tab='settings']")
    page.wait_for_timeout(300)


# --------------------------------------------------------------------------- Telegram widgets

FAKE_WIDGET = """
window.TelegramWidget = {
  chatData: %s,
  publicData: '',
  themeParams: {},
  request: () => new Promise(() => {}),
  onEvent: () => {},
  openLink: () => {},
  ready: () => {},
};
"""

CARD_ASK = {
    "i": "c1", "s": "ask", "c": "whisper", "t": SAMPLE["names"][0], "x": SAMPLE["whisper"][1],
    "o": SAMPLE["whisper"][0], "n": SAMPLE["names"], "a": 0, "e": "",
}
PANEL = {
    "on": 1, "v": VERSION, "md": "addon", "since": T - 3723, "c": "group", "cf": 0,
    "st": "off", "cs": "", "tr": 1, "n": ", ".join(SAMPLE["names"][:2]), "lg": LANG,
}
SETUP = {"on": 1, "v": VERSION, "md": "addon", "t": "ok", "e": "", "vc": "", "dl": ""}


# widgets.py WIDGET_STRINGS: which strings each widget gets.
WIDGET_STRINGS = {"card": ("card.", "chan."), "panel": ("panel.", "chan.", "mode."), "setup": ("setup.", "mode.")}


def widget_html(name: str) -> str:
    """Like widgets.load_html: the widget's HTML with its strings in LANG."""
    html = (WIDGETS / f"{name}.html").read_text(encoding="utf-8")
    table: dict[str, object] = {
        key.removeprefix(f"{name}."): text
        for key, text in strings(SERVER_LOCALES, LANG).items()
        if key.startswith(WIDGET_STRINGS[name])
    }
    if name == "panel":
        table["langs"] = [[code, title] for code, title in LANGUAGES.items()]
    table_json = json.dumps(table, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return html.replace("L=/*L*/{}", "L=" + table_json, 1)


def widget(page: Page, name: str, data: dict) -> None:
    page.set_viewport_size({"width": WIDGET_WIDTH, "height": 200})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    stub = FAKE_WIDGET % json.dumps(json.dumps(data, ensure_ascii=False))
    html = widget_html(name)
    page.set_content(f"<!doctype html><meta charset='utf-8'><script>{stub}</script>{html}")
    page.wait_for_timeout(200)
    if errors:
        raise RuntimeError(f"{name}.html: {errors}")


# --------------------------------------------------------------------------- run

AGENT_SHOTS = {
    "agent-status.png": agent_status,
    "agent-wizard-voice.png": agent_wizard_voice,
    "agent-settings.png": agent_settings,
}

WIDGET_SHOTS = {
    "telegram-card-ask.png": ("card", CARD_ASK),
    "telegram-panel.png": ("panel", PANEL),
    "telegram-setup.png": ("setup", SETUP),
}


def main() -> None:
    chromium = os.environ.get("VF_CHROMIUM") or None
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium)
        for name, scene in AGENT_SHOTS.items():
            context = browser.new_context(viewport=WINDOW, device_scale_factor=SCALE, timezone_id="UTC")
            context.clock.set_fixed_time(NOW)
            page = context.new_page()
            scene(page)
            page.screenshot(path=HERE / name)
            context.close()
            print(name)
        for name, (html, data) in WIDGET_SHOTS.items():
            context = browser.new_context(device_scale_factor=SCALE, timezone_id="UTC")
            context.clock.set_fixed_time(NOW)
            page = context.new_page()
            widget(page, html, data)
            page.locator("#r").screenshot(path=HERE / name)
            context.close()
            print(name)
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
