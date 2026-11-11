"""Telegram HTML widgets: the message card, the pinned control panel and the setup checklist.

Each widget is one HTML file from vf_server/html. Its HTML is fixed once sent; the bot moves
a widget between states by editing its chat data (compact JSON, at most 1 KB). Taps come
back as widget queries of at most 64 bytes:

  card   s:<card>:<channel>[:<target>]  send, x:<card> cancel, u:<card> undo the cancel
  panel  c:<channel>  default channel, f:0|1 confirm before sending, y:<style> message style,
         l:<lang> the bot's language
  setup  t  send the test line, ty the player saw it in the game, tn they did not

The card answers a query with an error text when it cannot act on it (an empty answer
means success); the other widgets ignore answers and wait for their chat data to change.

The widgets' text comes from locales/<lang>.json: the HTML is sent with the player's
language put in place of `L=/*L*/{}`. As the HTML cannot change after sending, a new
language reaches the cards and the setup checklist sent from then on, and the pinned
panel is sent again.
"""

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Literal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import Message, ReplyParameters

from .chat import Channel, ChatMessage
from .hub import AckStatus
from .i18n import LANGUAGES, SOURCE, from_code, strings, t
from .service import Delivery, VoiceService
from .tgwidgets import (
    MAX_DATA_BYTES,
    AnswerWidgetQuery,
    EditMessageWidgetData,
    SendWidget,
    WidgetQuery,
)

log = logging.getLogger(__name__)

WIDGET_WIDTH = 340  # layout hint; the bubble is 200-600 px wide
TEST_LINE = "VoiceForever test"
DEFAULT_CHANNELS = frozenset(Channel) - {Channel.WHISPER, Channel.CHANNEL, Channel.REPLY}

# Why a phrase did not reach the game, as keys of the bot's strings.
ACK_KEYS: frozendict[AckStatus, str] = frozendict({
    AckStatus.OFFLINE: "ack.offline",
    AckStatus.NOT_FOCUSED: "ack.not_focused",
    AckStatus.TIMEOUT: "ack.timeout",
    AckStatus.ERROR: "ack.error",
})

# Message styles one tap away on the panel; any other /style text is the player's own.
# These are instructions for the LLM, not text for people, so they stay as they are.
STYLE_PRESETS: frozendict[str, str] = frozendict({
    "short": "Коротко и по делу, без лишних слов.",
    "rp": "Как реплика ролевого персонажа в фэнтезийном мире, без современного сленга.",
})

# Which strings each widget gets: its own (without the prefix) and the shared ones.
WIDGET_STRINGS: frozendict[str, tuple[str, ...]] = frozendict({
    "card": ("card.", "chan."),
    "panel": ("panel.", "chan.", "mode."),
    "setup": ("setup.", "mode."),
})

type CardState = Literal["ask", "busy", "sent", "fail", "cancel"]
type EditResult = Literal["ok", "gone", "error"]


@cache
def load_html(name: str, lang: str = SOURCE) -> str:
    """A widget's HTML with its strings in `lang`."""
    html = (resources.files("vf_server") / "html" / f"{name}.html").read_text(encoding="utf-8")
    own = f"{name}."
    table: dict[str, object] = {
        key.removeprefix(own): text
        for key, text in strings(lang).items()
        if key.startswith(WIDGET_STRINGS[name])
    }
    if name == "panel":
        table["langs"] = [[code, title] for code, title in LANGUAGES.items()]
    # "</" inside a string would end the script element early.
    return html.replace("L=/*L*/{}", "L=" + _dumps(table).replace("</", "<\\/"), 1)


def fit(data: dict[str, object], shrink: tuple[str, ...] = ()) -> str:
    """Compact JSON of at most MAX_DATA_BYTES, cutting the `shrink` fields in that order."""
    data = dict(data)
    while len(raw := _dumps(data).encode()) > MAX_DATA_BYTES:
        over = len(raw) - MAX_DATA_BYTES
        for key in shrink:
            match data.get(key):
                case str(text) if text:
                    keep = max(0, len(text) - max(over // 2 + 2, len(text) // 4))
                    data[key] = text[:keep].rstrip() + "…" if keep else ""
                    break
                case list(items) if items:
                    data[key] = items[:-1]
                    break
        else:
            raise ValueError("widget data does not fit into 1 KB")
    return _dumps(data)


def _dumps(data: dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def failure_text(delivery: Delivery, lang: str) -> str:
    base = t(lang, ACK_KEYS.get(delivery.ack.status, ACK_KEYS[AckStatus.ERROR]))
    return f"{base}: {delivery.ack.detail}" if delivery.ack.detail else base


@dataclass(slots=True)
class Card:
    """One phrase on its way to the game, shown as a card under the voice message."""

    id: str
    user_id: int
    chat_id: int
    message: ChatMessage
    transcript: str
    names: list[str] = field(default_factory=list)
    state: CardState = "ask"
    message_id: int = 0
    at: int = 0  # when it reached the game, unix time (the widget shows local time)
    error: str = ""

    def data(self) -> str:
        return fit(
            {
                "i": self.id,
                "s": self.state,
                "c": str(self.message.channel),
                "t": self.message.target,
                "x": self.message.text,
                "o": self.transcript,
                "n": self.names,
                "a": self.at,
                "e": self.error,
            },
            shrink=("o", "n", "e", "x"),
        )


class CardStore:
    """Recent cards in memory. After a restart old cards answer that they are stale."""

    def __init__(self, limit: int = 200) -> None:
        self._items: OrderedDict[str, Card] = OrderedDict()
        self._limit = limit

    def new_id(self) -> str:
        return secrets.token_urlsafe(6)  # random, so ids from before a restart never match

    def add(self, card: Card) -> Card:
        self._items[card.id] = card
        while len(self._items) > self._limit:
            self._items.popitem(last=False)
        return card

    def get(self, card_id: str) -> Card | None:
        return self._items.get(card_id)

    def by_message(self, chat_id: int, message_id: int) -> Card | None:
        for card in reversed(self._items.values()):
            if card.chat_id == chat_id and card.message_id == message_id:
                return card
        return None


class WidgetUI:
    def __init__(
        self,
        service: VoiceService,
        users: set[int],
        download_url: str = "",
        poll_s: float = 3.0,
    ) -> None:
        self.service = service
        self.store = service.store
        self.users = users
        self.download_url = download_url
        self.poll_s = poll_s
        self.cards = CardStore()
        self._agents: dict[int, tuple[str, str] | None] = {}  # last seen (version, mode)
        self._online_since: dict[int, int] = {}
        self._test_error: dict[int, str] = {}
        self._task: asyncio.Task[None] | None = None

    def enabled(self, user_id: int) -> bool:
        return self.store.ui(user_id).widgets

    def lang(self, uid: int, telegram_code: str | None = None) -> str:
        """The player's language: the one they picked, else their Telegram app's, else English.

        `telegram_code` is the language_code of an update from them; it is remembered, so
        texts sent later without an update at hand (a widget refresh) use it too.
        """
        ui = self.store.ui(uid)
        if telegram_code and telegram_code != ui.tg_lang:
            ui = self.store.update_ui(uid, tg_lang=telegram_code)
        if ui.lang in LANGUAGES:
            return ui.lang
        return from_code(ui.tg_lang) or SOURCE

    def telegram_lang(self, uid: int) -> str:
        """The language "as in Telegram" would give."""
        return from_code(self.store.ui(uid).tg_lang) or SOURCE

    async def set_lang(self, bot: Bot, uid: int, lang: str) -> str:
        """Keep the player's pick ("" = as in Telegram) and resend the panel in it."""
        before = self.lang(uid)
        self.store.update_ui(uid, lang=lang if lang in LANGUAGES else "")
        after = self.lang(uid)
        if after != before:
            await self.resend_panel(bot, uid)
        return after

    # ------------------------------------------------------------------ card
    async def show_card(
        self, bot: Bot, message: Message, msg: ChatMessage, transcript: str, confirm: bool
    ) -> bool:
        """Reply with a card. Without confirmation it is sent to the game right away.
        False if Telegram refused the widget, so the caller can fall back to text."""
        uid = message.from_user.id
        card = Card(
            id=self.cards.new_id(),
            user_id=uid,
            chat_id=message.chat.id,
            message=msg,
            transcript=transcript,
            names=[n[:24] for n in self.service.context(uid).known_names[:6]],
            state="ask" if confirm else "busy",
        )
        try:
            sent = await bot(SendWidget(
                chat_id=message.chat.id,
                html=load_html("card", self.lang(uid)),
                width=WIDGET_WIDTH,
                chat_data=card.data(),
                allow_queries=True,
                reply_parameters=ReplyParameters(message_id=message.message_id),
            ))
        except TelegramAPIError as e:
            log.warning("sendWidget failed, falling back to text: %s", e)
            return False
        card.message_id = sent.message_id
        self.cards.add(card)
        if not confirm:
            await self._deliver(bot, card)
        return True

    def card_for_reply(self, message: Message) -> Card | None:
        """The card a message replies to, if its text can still be changed."""
        reply = message.reply_to_message
        if reply is None or message.from_user is None:
            return None
        card = self.cards.by_message(message.chat.id, reply.message_id)
        if card is None or card.user_id != message.from_user.id:
            return None
        return card if card.state in ("ask", "fail", "cancel") else None

    async def correct_card(
        self, bot: Bot, card: Card, msg: ChatMessage, transcript: str
    ) -> None:
        card.message, card.transcript, card.state, card.error = msg, transcript, "ask", ""
        await self._edit(bot, card.chat_id, card.message_id, card.data())

    async def _deliver(self, bot: Bot, card: Card) -> None:
        card.state, card.error = "busy", ""
        await self._edit(bot, card.chat_id, card.message_id, card.data())
        delivery = await self.service.deliver(card.user_id, card.message)
        card.message = delivery.message
        if delivery.ack.ok:
            card.state, card.at = "sent", int(time.time())
        else:
            card.state, card.error = "fail", failure_text(delivery, self.lang(card.user_id))
        await self._edit(bot, card.chat_id, card.message_id, card.data())
        await self.note_delivery(bot, card.user_id, delivery)

    async def _card_query(self, bot: Bot, q: WidgetQuery, uid: int, args: list[str]) -> None:
        action, card_id, *rest = args
        card = self.cards.get(card_id)
        if card is None or card.user_id != uid:
            await self._answer(bot, q, t(self.lang(uid), "widget.stale"))
            return
        match action, rest:
            case "s", [channel, *target] if channel in Channel:
                if card.state == "busy":
                    await self._answer(bot, q)
                    return
                card.message = ChatMessage(Channel(channel), "".join(target), card.message.text)
                await self._answer(bot, q)
                await self._deliver(bot, card)
            case "x", []:
                await self._answer(bot, q)
                if card.state != "busy":
                    card.state = "cancel"
                await self._edit(bot, card.chat_id, card.message_id, card.data())
            case "u", []:
                await self._answer(bot, q)
                if card.state == "cancel":
                    card.state = "ask"
                await self._edit(bot, card.chat_id, card.message_id, card.data())
            case _:
                await self._answer(bot, q, t(self.lang(uid), "widget.unknown"))

    # ------------------------------------------------------------------ panel
    def panel_data(self, uid: int) -> str:
        s = self.store.get(uid)
        ui = self.store.ui(uid)
        info = self.service.hub.info(uid)
        router = self.service.router
        if not s.rewrite_style:
            style = "off"
        else:
            style = next((k for k, v in STYLE_PRESETS.items() if v == s.rewrite_style), "custom")
        custom = ui.custom_style or (s.rewrite_style if style == "custom" else "")
        return fit(
            {
                "on": int(info is not None),
                "v": info.version if info else "",
                "md": info.mode if info else "",
                "since": self._online_since.get(uid, 0) if info else 0,
                "c": str(s.default_channel),
                "cf": int(s.confirm),
                "st": style,
                "cs": custom,
                "tr": int(router is not None and router.allow_transform),
                "n": s.known_names,
                "lg": self.lang(uid),
            },
            shrink=("n", "cs"),
        )

    async def send_panel(self, bot: Bot, chat_id: int, uid: int) -> bool:
        """Send the control panel and pin it in place of the previous one."""
        old = self.store.ui(uid).panel_msg_id
        try:
            sent = await bot(SendWidget(
                chat_id=chat_id,
                html=load_html("panel", self.lang(uid)),
                width=WIDGET_WIDTH,
                chat_data=self.panel_data(uid),
                allow_queries=True,
                disable_notification=True,
            ))
        except TelegramAPIError as e:
            log.warning("sendWidget (panel) failed: %s", e)
            return False
        self.store.update_ui(uid, panel_msg_id=sent.message_id)
        if old:
            with contextlib.suppress(TelegramAPIError):
                await bot.unpin_chat_message(chat_id=chat_id, message_id=old)
        with contextlib.suppress(TelegramAPIError):
            await bot.pin_chat_message(
                chat_id=chat_id, message_id=sent.message_id, disable_notification=True
            )
        return True

    async def refresh_panel(self, bot: Bot, uid: int) -> None:
        if not (msg_id := self.store.ui(uid).panel_msg_id):
            return
        if await self._edit(bot, uid, msg_id, self.panel_data(uid)) == "gone":
            self.store.update_ui(uid, panel_msg_id=0)

    async def resend_panel(self, bot: Bot, uid: int) -> None:
        """Replace the pinned panel with a new one, for a new language."""
        old = self.store.ui(uid).panel_msg_id
        if not old or not self.enabled(uid) or not await self.send_panel(bot, uid, uid):
            return
        try:
            await bot.delete_message(chat_id=uid, message_id=old)
        except TelegramAPIError:  # too old to delete: let it show the new choice
            await self._edit(bot, uid, old, self.panel_data(uid))

    async def _panel_query(self, bot: Bot, q: WidgetQuery, uid: int, args: list[str]) -> None:
        await self._answer(bot, q)
        router = self.service.router
        match args:
            case ["l", lang] if lang in LANGUAGES:
                if lang != self.lang(uid):
                    await self.set_lang(bot, uid, lang)
                    return
                self.store.update_ui(uid, lang=lang)
            case ["c", channel] if channel in Channel and Channel(channel) in DEFAULT_CHANNELS:
                self.store.update(uid, default_channel=Channel(channel))
            case ["f", "0" | "1" as flag]:
                self.store.update(uid, confirm=flag == "1")
            case ["y", style] if router is not None and router.allow_transform:
                match style:
                    case "off":
                        self.store.update(uid, rewrite_style="")
                    case "custom" if custom := self.store.ui(uid).custom_style:
                        self.store.update(uid, rewrite_style=custom)
                    case preset if preset in STYLE_PRESETS:
                        self.store.update(uid, rewrite_style=STYLE_PRESETS[preset])
        await self.refresh_panel(bot, uid)

    # ------------------------------------------------------------------ setup
    def setup_data(self, uid: int) -> str:
        ui = self.store.ui(uid)
        info = self.service.hub.info(uid)
        return fit(
            {
                "on": int(info is not None),
                "v": info.version if info else "",
                "md": info.mode if info else "",
                "t": ui.setup_test,
                "e": self._test_error.get(uid, "") if ui.setup_test == "fail" else "",
                "vc": ui.setup_voice,
                "dl": self.download_url,
            },
            shrink=("vc", "e"),
        )

    async def send_setup(self, bot: Bot, chat_id: int, uid: int) -> bool:
        if self.store.ui(uid).setup_test == "busy":  # a test lost in a restart
            self.store.update_ui(uid, setup_test="")
        try:
            sent = await bot(SendWidget(
                chat_id=chat_id,
                html=load_html("setup", self.lang(uid)),
                width=WIDGET_WIDTH,
                chat_data=self.setup_data(uid),
                allow_queries=True,
            ))
        except TelegramAPIError as e:
            log.warning("sendWidget (setup) failed: %s", e)
            return False
        self.store.update_ui(uid, setup_msg_id=sent.message_id)
        return True

    def setup_done(self, uid: int) -> bool:
        ui = self.store.ui(uid)
        return ui.setup_test == "ok" and bool(ui.setup_voice)

    async def refresh_setup(self, bot: Bot, uid: int) -> None:
        if not (msg_id := self.store.ui(uid).setup_msg_id):
            return
        if await self._edit(bot, uid, msg_id, self.setup_data(uid)) == "gone":
            self.store.update_ui(uid, setup_msg_id=0)

    async def note_delivery(self, bot: Bot, uid: int, delivery: Delivery) -> None:
        """The first phrase that reaches the game after the test completes the setup."""
        ui = self.store.ui(uid)
        if not delivery.ack.ok or ui.setup_test != "ok" or ui.setup_voice or not ui.setup_msg_id:
            return
        msg = delivery.message
        label = msg.label(self.lang(uid))
        self.store.update_ui(uid, setup_voice=f"[{label}] {msg.text}"[:200])
        await self.refresh_setup(bot, uid)
        if not ui.panel_msg_id:
            await self.send_panel(bot, uid, uid)

    async def _setup_query(self, bot: Bot, q: WidgetQuery, uid: int, args: list[str]) -> None:
        await self._answer(bot, q)
        match args:
            case ["t"] if self.store.ui(uid).setup_test != "busy":
                self.store.update_ui(uid, setup_test="busy")
                await self.refresh_setup(bot, uid)
                delivery = await self.service.deliver(uid, ChatMessage(Channel.SAY, "", TEST_LINE))
                if delivery.ack.ok:
                    self.store.update_ui(uid, setup_test="ask")
                else:
                    self._test_error[uid] = failure_text(delivery, self.lang(uid))
                    self.store.update_ui(uid, setup_test="fail")
            case ["ty"]:
                self.store.update_ui(uid, setup_test="ok")
            case ["tn"]:
                self.store.update_ui(uid, setup_test="help")
        await self.refresh_setup(bot, uid)

    # ------------------------------------------------------------------ queries
    async def on_query(self, bot: Bot, q: WidgetQuery) -> None:
        uid = q.from_user.id
        if uid not in self.users:
            lang = from_code(q.from_user.language_code) or SOURCE
            await self._answer(bot, q, t(lang, "widget.denied"))
            return
        lang = self.lang(uid, q.from_user.language_code)
        args = q.data.split(":", 3)
        try:
            match args[0]:
                case "s" | "x" | "u" if len(args) >= 2:
                    await self._card_query(bot, q, uid, args)
                case "c" | "f" | "y" | "l":
                    await self._panel_query(bot, q, uid, args)
                case "t" | "ty" | "tn":
                    await self._setup_query(bot, q, uid, args)
                case _:
                    await self._answer(bot, q, t(lang, "widget.unknown"))
        except Exception:
            log.exception("widget query %r from user %s failed", q.data, uid)

    # ------------------------------------------------------------------ agent status
    def start(self, bot: Bot) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.watch(bot))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def watch(self, bot: Bot) -> None:
        """Keep the panel and the setup checklist in step with the agent's connection.
        The hub is polled (a few users, every few seconds) instead of calling back."""
        while True:
            for uid in self.users:
                try:
                    await self.check_agent(bot, uid)
                except Exception:
                    log.exception("updating widgets for user %s failed", uid)
            await asyncio.sleep(self.poll_s)

    async def check_agent(self, bot: Bot, uid: int) -> None:
        info = self.service.hub.info(uid)
        key = (info.version, info.mode) if info else None
        if uid in self._agents and self._agents[uid] == key:
            return
        self._agents[uid] = key
        if key is None:
            self._online_since.pop(uid, None)
        else:
            self._online_since.setdefault(uid, int(time.time()))
        await self.refresh_panel(bot, uid)
        if not self.setup_done(uid):
            await self.refresh_setup(bot, uid)

    # ------------------------------------------------------------------ telegram
    async def _answer(self, bot: Bot, q: WidgetQuery, error: str = "") -> None:
        try:
            await bot(AnswerWidgetQuery(widget_query_id=q.id, data=error or None))
        except TelegramAPIError as e:
            log.info("answerWidgetQuery failed: %s", e)

    async def _edit(self, bot: Bot, chat_id: int, message_id: int, data: str) -> EditResult:
        try:
            await bot(EditMessageWidgetData(chat_id=chat_id, message_id=message_id, chat_data=data))
        except TelegramBadRequest as e:
            text = str(e).lower()
            if "not modified" in text:
                return "ok"
            log.info("editMessageWidgetData failed: %s", e)
            return "gone" if "not found" in text or "message_id_invalid" in text else "error"
        except TelegramAPIError as e:
            log.warning("editMessageWidgetData failed: %s", e)
            return "error"
        return "ok"
