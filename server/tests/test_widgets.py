import json
import re
import zlib
from types import SimpleNamespace

import pytest
from aiogram.types import Update

from vf_server.access import LocalAccess
from vf_server.chat import Channel, ChatMessage
from vf_server.hub import Ack, AckStatus, AgentInfo
from vf_server.service import VoiceService
from vf_server.store import Store, UiState
from vf_server.tgwidgets import (
    MAX_DATA_BYTES,
    MAX_HTML_BYTES,
    MAX_HTML_COMPRESSED_BYTES,
    MAX_QUERY_BYTES,
    AnswerWidgetQuery,
    EditMessageWidgetData,
    SendWidget,
    WidgetQuery,
    WidgetQueryMiddleware,
)
from vf_server.i18n import LANGUAGES, t
from vf_server.widgets import STYLE_PRESETS, TEST_LINE, Card, WidgetUI, fit, load_html

USER = 42
WIDGETS = ("card", "panel", "setup")


class FakeHub:
    def __init__(self, access=None) -> None:
        self.access = access or LocalAccess({USER: "t" * 16})
        self.status = AckStatus.OK
        self.online = True
        self.sent: list[ChatMessage] = []
        self.accounts: list = []

    def info(self, user_id):
        return AgentInfo("0.3.0", "addon") if self.online else None

    def online_users(self):
        return [USER] if self.online else []

    async def send_account(self, user_id, account):
        self.accounts.append((user_id, dict(account)))

    async def send(self, user_id, msg, parts, *args):
        self.sent.append(msg)
        return Ack(self.status)


class FakeBot:
    def __init__(self) -> None:
        self.calls: list = []
        self._ids = iter(range(100, 10_000))

    async def __call__(self, method, request_timeout=None):
        self.calls.append(method)
        if isinstance(method, SendWidget):
            return SimpleNamespace(message_id=next(self._ids))
        return True

    async def pin_chat_message(self, **kw):
        self.calls.append(("pin", kw["message_id"]))

    async def unpin_chat_message(self, **kw):
        self.calls.append(("unpin", kw["message_id"]))

    async def delete_message(self, **kw):
        self.calls.append(("delete", kw["message_id"]))

    def of(self, kind):
        return [c for c in self.calls if isinstance(c, kind)]

    def states(self):
        return [json.loads(c.chat_data).get("s") for c in self.of(EditMessageWidgetData)]


def make_ui(router=SimpleNamespace(allow_transform=True)):
    hub = FakeHub()
    service = VoiceService(Store(":memory:"), hub, transcriber=None, router=router)
    return WidgetUI(service, download_url="https://example.com/agent"), hub


def tg_message(message_id=10, reply_to=None, user=USER):
    return SimpleNamespace(
        message_id=message_id,
        from_user=SimpleNamespace(id=user),
        chat=SimpleNamespace(id=user),
        reply_to_message=SimpleNamespace(message_id=reply_to) if reply_to else None,
    )


def query(data, user=USER):
    return WidgetQuery.model_validate({
        "id": "q1",
        "from": {"id": user, "is_bot": False, "first_name": "Kirill"},
        "chat": {"id": user, "type": "private"},
        "message_id": 101,
        "data": data,
    })


# ---------------------------------------------------------------------- html


@pytest.mark.parametrize("lang", LANGUAGES)
@pytest.mark.parametrize("name", WIDGETS)
def test_widget_fits_telegram_limits(name, lang):
    raw = load_html(name, lang).encode()
    assert len(raw) <= MAX_HTML_BYTES
    assert len(zlib.compress(raw, 9)) <= MAX_HTML_COMPRESSED_BYTES
    assert b"\0" not in raw


@pytest.mark.parametrize("name", WIDGETS)
def test_widget_uses_only_what_widgets_allow(name):
    html = load_html(name)
    for banned in ("<input", "<select", "<textarea", "<link", "<iframe", "<script src",
                   "@font-face", "@import", "text-shadow", "position:sticky", "overflow:auto",
                   "overflow:scroll", "vh;", "fetch(", "XMLHttpRequest", "WebSocket"):
        assert banned not in html, banned
    assert not re.search(r"""(src|href)\s*=\s*["']?https?:""", html)
    generic = {"serif", "sans-serif", "system-ui", "monospace"}
    for family in re.findall(r"font(?:-family)?:[^;}]*?(?:\d(?:px|em)(?:/[\d.]+)?\s+)([^;}]+)", html):
        assert {f.strip() for f in family.split(",")} <= generic, family


def test_longest_queries_fit_64_bytes():
    card_id = "A" * 8  # secrets.token_urlsafe(6)
    longest = f"s:{card_id}:{Channel.RAID_WARNING}:{'Ж' * 12}-{'Ж' * 4}"
    assert len(longest.encode()) <= MAX_QUERY_BYTES
    assert len(f"c:{Channel.RAID_WARNING}".encode()) <= MAX_QUERY_BYTES


# ---------------------------------------------------------------------- data


def test_fit_cuts_long_fields_in_order():
    data = {"i": "x", "o": "слово " * 300, "n": ["Арторн"] * 6, "x": "текст " * 80}
    raw = fit(data, shrink=("o", "n", "x"))
    assert len(raw.encode()) <= MAX_DATA_BYTES
    parsed = json.loads(raw)
    assert parsed["x"] == data["x"]  # the message itself survives
    assert parsed["o"].endswith("…") or parsed["o"] == ""


def test_card_data_fits_with_worst_case_text():
    card = Card(
        id="abcdefgh", user_id=USER, chat_id=USER,
        message=ChatMessage(Channel.WHISPER, "Арторн", "очень длинное сообщение " * 40),
        transcript="шепни Арторну " + "очень длинное сообщение " * 40,
        names=["Арторнабвгде"] * 6, error="Агент не смог ввести текст: " + "ошибка " * 50,
    )
    assert len(card.data().encode()) <= MAX_DATA_BYTES


def test_store_keeps_ui_state():
    store = Store(":memory:")
    assert store.ui(USER) == UiState()
    store.update_ui(USER, widgets=False, panel_msg_id=7, custom_style="как орк")
    assert store.ui(USER) == UiState(widgets=False, panel_msg_id=7, custom_style="как орк")


# ---------------------------------------------------------------------- card


async def test_card_with_confirmation_waits_for_tap():
    ui, hub = make_ui()
    ui.service.store.update(USER, known_names="Арторн, Лиана")
    bot = FakeBot()
    msg = ChatMessage(Channel.GUILD, "", "Отойду на 5 минут")
    assert await ui.show_card(bot, tg_message(), USER, msg, "в гильдию отойду", confirm=True)

    sent = bot.of(SendWidget)[0]
    data = json.loads(sent.chat_data)
    assert (data["s"], data["c"], data["n"]) == ("ask", "guild", ["Арторн", "Лиана"])
    assert sent.allow_queries and sent.reply_parameters.message_id == 10
    assert hub.sent == []

    await ui.on_query(bot, query(f"s:{data['i']}:whisper:Арторн"))
    assert bot.of(AnswerWidgetQuery)[0].data is None
    assert hub.sent == [ChatMessage(Channel.WHISPER, "Арторн", "Отойду на 5 минут")]
    assert bot.states() == ["busy", "sent"]
    assert json.loads(bot.of(EditMessageWidgetData)[-1].chat_data)["a"] > 0


async def test_card_without_confirmation_goes_straight_to_the_game():
    ui, hub = make_ui()
    bot = FakeBot()
    await ui.show_card(bot, tg_message(), USER, ChatMessage(Channel.SAY, "", "Привет"), "привет", False)
    assert json.loads(bot.of(SendWidget)[0].chat_data)["s"] == "busy"
    assert hub.sent == [ChatMessage(Channel.SAY, "", "Привет")]
    assert bot.states()[-1] == "sent"


async def test_failed_card_can_be_retried():
    ui, hub = make_ui()
    hub.status = AckStatus.NOT_FOCUSED
    bot = FakeBot()
    await ui.show_card(bot, tg_message(), USER, ChatMessage(Channel.GROUP, "", "Иду"), "иду", False)
    last = json.loads(bot.of(EditMessageWidgetData)[-1].chat_data)
    assert last["s"] == "fail" and last["e"] == t("en", "ack.not_focused")

    hub.status = AckStatus.OK
    await ui.on_query(bot, query(f"s:{last['i']}:group"))
    assert bot.states()[-1] == "sent" and len(hub.sent) == 2


async def test_cancel_undo_and_stale_cards():
    ui, hub = make_ui()
    bot = FakeBot()
    await ui.show_card(bot, tg_message(), USER, ChatMessage(Channel.SAY, "", "Ну"), "ну", True)
    card_id = json.loads(bot.of(SendWidget)[0].chat_data)["i"]

    await ui.on_query(bot, query(f"x:{card_id}"))
    await ui.on_query(bot, query(f"u:{card_id}"))
    assert bot.states() == ["cancel", "ask"]

    await ui.on_query(bot, query("s:unknown1:say"))
    assert bot.of(AnswerWidgetQuery)[-1].data == t("en", "widget.stale")
    await ui.on_query(bot, query(f"s:{card_id}:say", user=7))  # not in VF_USERS
    assert bot.of(AnswerWidgetQuery)[-1].data == t("en", "widget.denied")
    assert hub.sent == []


async def test_reply_to_card_changes_its_text():
    ui, _ = make_ui()
    bot = FakeBot()
    await ui.show_card(bot, tg_message(), USER, ChatMessage(Channel.SAY, "", "Првиет"), "првиет", True)
    card = ui.card_for_reply(tg_message(message_id=11, reply_to=100), USER)
    assert card is not None
    assert ui.card_for_reply(tg_message(message_id=11, reply_to=100, user=7), 7) is None

    await ui.correct_card(bot, card, ChatMessage(Channel.SAY, "", "Привет"), "привет")
    assert json.loads(bot.of(EditMessageWidgetData)[-1].chat_data)["x"] == "Привет"


# ---------------------------------------------------------------------- panel


async def test_panel_is_pinned_and_changes_settings():
    ui, hub = make_ui()
    store = ui.service.store
    bot = FakeBot()
    assert await ui.send_panel(bot, USER, USER)
    assert ("pin", 100) in bot.calls and store.ui(USER).panel_msg_id == 100
    data = json.loads(bot.of(SendWidget)[0].chat_data)
    assert (data["on"], data["v"], data["c"], data["tr"]) == (1, "0.3.0", "say", 1)

    await ui.on_query(bot, query("c:guild"))
    await ui.on_query(bot, query("f:1"))
    await ui.on_query(bot, query("y:short"))
    s = store.get(USER)
    assert (s.default_channel, s.confirm, s.rewrite_style) == (
        Channel.GUILD, True, STYLE_PRESETS["short"]
    )
    data = json.loads(bot.of(EditMessageWidgetData)[-1].chat_data)
    assert (data["c"], data["cf"], data["st"]) == ("guild", 1, "short")

    await ui.on_query(bot, query("c:whisper"))  # not a default channel
    await ui.on_query(bot, query("y:custom"))  # no custom style yet
    assert store.get(USER).default_channel is Channel.GUILD
    assert store.get(USER).rewrite_style == STYLE_PRESETS["short"]

    store.update_ui(USER, custom_style="как орк-шаман")
    await ui.on_query(bot, query("y:custom"))
    assert store.get(USER).rewrite_style == "как орк-шаман"

    assert await ui.send_panel(bot, USER, USER)  # /panel again replaces the pin
    assert ("unpin", 100) in bot.calls and store.ui(USER).panel_msg_id == 101


async def test_panel_style_is_locked_when_server_forbids_rewriting():
    ui, _ = make_ui(router=None)
    bot = FakeBot()
    await ui.send_panel(bot, USER, USER)
    await ui.on_query(bot, query("y:rp"))
    assert ui.service.store.get(USER).rewrite_style == ""
    assert json.loads(bot.of(SendWidget)[0].chat_data)["tr"] == 0


async def test_agent_changes_update_the_panel():
    ui, hub = make_ui()
    bot = FakeBot()
    await ui.send_panel(bot, USER, USER)
    await ui.check_agent(bot, USER)
    await ui.check_agent(bot, USER)  # nothing changed: no edit
    assert len(bot.of(EditMessageWidgetData)) == 1
    hub.online = False
    await ui.check_agent(bot, USER)
    assert json.loads(bot.of(EditMessageWidgetData)[-1].chat_data)["on"] == 0


# ---------------------------------------------------------------------- setup


async def test_setup_checklist_walkthrough():
    ui, hub = make_ui()
    store = ui.service.store
    bot = FakeBot()
    assert await ui.send_setup(bot, USER, USER)
    first = json.loads(bot.of(SendWidget)[0].chat_data)
    assert (first["on"], first["t"], first["dl"]) == (1, "", "https://example.com/agent")

    await ui.on_query(bot, query("t"))
    assert hub.sent == [ChatMessage(Channel.SAY, "", TEST_LINE)]
    assert [json.loads(c.chat_data)["t"] for c in bot.of(EditMessageWidgetData)] == ["busy", "ask"]

    await ui.on_query(bot, query("tn"))
    assert store.ui(USER).setup_test == "help"
    await ui.on_query(bot, query("ty"))
    assert store.ui(USER).setup_test == "ok"

    # The next phrase that reaches the game completes the list and pins the panel.
    await ui.show_card(bot, tg_message(), USER, ChatMessage(Channel.GUILD, "", "Всем привет!"), "", False)
    assert store.ui(USER).setup_voice == "[Guild] Всем привет!"
    assert store.ui(USER).panel_msg_id and ("pin", store.ui(USER).panel_msg_id) in bot.calls
    assert ui.setup_done(USER)


async def test_setup_test_reports_why_it_failed():
    ui, hub = make_ui()
    hub.status = AckStatus.OFFLINE
    bot = FakeBot()
    await ui.send_setup(bot, USER, USER)
    await ui.on_query(bot, query("t"))
    data = json.loads(bot.of(EditMessageWidgetData)[-1].chat_data)
    assert data["t"] == "fail" and data["e"] == t("en", "ack.offline")


# ---------------------------------------------------------------------- dispatcher


async def test_middleware_takes_widget_queries_only():
    seen, passed = [], []

    async def on_query(bot, q):
        seen.append(q.data)

    async def handler(event, data):
        passed.append(event.update_id)

    mw = WidgetQueryMiddleware(on_query)
    widget_update = Update.model_validate({
        "update_id": 1,
        "widget_query": {"id": "1", "from": {"id": USER, "is_bot": False, "first_name": "K"},
                         "data": "c:guild"},
    })
    plain_update = Update.model_validate({"update_id": 2})
    await mw(handler, widget_update, {"bot": None})
    await mw(handler, plain_update, {"bot": None})
    assert seen == ["c:guild"] and passed == [2]


async def test_dispatcher_routes_widget_queries_to_the_widgets():
    from aiogram import Bot, Dispatcher
    from aiogram.client.session.base import BaseSession

    from vf_server.bot import setup_bot

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

    hub = FakeHub()
    service = VoiceService(Store(":memory:"), hub, transcriber=None, router=None)
    dp = Dispatcher()
    allowed_updates = setup_bot(dp, service, {USER})
    assert "widget_query" in allowed_updates and "message" in allowed_updates

    session = RecordingSession()
    bot = Bot("123456:" + "A" * 35, session=session)
    await dp.feed_raw_update(bot, {
        "update_id": 1,
        "widget_query": {
            "id": "77", "from": {"id": USER, "is_bot": False, "first_name": "K"},
            "chat": {"id": USER, "type": "private"}, "message_id": 5, "data": "f:1",
        },
    })
    assert session.requests[0] == ("answerWidgetQuery", {"widget_query_id": "77"})
    assert service.store.get(USER).confirm is True
