"""Telegram bot: voice in, chat message out.

Replies come in the player's language (see i18n): the one they picked with /language or on
the panel, else the language of their Telegram app.
"""

import contextlib
import io
import itertools
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import cache
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    TelegramObject,
    User,
)

from .access import Check, LocalAccess, Outcome, Reason, Refusal, Source, StarPayment
from .chat import Channel, ChatMessage, channel_name
from .i18n import LANGUAGES, SOURCE, from_code, strings, t
from .router import Decision, RouteResult
from .service import Delivery, VoiceService, with_access_note
from .stt import TranscriptionError
from .tgwidgets import WIDGET_UPDATE_TYPES, WidgetQueryMiddleware
from .widgets import DEFAULT_CHANNELS, STYLE_PRESETS, Card, WidgetUI, failure_text

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
COMMANDS = ("start", "status", "panel", "channel", "confirm", "style", "names", "channels",
            "language", "widgets", "help")
# Added when the access point sells a subscription; Telegram asks for /terms and /paysupport.
PAYMENT_COMMANDS = ("buy", "terms", "paysupport")
STARS = "XTR"


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


class PlayerMiddleware(BaseMiddleware):
    """Finds the player behind each update and passes handlers `uid` (None for people the
    access point does not know), `refusal` (why not) and `lang`. Their Telegram app's
    language is noted; nothing is stored for strangers."""

    def __init__(self, ui: WidgetUI) -> None:
        self.ui = ui

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        uid: int | None = None
        refusal: Refusal | None = None
        if user is None or user.is_bot:
            lang = SOURCE
        else:
            found = await self.ui.player(user)
            if isinstance(found, Refusal):
                refusal, lang = found, from_code(user.language_code) or SOURCE
            else:
                uid, lang = found, self.ui.lang(found, user.language_code)
        data.update(uid=uid, refusal=refusal, lang=lang)
        return await handler(event, data)


class IsPlayer(Filter):
    """The update is from someone the access point knows (see PlayerMiddleware)."""

    async def __call__(self, event: TelegramObject, uid: int | None = None) -> bool:
        return uid is not None


def access_text(reason: str, lang: str) -> str:
    """Why the access point refused, for the player; an unknown reason reads as no access."""
    key = f"access.{reason}"
    return t(lang, key if key in strings(lang) else "access.no_access")


def link_keyboard(link: str, lang: str) -> InlineKeyboardMarkup | None:
    """A button to the account page, where the player can sign up, subscribe or link Telegram."""
    if not link.startswith("https://"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "bot.account_button"), url=link)]
    ])


def build_router(
    service: VoiceService, ui: WidgetUI, terms_url: str = "", pay_support: str = ""
) -> Router:
    router = Router()
    pending = PendingMessages()
    is_player = IsPlayer()
    players = PlayerMiddleware(ui)
    router.message.outer_middleware(players)
    router.callback_query.outer_middleware(players)
    router.pre_checkout_query.outer_middleware(players)
    access = service.access
    limit_told: dict[int, str] = {}  # player -> UTC day they were told AI messages ran out

    def language_line(uid: int, lang: str) -> str:
        if ui.store.ui(uid).lang in LANGUAGES:
            return LANGUAGES[lang]
        return t(lang, "bot.language_auto", name=LANGUAGES[lang])

    def language_menu(uid: int, lang: str) -> tuple[str, InlineKeyboardMarkup]:
        text = t(lang, "bot.language", name=language_line(uid, lang))
        return text, language_keyboard(lang, ui.store.ui(uid).lang, ui.telegram_lang(uid))

    async def refuse(message: Message, refusal: Refusal | None, lang: str) -> None:
        """Tell someone the bot does not know why, with a button to fix it if there is one."""
        refusal = refusal or Refusal(Reason.UNKNOWN)
        if refusal.reason == Reason.UNKNOWN and isinstance(access, LocalAccess):
            user = message.from_user
            text = t(lang, "bot.not_allowed", id=user.id if user else "?")
        else:
            text = access_text(refusal.reason, lang)
        await message.answer(text, reply_markup=link_keyboard(refusal.link, lang))

    @router.message(Command("start", "help"))
    async def start(
        message: Message,
        bot: Bot,
        command: CommandObject,
        lang: str,
        uid: int | None,
        refusal: Refusal | None,
    ) -> None:
        if uid is None:
            await refuse(message, refusal, lang)
            return
        if command.command == "start" and ui.enabled(uid):
            if await ui.send_setup(bot, message.chat.id, uid):
                return
        await message.answer(t(lang, "bot.help"))

    @router.message(Command("panel"), is_player)
    async def panel(message: Message, bot: Bot, lang: str, uid: int) -> None:
        if not ui.enabled(uid):
            await message.answer(t(lang, "bot.widgets_disabled"))
        elif not await ui.send_panel(bot, message.chat.id, uid):
            await message.answer(t(lang, "bot.panel_refused"))

    @router.message(Command("widgets"), is_player)
    async def set_widgets(message: Message, command: CommandObject, lang: str, uid: int) -> None:
        on = (command.args or "").strip().lower() not in OFF_WORDS
        service.store.update_ui(uid, widgets=on)
        await message.answer(t(lang, "bot.widgets_on" if on else "bot.widgets_off"))

    def game_line(uid: int, lang: str) -> str:
        """What the server knows from the player's game: how many channels and nicknames."""
        if (game := service.store.game(uid)) is None:
            return t(lang, "bot.game_none")
        c = game.counts()
        return t(
            lang, "bot.game",
            channels=c["channels"], guild=c["guild"], online=c["guild_online"], friends=c["friends"],
            group=c["group"], min=max(0, int(time.time() - game.made_at) // 60),
        )

    @router.message(Command("status"), is_player)
    async def status(message: Message, lang: str, uid: int) -> None:
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
        ) + "\n" + game_line(uid, lang))

    @router.message(Command("channels"), is_player)
    async def channels(message: Message, lang: str, uid: int) -> None:
        game = service.store.game(uid)
        if game is None or not game.channels:
            await message.answer(t(lang, "bot.channels_none"))
            return
        lines = "\n".join(f"{c.n}. {c.name}" for c in game.channels)
        await message.answer(t(lang, "bot.channels", list=lines))

    @router.message(Command("channel"), is_player)
    async def set_channel(
        message: Message, command: CommandObject, bot: Bot, lang: str, uid: int
    ) -> None:
        if (channel := parse_channel(command.args or "")) is None:
            await message.answer(t(lang, "bot.channel_usage"))
            return
        service.store.update(uid, default_channel=channel)
        await message.answer(t(lang, "bot.channel_set", channel=channel_name(channel, lang)))
        await ui.refresh_panel(bot, uid)

    @router.message(Command("confirm"), is_player)
    async def set_confirm(
        message: Message, command: CommandObject, bot: Bot, lang: str, uid: int
    ) -> None:
        on = (command.args or "").strip().lower() in ON_WORDS
        service.store.update(uid, confirm=on)
        await message.answer(t(lang, "bot.confirm_set", state=on_off(lang, on)))
        await ui.refresh_panel(bot, uid)

    @router.message(Command("style"), is_player)
    async def set_style(
        message: Message, command: CommandObject, bot: Bot, lang: str, uid: int
    ) -> None:
        if service.router is None or not service.router.allow_transform:
            await message.answer(t(lang, "bot.style_disabled"))
            return
        arg = (command.args or "").strip()
        style = "" if not arg or arg.lower() in OFF_WORDS else arg[:300]
        service.store.update(uid, rewrite_style=style)
        if style and style not in STYLE_PRESETS.values():
            service.store.update_ui(uid, custom_style=style)
        await message.answer(t(lang, "bot.style_set", style=style) if style else t(lang, "bot.style_off"))
        await ui.refresh_panel(bot, uid)

    @router.message(Command("names"), is_player)
    async def set_names(
        message: Message, command: CommandObject, bot: Bot, lang: str, uid: int
    ) -> None:
        names = ", ".join(n.strip() for n in (command.args or "").split(",") if n.strip())
        service.store.update(uid, known_names=names[:500])
        await message.answer(t(lang, "bot.names_set", names=names or "—"))
        await ui.refresh_panel(bot, uid)

    @router.message(Command("language", "lang"), is_player)
    async def set_language(
        message: Message, command: CommandObject, bot: Bot, lang: str, uid: int
    ) -> None:
        if (picked := parse_language(command.args or "")) is not None:
            lang = await ui.set_lang(bot, uid, picked)
            await message.answer(t(lang, "bot.language_set", name=language_line(uid, lang)))
            return
        text, keyboard = language_menu(uid, lang)
        await message.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("lang:"), is_player)
    async def on_language(query: CallbackQuery, bot: Bot, uid: int) -> None:
        code = (query.data or "").removeprefix("lang:")
        if code == "auto" or code in LANGUAGES:
            lang = await ui.set_lang(bot, uid, "" if code == "auto" else code)
            text, keyboard = language_menu(uid, lang)
            if isinstance(query.message, Message):
                with contextlib.suppress(TelegramBadRequest):  # "message is not modified"
                    await query.message.edit_text(text, reply_markup=keyboard)
        await query.answer()

    async def admit(
        message: Message, uid: int, lang: str, *, voice: bool, audio_s: float = 0.0
    ) -> Check | None:
        """Check before anything that costs money: the agent must be there to type the
        message, and the access point must allow it. None after telling the player why not."""
        if service.hub.info(uid) is None:
            await message.reply(t(lang, "ack.offline"))
            return None
        check = await service.admit(uid, Source.TELEGRAM, voice=voice, audio_s=audio_s)
        if not check.ok:
            await message.reply(access_text(check.reason, lang), reply_markup=link_keyboard(check.link, lang))
            return None
        return check

    @router.message(is_player, F.voice | F.audio | F.video_note)
    async def on_voice(message: Message, bot: Bot, lang: str, uid: int) -> None:
        match message:
            case Message(voice=voice) if voice:
                media, filename = voice, "voice.ogg"
            case Message(video_note=note) if note:
                media, filename = note, "note.mp4"
            case Message(audio=audio) if audio:
                media, filename = audio, audio.file_name or "audio.mp3"
            case _:
                return
        audio_s = float(media.duration or 0)
        if (check := await admit(message, uid, lang, voice=True, audio_s=audio_s)) is None:
            return
        buf = io.BytesIO()
        await bot.download(media, destination=buf)
        try:
            transcript = await service.transcribe(uid, buf.getvalue(), filename)
        except TranscriptionError as e:
            log.error("STT failed: %s", e)
            service.report(check, uid, Source.TELEGRAM, Outcome.STT_ERROR, audio_s)
            await message.reply(t(lang, "bot.stt_failed"))
            return
        if not transcript:
            service.report(check, uid, Source.TELEGRAM, Outcome.EMPTY, audio_s)
            await message.reply(t(lang, "bot.heard_nothing"))
            return
        await handle_phrase(message, transcript, bot, lang, uid, check, audio_s)

    @router.message(is_player, F.text, ~F.text.startswith("/"))
    async def on_text(message: Message, bot: Bot, lang: str, uid: int) -> None:
        if (check := await admit(message, uid, lang, voice=False)) is not None:
            await handle_phrase(message, message.text, bot, lang, uid, check)

    async def handle_phrase(
        message: Message,
        transcript: str,
        bot: Bot,
        lang: str,
        uid: int,
        check: Check,
        audio_s: float = 0.0,
    ) -> None:
        def report(outcome: Outcome) -> None:
            service.report(check, uid, Source.TELEGRAM, outcome, audio_s, result)

        widgets = ui.enabled(uid)
        card = ui.card_for_reply(message, uid) if widgets else None
        result = with_access_note(await service.route(uid, transcript, check.allow), check)
        note = note_text(result, lang)
        if result.decision is Decision.SKIP:
            report(Outcome.SKIPPED)
            await message.reply(t(lang, "bot.skipped", text=transcript))
            return
        if card is not None:  # a reply to a card replaces the card's text
            report(Outcome.ROUTED)
            await ui.correct_card(bot, card, result.message, transcript)
            return
        confirm = service.store.get(uid).confirm
        if widgets and (shown := await ui.show_card(bot, message, uid, result.message, transcript, confirm)):
            report(Outcome.ROUTED if confirm else card_outcome(shown))
            if result.note == "ai_limit" and limit_told.get(uid) != (today := utc_day()):
                limit_told[uid] = today  # cards have no room for notes: say it once a day
                await message.answer(t(lang, "note.ai_limit"))
            return
        if confirm:
            report(Outcome.ROUTED)
            pid = pending.add(uid, result.message)
            await message.reply(
                f"[{result.message.label(lang)}] {result.message.text}{note}",
                reply_markup=confirm_keyboard(pid, lang),
            )
            return
        delivery = await service.deliver(uid, result.message)
        report(Outcome.DELIVERED if delivery.ack.ok else Outcome.FAILED)
        await message.reply(describe(delivery, lang) + note)
        await ui.note_delivery(bot, uid, delivery)

    @router.callback_query(F.data.startswith("vf:"), is_player)
    async def on_button(query: CallbackQuery, bot: Bot, lang: str, uid: int) -> None:
        match (query.data or "").split(":"):
            case ["vf", action, pid, *rest]:
                pass
            case _:
                await query.answer()
                return
        item = pending.get(pid)
        if item is None or item[0] != uid:
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
                delivery = await service.deliver(uid, msg)
                await query.message.edit_text(describe(delivery, lang))
                await ui.note_delivery(bot, uid, delivery)
        await query.answer()

    if access.payments:
        add_payments(router, service, terms_url, pay_support)

    # Last: people the access point does not know get told why, in private chats only.
    @router.message(
        ~is_player,
        F.chat.type == "private",
        F.voice | F.audio | F.video_note | (F.text & ~F.text.startswith("/")),
    )
    async def stranger(message: Message, lang: str, refusal: Refusal | None) -> None:
        await refuse(message, refusal, lang)

    @router.startup()
    async def on_startup(bot: Bot) -> None:
        await set_commands(bot, payments=access.payments)
        ui.start(bot)

    @router.shutdown()
    async def on_shutdown() -> None:
        await ui.stop()

    return router


def add_payments(router: Router, service: VoiceService, terms_url: str, pay_support: str) -> None:
    """/buy: the access point's subscription for Telegram Stars, renewed by Telegram every
    30 days. The bot only passes the payment on; the access point keeps the subscription.
    Telegram asks bots that sell to answer /terms and /paysupport."""
    access = service.access
    is_player = IsPlayer()

    @router.message(Command("buy"), is_player)
    async def buy(message: Message, bot: Bot, lang: str, uid: int) -> None:
        offer = await access.offer(uid, lang)
        if isinstance(offer, Refusal):
            await message.answer(access_text(offer.reason, lang), reply_markup=link_keyboard(offer.link, lang))
            return
        try:
            link = await bot.create_invoice_link(
                title=offer.title[:32],
                description=(offer.description or offer.title)[:255],
                payload=offer.payload,
                currency=STARS,
                prices=[LabeledPrice(label=offer.title[:32], amount=offer.stars)],
                subscription_period=offer.period_s,
            )
        except TelegramAPIError as e:
            log.warning("createInvoiceLink failed: %s", e)
            await message.answer(t(lang, "bot.buy_failed"))
            return
        button = InlineKeyboardButton(text=t(lang, "bot.buy_button", stars=offer.stars), url=link)
        await message.answer(
            t(lang, "bot.buy", title=offer.title, stars=offer.stars),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
        )

    @router.pre_checkout_query()
    async def pre_checkout(
        query: PreCheckoutQuery, lang: str, uid: int | None, refusal: Refusal | None
    ) -> None:
        if uid is None:
            refused: Refusal | None = refusal or Refusal(Reason.UNKNOWN)
        elif query.currency != STARS:
            refused = Refusal(Reason.NO_ACCESS)
        else:
            refused = await access.pre_checkout(uid, query.invoice_payload, query.currency, query.total_amount)
        if refused is None:
            await query.answer(ok=True)
        else:
            await query.answer(ok=False, error_message=access_text(refused.reason, lang))

    @router.message(F.successful_payment)
    async def paid(message: Message, lang: str, uid: int | None) -> None:
        p = message.successful_payment
        payment = StarPayment(
            telegram_id=message.from_user.id if message.from_user else 0,
            payload=p.invoice_payload,
            currency=p.currency,
            total_amount=p.total_amount,
            telegram_payment_charge_id=p.telegram_payment_charge_id,
            is_recurring=bool(p.is_recurring),
            is_first_recurring=bool(p.is_first_recurring),
            subscription_expiration_date=p.subscription_expiration_date or 0,
        )
        log.info("Stars payment from %s: %s %s", payment.telegram_id, p.total_amount, p.currency)
        account = await access.paid(uid or 0, payment)
        if uid is not None and account is not None:
            await service.hub.send_account(uid, account)
        if until := payment.subscription_expiration_date:
            day = datetime.fromtimestamp(until, UTC).strftime("%Y-%m-%d")
            await message.answer(t(lang, "bot.paid_until", date=day))
        else:
            await message.answer(t(lang, "bot.paid"))

    @router.message(Command("terms"))
    async def terms(message: Message, lang: str) -> None:
        await message.answer(t(lang, "bot.terms", url=terms_url or "—"))

    @router.message(Command("paysupport"))
    async def paysupport(message: Message, lang: str) -> None:
        await message.answer(t(lang, "bot.paysupport", contact=pay_support or terms_url or "—"))


def card_outcome(card: Card) -> Outcome:
    """A card sent without confirmation has tried the game by the time it is shown."""
    return Outcome.DELIVERED if card.state == "sent" else Outcome.FAILED


def utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


async def set_commands(bot: Bot, payments: bool = False) -> None:
    """The command menu in each language; Telegram shows the one matching the app's
    language and English to everyone else."""
    names = COMMANDS + (PAYMENT_COMMANDS if payments else ())
    for lang in LANGUAGES:
        commands = [BotCommand(command=c, description=t(lang, f"cmd.{c}")) for c in names]
        try:
            await bot.set_my_commands(commands, language_code=None if lang == SOURCE else lang)
        except TelegramAPIError as e:
            log.warning("setMyCommands (%s) failed: %s", lang, e)


def setup_bot(
    dp: Dispatcher,
    service: VoiceService,
    download_url: str = "",
    terms_url: str = "",
    pay_support: str = "",
) -> list[str]:
    """Wire the bot into the dispatcher. Returns allowed_updates for polling."""
    ui = WidgetUI(service, download_url)
    dp.include_router(build_router(service, ui, terms_url, pay_support))
    dp.update.outer_middleware(WidgetQueryMiddleware(ui.on_query))
    return [*dp.resolve_used_update_types(), *WIDGET_UPDATE_TYPES]
