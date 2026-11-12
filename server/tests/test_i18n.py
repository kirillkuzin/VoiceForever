"""The bot's languages: complete tables, keys the code asks for, detection, the switch."""

import json
import re
import sqlite3
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession

from vf_server import i18n
from vf_server.access import Reason
from vf_server.bot import (
    COMMANDS,
    PAYMENT_COMMANDS,
    describe,
    note_text,
    parse_channel,
    parse_language,
    set_commands,
    setup_bot,
)
from vf_server.chat import Channel, ChatMessage
from vf_server.hub import Ack, AckStatus
from vf_server.router import Decision, RouteResult
from vf_server.service import Delivery, VoiceService
from vf_server.store import Store
from vf_server.tgwidgets import EditMessageWidgetData, SendWidget
from vf_server.widgets import WIDGET_STRINGS, load_html
from tests.test_widgets import USER, FakeBot, FakeHub, make_ui, query, tg_message

PACKAGE = Path(i18n.__file__).resolve().parent
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def table(lang):
    return json.loads((PACKAGE / "locales" / f"{lang}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("lang", i18n.LANGUAGES)
def test_every_language_has_every_string(lang):
    source, other = table("en"), table(lang)
    assert other.keys() == source.keys()
    for key, text in other.items():
        assert isinstance(text, str) and text.strip(), key
        assert set(PLACEHOLDER.findall(text)) == set(PLACEHOLDER.findall(source[key])), key


def python_keys():
    keys = set()
    prefixes = "|".join(sorted({key.split(".")[0] for key in table("en")}))
    for path in PACKAGE.glob("*.py"):
        keys |= set(re.findall(rf"""["']((?:{prefixes})\.[a-z0-9_.]+)["']""", path.read_text(encoding="utf-8")))
    keys.discard("note.mp4")  # a file name in bot.py
    # Keys built from a value.
    keys |= {f"chan.{c}" for c in Channel}
    keys |= {f"cmd.{c}" for c in COMMANDS + PAYMENT_COMMANDS}
    keys |= {f"access.{r}" for r in Reason}
    keys.add("setup.s1_ok_account")  # replaces setup.s1_ok with an access service
    keys |= {f"note.{n}" for n in ("llm_limit", "llm_error", "llm_down", "llm_format")}
    keys |= {"mode.addon", "mode.chat"}
    return keys


def widget_keys():
    keys = set()
    dynamic = {
        "card": {"ask", "busy", "sent", "fail", "cancel"},
        "panel": {"online", "offline", "online_hint", "online_since", "offline_hint", "confirm_on", "confirm_off"}
        | {f"style_{k}{x}" for k in ("off", "short", "rp", "custom") for x in ("", "_hint")},
        "setup": {"again", "send_test"},
    }
    for name, prefixes in WIDGET_STRINGS.items():
        html = (PACKAGE / "html" / f"{name}.html").read_text(encoding="utf-8")
        literals = {lit for call in re.findall(r"\bT\(([^()]*)", html) for lit in re.findall(r"'([a-z0-9_.]+)'", call)}
        for key in {k for k in literals if not k.endswith(("_", ".")) and not k.startswith("_")} | dynamic[name]:
            keys.add(key if key.startswith(prefixes[1:]) else f"{name}.{key}")
        keys |= {f"mode.{m}" for m in ("chat", "addon")} if "mode." in prefixes else set()
        keys |= {f"chan.{c}" for c in Channel} if "chan." in prefixes else set()
    return keys


def test_code_asks_only_for_strings_that_exist_and_uses_them_all():
    source = table("en").keys()
    used = python_keys() | widget_keys()
    assert sorted(used - source) == []
    assert sorted(source - used) == []


def test_t_fills_placeholders_and_falls_back_to_the_key():
    assert i18n.t("de", "bot.channel_set", channel="Gilde") == "Standardkanal: Gilde"
    assert i18n.t("ru", "chan.whisper_to") == "Шёпот → {name}"
    assert i18n.t("fr", "no.such.key") == "no.such.key"
    assert i18n.strings("pt") == i18n.strings("en")


@pytest.mark.parametrize(
    "code, lang",
    [("ru", "ru"), ("en-US", "en"), ("de-AT", "de"), ("es-419", "es"), ("fr_CA", "fr"), ("RU", "ru"),
     ("pt-br", None), ("uk", None), ("", None), (None, None)],
)
def test_from_code(code, lang):
    assert i18n.from_code(code) == lang


def test_old_database_gets_the_language_columns(tmp_path):
    path = tmp_path / "vf.db"
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE ui_state (
               user_id INTEGER PRIMARY KEY, widgets INTEGER NOT NULL DEFAULT 1,
               panel_msg_id INTEGER NOT NULL DEFAULT 0, setup_msg_id INTEGER NOT NULL DEFAULT 0,
               setup_test TEXT NOT NULL DEFAULT '', setup_voice TEXT NOT NULL DEFAULT '',
               custom_style TEXT NOT NULL DEFAULT '') STRICT"""
    )
    db.execute("INSERT INTO ui_state (user_id, panel_msg_id, custom_style) VALUES (?, 7, 'орк')", (USER,))
    db.commit()
    db.close()
    store = Store(str(path))
    ui = store.ui(USER)
    assert (ui.panel_msg_id, ui.custom_style, ui.lang, ui.tg_lang) == (7, "орк", "", "")
    store.update_ui(USER, lang="de")
    assert Store(str(path)).ui(USER).lang == "de"


# ---------------------------------------------------------------------- widgets


@pytest.mark.parametrize("name", WIDGET_STRINGS)
def test_widgets_carry_the_strings_of_their_language(name):
    for lang in i18n.LANGUAGES:
        html = load_html(name, lang)
        assert "L=/*L*/{}" not in html and html.count("</script>") == 1
        own = {k.removeprefix(f"{name}."): v for k, v in i18n.strings(lang).items() if k.startswith(f"{name}.")}
        start = html.index("L=") + 2
        injected, _ = json.JSONDecoder().raw_decode(html[start:].replace("<\\/", "</"))
        assert own.items() <= injected.items()
    assert "Отправить" in load_html("card", "ru") and "Отправить" not in load_html("card", "en")
    assert '["fr","Français"]' in load_html("panel", "de")


def test_language_follows_telegram_until_the_player_picks_one():
    ui, _ = make_ui()
    assert ui.lang(USER) == "en"
    assert ui.lang(USER, "ru") == "ru"
    assert ui.lang(USER) == "ru"  # remembered for refreshes without an update at hand
    assert ui.lang(USER, "pt-br") == "en"
    ui.store.update_ui(USER, lang="de")
    assert ui.lang(USER, "ru") == "de" and ui.telegram_lang(USER) == "ru"


async def test_cards_and_failures_come_in_the_player_language():
    ui, hub = make_ui()
    ui.lang(USER, "es")
    hub.status = AckStatus.NOT_FOCUSED
    bot = FakeBot()
    await ui.show_card(bot, tg_message(), USER, ChatMessage(Channel.GUILD, "", "hola"), "hola", False)
    assert "Responder" in bot.of(SendWidget)[0].html
    assert json.loads(bot.of(EditMessageWidgetData)[-1].chat_data)["e"] == i18n.t("es", "ack.not_focused")

    await ui.on_query(bot, query("s:unknown1:say"))
    assert bot.calls[-1].data == i18n.t("es", "widget.stale")


async def test_panel_language_row_sends_the_panel_again():
    ui, _ = make_ui()
    store = ui.store
    bot = FakeBot()
    await ui.send_panel(bot, USER, USER)
    assert json.loads(bot.of(SendWidget)[0].chat_data)["lg"] == "en"

    await ui.on_query(bot, query("l:fr"))
    assert store.ui(USER).lang == "fr"
    panel = bot.of(SendWidget)[-1]
    assert "Canal par défaut" in panel.html and json.loads(panel.chat_data)["lg"] == "fr"
    assert ("delete", 100) in bot.calls and ("pin", 101) in bot.calls
    assert store.ui(USER).panel_msg_id == 101

    await ui.on_query(bot, query("l:fr"))  # the same language: nothing to resend
    await ui.on_query(bot, query("l:xx"))
    assert len(bot.of(SendWidget)) == 2 and store.ui(USER).lang == "fr"


# ---------------------------------------------------------------------- bot


def test_channel_and_language_arguments():
    for arg, channel in [("пати", "group"), ("guild", "guild"), ("Gilde", "guild"), ("hermandad", "guild"),
                         ("Schlachtzug", "raid"), ("groupe", "group"), ("Group", "group"), ("dire", "say")]:
        assert parse_channel(arg) == channel, arg
    assert parse_channel("whisper") is None and parse_channel("Flüstern") is None and parse_channel("") is None
    for arg, lang in [("de", "de"), ("ES", "es"), ("Français", "fr"), ("russian", "ru"), ("auto", ""),
                      ("klingon", None), ("", None)]:
        assert parse_language(arg) == lang, arg


def test_delivery_texts():
    msg = ChatMessage(Channel.GUILD, "", "Hallo")
    ok = Delivery(Ack(AckStatus.OK), msg, ["a", "b"])
    assert describe(ok, "de") == "✓ [Gilde] Hallo (2 Nachrichten)"
    failed = Delivery(Ack(AckStatus.ERROR, "no edit box"), msg, ["a"])
    assert describe(failed, "fr") == "⚠️ L'agent n'a pas pu taper le texte: no edit box\n[Guilde] Hallo"
    routed = RouteResult(Decision.SEND, msg, "rules", note="llm_error", note_code=529)
    assert note_text(routed, "en") == "\n(LLM error 529)"
    assert note_text(RouteResult(Decision.SEND, msg, "llm"), "en") == ""


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def make_request(self, bot, method, timeout=None):
        self.requests.append((method.__api_method__, method.model_dump(exclude_none=True)))
        return True

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def close(self):
        pass


@pytest.fixture
def bot_env():
    service = VoiceService(Store(":memory:"), FakeHub(), transcriber=None, router=None)
    dp = Dispatcher()
    setup_bot(dp, service)
    session = RecordingSession()
    bot = Bot("123456:" + "A" * 35, session=session)
    updates = iter(range(1, 1000))

    def user(uid, code):
        return {"id": uid, "is_bot": False, "first_name": "K"} | ({"language_code": code} if code else {})

    async def send(text, uid=USER, code=None):
        command = text.split()[0]
        await dp.feed_raw_update(bot, {"update_id": next(updates), "message": {
            "message_id": 1, "date": 0, "chat": {"id": uid, "type": "private"}, "from": user(uid, code),
            "text": text, "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
        }})
        return session.requests[-1][1]

    async def tap(data, code=None):
        await dp.feed_raw_update(bot, {"update_id": next(updates), "callback_query": {
            "id": "c1", "from": user(USER, code), "chat_instance": "1", "data": data,
            "message": {"message_id": 9, "date": 1, "chat": {"id": USER, "type": "private"}, "text": "…"},
        }})
        return [r for r in session.requests if r[0] == "editMessageText"][-1][1]

    return service, session, send, tap, bot


async def test_strangers_are_told_their_id_in_their_language(bot_env):
    _, _, send, _, _ = bot_env
    reply = await send("/start", uid=7, code="de-DE")
    assert reply["text"] == "Deine Telegram-ID ist 7. Trag sie auf dem Server in VF_USERS ein, um den Bot zu nutzen."


async def test_replies_follow_telegram_then_the_language_command(bot_env):
    service, _, send, tap, _ = bot_env
    reply = await send("/status", code="fr")
    assert "Langue\u00a0: Français, comme dans Telegram" in reply["text"]
    assert "Canal par défaut\u00a0: Dire" in reply["text"]

    reply = await send("/language", code="fr")
    rows = reply["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in rows[0]] == ["English", "Русский", "Deutsch"]
    assert [b["text"] for b in rows[1]] == ["Español", "Français"]
    assert rows[2][0] == {"text": "✓ Comme dans Telegram (Français)", "callback_data": "lang:auto"}

    edited = await tap("lang:ru", code="fr")
    assert edited["text"] == "Язык бота: Русский. Выбери другой:"
    assert edited["reply_markup"]["inline_keyboard"][0][1]["text"] == "✓ Русский"
    assert service.store.ui(USER).lang == "ru"
    reply = await send("/channel Gilde", code="fr")
    assert reply["text"] == "Канал по умолчанию: Гильдия"

    reply = await send("/lang auto", code="fr")
    assert reply["text"] == "Langue du bot\u00a0: Français, comme dans Telegram."
    reply = await send("/lang Deutsch", code="fr")
    assert reply["text"] == "Sprache des Bots: Deutsch."
    reply = await send("/confirm ja")
    assert reply["text"] == "Vor dem Senden fragen: an" and service.store.get(USER).confirm


async def test_command_menu_in_every_language():
    session = RecordingSession()
    await set_commands(Bot("123456:" + "A" * 35, session=session))
    calls = [r[1] for r in session.requests if r[0] == "setMyCommands"]
    assert [c.get("language_code") for c in calls] == [None, "ru", "de", "es", "fr"]
    assert [c["command"] for c in calls[0]["commands"]] == list(COMMANDS)
    assert calls[1]["commands"][8] == {"command": "language", "description": "Язык бота"}


async def test_channels_and_status_show_what_the_game_sent(bot_env):
    service, _, send, _, _ = bot_env
    reply = await send("/channels", code="ru")
    assert reply["text"].startswith("Каналов из игры пока нет.")
    assert (await send("/status", code="en"))["text"].endswith("From the game: nothing yet. Channels and nicknames come from the VoiceForever addon through the agent.")
    service.set_game_context(USER, {
        "type": "context", "v": 1, "source": "live", "age_s": 125,
        "channels": [{"n": 1, "name": "Общий"}, {"n": 5, "name": "Рейдеры"}],
        "group": ["Лиана"], "guild": [{"name": "Арторн", "online": True}, {"name": "Спящий", "online": False}],
    })
    reply = await send("/channels", code="ru")
    assert reply["text"] == "Каналы чата в игре:\n1. Общий\n5. Рейдеры\n\nЧтобы написать в канал, скажи «в канал Рейдеры, …»."
    reply = await send("/status", code="ru")
    assert reply["text"].endswith("\nИз игры: каналы 2, гильдия 2 (в сети 1), друзья 0, группа 1. Обновлено 2 мин назад.")
