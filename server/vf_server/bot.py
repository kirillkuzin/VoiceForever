"""Telegram bot: voice in, chat message out.

Replies come in the player's language (see i18n): the one they picked with /language or on
the panel, else the language of their Telegram app.
"""

import contextlib
import io
import itertools
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from functools import cache
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    User,
)

from .chat import Channel, ChatMessage, channel_name
from .i18n import LANGUAGES, SOURCE, from_code, strings, t
from .router import Decision, RouteResult
from .service import Delivery, VoiceService
from .stt import TranscriptionError
from .tgwidgets import WIDGET_UPDATE_TYPES, WidgetQueryMiddleware
from .widgets import DEFAULT_CHANNELS, STYLE_PRESETS, WidgetUI, failure_text

log = logging.getLogger(__name__)

# Russian words players use for channels; the channel names of every language work too.
CHANNEL_ALIASES: frozendict[str, Channel] = frozendict({
    "пати": Channel.GROUP, "группа": Channel.GROUP, "группу": Channel.GROUP,
    "рейд": Channel.RAID, "инст": Channel.INSTANCE, "данж": Channel.INSTANCE,
    "гильдия": Channel.GUILD, "гильдию": Channel.GUILD, "ги": Channel.GUILD,
    "офицеры": Channel.OFFICER, "сказать": Channel.SAY, "крик": Channel.YELL,
})
QUICK_CHANNELS = (Channel.SAY, Channel.GROUP, Channel.GUILD)

# on/off arguments of /confirm, /widgets and /style, in the bot's languages.
ON_WORDS = frozenset({"on", "1", "yes", "вкл", "да", "ja", "an", "ein", "sí", "si", "oui"})
OFF_WORDS = frozenset({"off", "0", "no", "выкл", "нет", "nein", "aus", "non"})

# /language arguments besides the codes: "as in Telegram" and the languages' names.
AUTO_WORDS = frozenset({"auto", "telegram", "авто"})
LANGUAGE_NAMES: frozendict[str, str] = frozendict(
    {name.lower(): code for code, name in LANGUAGES.items()}
    | {"english": "en", "russian": "ru", "german": "de", "spanish": "es", "french": "fr"}
)

# The command menu, in this order; the descriptions are cmd.<command> in the locales.
COMMANDS = ("start", "status", "panel", "channel", "confirm", "style", "names", "language",
            "widgets", "help")


class PendingMessages:
    """Messages waiting for a confirm tap. Small and in memory on purpose."""

    def __init__(self, limit: int = 100) -> None:
        self._items: OrderedDict[str, tuple[int, ChatMessage]] = OrderedDict()
        self._ids = itertools.count(1)
        self._limit = limit

    def add(self, user_id: int, msg: ChatMessage) -> str:
        pid = str(next(self._ids))
        self._items[pid] = (user_id, msg)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)
        return pid

    def get(self, pid: str) -> tuple[int, ChatMessage] | None:
        return self._items.get(pid)

    def pop(self, pid: str) -> tuple[int, ChatMessage] | None:
        return self._items.pop(pid, None)

    def replace(self, pid: str, msg: ChatMessage) -> None:
        if pid in self._items:
            self._items[pid] = (self._items[pid][0], msg)


@cache
def _channel_names() -> dict[str, Channel]:
    names: dict[str, Channel] = {}
    for lang in LANGUAGES:
        for channel in Channel:  # GROUP before PARTY, so a shared name means GROUP
            if channel in DEFAULT_CHANNELS:
                names.setdefault(channel_name(channel, lang).lower(), channel)
    return names


def parse_channel(arg: str) -> Channel | None:
    """The default channel a /channel argument names: "пати", "guild", "Gilde", "raid"…"""
    arg = arg.strip().lower()
    channel = CHANNEL_ALIASES.get(arg) or (Channel(arg) if arg in Channel else None)
    channel = channel or _channel_names().get(arg)
    return channel if channel in DEFAULT_CHANNELS else None


def parse_language(arg: str) -> str | None:
    """The /language argument as a code, "" for "as in Telegram", None if unknown."""
    arg = arg.strip().lower()
    if arg in AUTO_WORDS:
        return ""
    return from_code(arg) or LANGUAGE_NAMES.get(arg)


def on_off(lang: str, on: bool) -> str:
    return t(lang, "bot.on" if on else "bot.off")


def mode_name(mode: str, lang: str) -> str:
    """How the agent types: "addon mode" or "plain chat"; an unknown mode as it came."""
    key = f"mode.{mode}"
    return t(lang, key) if key in strings(lang) else mode


def confirm_keyboard(pid: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t(lang, "bot.send"), callback_data=f"vf:send:{pid}"),
            InlineKeyboardButton(text=t(lang, "bot.cancel"), callback_data=f"vf:cancel:{pid}"),
        ],
        [
            InlineKeyboardButton(text=channel_name(ch, lang), callback_data=f"vf:ch:{pid}:{ch}")
            for ch in QUICK_CHANNELS
        ],
    ])


def language_keyboard(lang: str, picked: str, telegram: str) -> InlineKeyboardMarkup:
    """The languages, three to a row, and "as in Telegram"; the current choice is ticked."""
    def button(text: str, on: bool, code: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=f"✓ {text}" if on else text, callback_data=f"lang:{code}")

    codes = list(LANGUAGES)
    rows = [
        [button(LANGUAGES[code], code == picked, code) for code in codes[i:i + 3]]
        for i in range(0, len(codes), 3)
    ]
    auto = t(lang, "bot.language_button_auto", name=LANGUAGES[telegram])
    rows.append([button(auto, not picked, "auto")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def describe(delivery: Delivery, lang: str) -> str:
    msg = delivery.message
    label = msg.label(lang)
    if delivery.ack.ok:
        count = len(delivery.parts)
        suffix = " " + t(lang, "bot.parts", count=count) if count > 1 else ""
        return f"✓ [{label}] {msg.text}{suffix}"
    return f"⚠️ {failure_text(delivery, lang)}\n[{label}] {msg.text}"


def note_text(result: RouteResult, lang: str) -> str:
    """Why the simple rules picked the channel, on a line of its own; "" when the LLM did."""
    if not result.note:
        return ""
    return f"\n({t(lang, f'note.{result.note}', code=result.note_code)})"


class LanguageMiddleware(BaseMiddleware):
    """Passes the player's language to handlers as `lang`, noting their Telegram app's."""

    def __init__(self, ui: WidgetUI, allowed: set[int]) -> None:
        self.ui = ui
        self.allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            data["lang"] = SOURCE
        elif user.id in self.allowed:
            data["lang"] = self.ui.lang(user.id, user.language_code)
        else:  # nothing is stored for strangers
            data["lang"] = from_code(user.language_code) or SOURCE
        return await handler(event, data)


def build_router(service: VoiceService, allowed: set[int], ui: WidgetUI) -> Router:
    router = Router()
    pending = PendingMessages()
    allowed_filter = F.from_user.id.in_(allowed)
    languages = LanguageMiddleware(ui, allowed)
    router.message.middleware(languages)
    router.callback_query.middleware(languages)

    def language_line(uid: int, lang: str) -> str:
        if ui.store.ui(uid).lang in LANGUAGES:
            return LANGUAGES[lang]
        return t(lang, "bot.language_auto", name=LANGUAGES[lang])

    def language_menu(uid: int, lang: str) -> tuple[str, InlineKeyboardMarkup]:
        text = t(lang, "bot.language", name=language_line(uid, lang))
        return text, language_keyboard(lang, ui.store.ui(uid).lang, ui.telegram_lang(uid))

    @router.message(Command("start", "help"))
    async def start(message: Message, bot: Bot, command: CommandObject, lang: str) -> None:
        user = message.from_user
        if user is None or user.id not in allowed:
            uid = user.id if user else "?"
            await message.answer(t(lang, "bot.not_allowed", id=uid))
            return
        if command.command == "start" and ui.enabled(user.id):
            if await ui.send_setup(bot, message.chat.id, user.id):
                return
        await message.answer(t(lang, "bot.help"))

    @router.message(Command("panel"), allowed_filter)
    async def panel(message: Message, bot: Bot, lang: str) -> None:
        if not ui.enabled(message.from_user.id):
            await message.answer(t(lang, "bot.widgets_disabled"))
        elif not await ui.send_panel(bot, message.chat.id, message.from_user.id):
            await message.answer(t(lang, "bot.panel_refused"))

    @router.message(Command("widgets"), allowed_filter)
    async def set_widgets(message: Message, command: CommandObject, lang: str) -> None:
        on = (command.args or "").strip().lower() not in OFF_WORDS
        service.store.update_ui(message.from_user.id, widgets=on)
        await message.answer(t(lang, "bot.widgets_on" if on else "bot.widgets_off"))

    @router.message(Command("status"), allowed_filter)
    async def status(message: Message, lang: str) -> None:
        uid = message.from_user.id
        s = service.store.get(uid)
        match service.hub.info(uid):
            case None:
                agent = t(lang, "bot.agent_offline")
            case info:
                agent = t(lang, "bot.agent_online", version=info.version, mode=mode_name(info.mode, lang))
        await message.answer(t(
            lang, "bot.status",
            agent=agent,
            channel=channel_name(s.default_channel, lang),
            confirm=on_off(lang, s.confirm),
            style=s.rewrite_style or t(lang, "bot.off"),
            names=s.known_names or "—",
            language=language_line(uid, lang),
        ))

    @router.message(Command("channel"), allowed_filter)
    async def set_channel(message: Message, command: CommandObject, bot: Bot, lang: str) -> None:
        if (channel := parse_channel(command.args or "")) is None:
            await message.answer(t(lang, "bot.channel_usage"))
            return
        service.store.update(message.from_user.id, default_channel=channel)
        await message.answer(t(lang, "bot.channel_set", channel=channel_name(channel, lang)))
        await ui.refresh_panel(bot, message.from_user.id)

    @router.message(Command("confirm"), allowed_filter)
    async def set_confirm(message: Message, command: CommandObject, bot: Bot, lang: str) -> None:
        on = (command.args or "").strip().lower() in ON_WORDS
        service.store.update(message.from_user.id, confirm=on)
        await message.answer(t(lang, "bot.confirm_set", state=on_off(lang, on)))
        await ui.refresh_panel(bot, message.from_user.id)

    @router.message(Command("style"), allowed_filter)
    async def set_style(message: Message, command: CommandObject, bot: Bot, lang: str) -> None:
        if service.router is None or not service.router.allow_transform:
            await message.answer(t(lang, "bot.style_disabled"))
            return
        arg = (command.args or "").strip()
        style = "" if not arg or arg.lower() in OFF_WORDS else arg[:300]
        service.store.update(message.from_user.id, rewrite_style=style)
        if style and style not in STYLE_PRESETS.values():
            service.store.update_ui(message.from_user.id, custom_style=style)
        await message.answer(t(lang, "bot.style_set", style=style) if style else t(lang, "bot.style_off"))
        await ui.refresh_panel(bot, message.from_user.id)

    @router.message(Command("names"), allowed_filter)
    async def set_names(message: Message, command: CommandObject, bot: Bot, lang: str) -> None:
        names = ", ".join(n.strip() for n in (command.args or "").split(",") if n.strip())
        service.store.update(message.from_user.id, known_names=names[:500])
        await message.answer(t(lang, "bot.names_set", names=names or "—"))
        await ui.refresh_panel(bot, message.from_user.id)

    @router.message(Command("language", "lang"), allowed_filter)
    async def set_language(message: Message, command: CommandObject, bot: Bot, lang: str) -> None:
        uid = message.from_user.id
        if (picked := parse_language(command.args or "")) is not None:
            lang = await ui.set_lang(bot, uid, picked)
            await message.answer(t(lang, "bot.language_set", name=language_line(uid, lang)))
            return
        text, keyboard = language_menu(uid, lang)
        await message.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("lang:"), allowed_filter)
    async def on_language(query: CallbackQuery, bot: Bot) -> None:
        code = (query.data or "").removeprefix("lang:")
        if code == "auto" or code in LANGUAGES:
            uid = query.from_user.id
            lang = await ui.set_lang(bot, uid, "" if code == "auto" else code)
            text, keyboard = language_menu(uid, lang)
            if isinstance(query.message, Message):
                with contextlib.suppress(TelegramBadRequest):  # "message is not modified"
                    await query.message.edit_text(text, reply_markup=keyboard)
        await query.answer()

    @router.message(allowed_filter, F.voice | F.audio | F.video_note)
    async def on_voice(message: Message, bot: Bot, lang: str) -> None:
        match message:
            case Message(voice=voice) if voice:
                media, filename = voice, "voice.ogg"
            case Message(video_note=note) if note:
                media, filename = note, "note.mp4"
            case Message(audio=audio) if audio:
                media, filename = audio, audio.file_name or "audio.mp3"
            case _:
                return
        buf = io.BytesIO()
        await bot.download(media, destination=buf)
        try:
            transcript = await service.transcribe(message.from_user.id, buf.getvalue(), filename)
        except TranscriptionError as e:
            log.error("STT failed: %s", e)
            await message.reply(t(lang, "bot.stt_failed"))
            return
        if not transcript:
            await message.reply(t(lang, "bot.heard_nothing"))
            return
        await handle_phrase(message, transcript, bot, lang)

    @router.message(allowed_filter, F.text, ~F.text.startswith("/"))
    async def on_text(message: Message, bot: Bot, lang: str) -> None:
        await handle_phrase(message, message.text, bot, lang)

    async def handle_phrase(message: Message, transcript: str, bot: Bot, lang: str) -> None:
        uid = message.from_user.id
        widgets = ui.enabled(uid)
        card = ui.card_for_reply(message) if widgets else None
        result = await service.route(uid, transcript)
        note = note_text(result, lang)
        if result.decision is Decision.SKIP:
            await message.reply(t(lang, "bot.skipped", text=transcript))
            return
        if card is not None:  # a reply to a card replaces the card's text
            await ui.correct_card(bot, card, result.message, transcript)
            return
        confirm = service.store.get(uid).confirm
        if widgets and await ui.show_card(bot, message, result.message, transcript, confirm):
            return
        if confirm:
            pid = pending.add(uid, result.message)
            await message.reply(
                f"[{result.message.label(lang)}] {result.message.text}{note}",
                reply_markup=confirm_keyboard(pid, lang),
            )
            return
        delivery = await service.deliver(uid, result.message)
        await message.reply(describe(delivery, lang) + note)
        await ui.note_delivery(bot, uid, delivery)

    @router.callback_query(F.data.startswith("vf:"), allowed_filter)
    async def on_button(query: CallbackQuery, bot: Bot, lang: str) -> None:
        match (query.data or "").split(":"):
            case ["vf", action, pid, *rest]:
                pass
            case _:
                await query.answer()
                return
        item = pending.get(pid)
        if item is None or item[0] != query.from_user.id:
            await query.answer(t(lang, "bot.stale"))
            return
        _, msg = item
        match action, rest:
            case "cancel", _:
                pending.pop(pid)
                await query.message.edit_text(t(lang, "bot.cancelled", text=msg.text))
            case "ch", [channel] if channel in Channel:
                msg = ChatMessage(Channel(channel), "", msg.text)
                pending.replace(pid, msg)
                await query.message.edit_text(
                    f"[{msg.label(lang)}] {msg.text}", reply_markup=confirm_keyboard(pid, lang)
                )
            case "send", _:
                pending.pop(pid)
                delivery = await service.deliver(query.from_user.id, msg)
                await query.message.edit_text(describe(delivery, lang))
                await ui.note_delivery(bot, query.from_user.id, delivery)
        await query.answer()

    @router.startup()
    async def on_startup(bot: Bot) -> None:
        await set_commands(bot)
        ui.start(bot)

    @router.shutdown()
    async def on_shutdown() -> None:
        await ui.stop()

    return router


async def set_commands(bot: Bot) -> None:
    """The command menu in each language; Telegram shows the one matching the app's
    language and English to everyone else."""
    for lang in LANGUAGES:
        commands = [BotCommand(command=c, description=t(lang, f"cmd.{c}")) for c in COMMANDS]
        try:
            await bot.set_my_commands(commands, language_code=None if lang == SOURCE else lang)
        except TelegramAPIError as e:
            log.warning("setMyCommands (%s) failed: %s", lang, e)


def setup_bot(
    dp: Dispatcher, service: VoiceService, allowed: set[int], download_url: str = ""
) -> list[str]:
    """Wire the bot into the dispatcher. Returns allowed_updates for polling."""
    ui = WidgetUI(service, allowed, download_url)
    dp.include_router(build_router(service, allowed, ui))
    dp.update.outer_middleware(WidgetQueryMiddleware(ui.on_query))
    return [*dp.resolve_used_update_types(), *WIDGET_UPDATE_TYPES]
