"""Telegram bot: voice in, chat message out."""

import io
import itertools
import logging
from collections import OrderedDict

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .chat import LABELS, Channel, ChatMessage
from .hub import AckStatus
from .router import Decision
from .service import Delivery, VoiceService
from .stt import TranscriptionError

log = logging.getLogger(__name__)

CHANNEL_ALIASES: frozendict[str, Channel] = frozendict({
    "пати": Channel.GROUP, "группа": Channel.GROUP, "группу": Channel.GROUP,
    "рейд": Channel.RAID, "инст": Channel.INSTANCE, "данж": Channel.INSTANCE,
    "гильдия": Channel.GUILD, "гильдию": Channel.GUILD, "ги": Channel.GUILD,
    "офицеры": Channel.OFFICER, "сказать": Channel.SAY, "крик": Channel.YELL,
})
DEFAULT_CHANNEL_CHOICES = frozenset(Channel) - {Channel.WHISPER, Channel.CHANNEL, Channel.REPLY}
QUICK_CHANNELS = (Channel.SAY, Channel.GROUP, Channel.GUILD)

ACK_TEXT: frozendict[AckStatus, str] = frozendict({
    AckStatus.OFFLINE: "Агент на ПК не подключён. Запусти VoiceForever Agent.",
    AckStatus.NOT_FOCUSED: "Окно WoW не было активным, сообщение не отправлено.",
    AckStatus.TIMEOUT: "Агент не ответил вовремя. Проверь, отправилось ли сообщение.",
    AckStatus.ERROR: "Агент не смог ввести текст",
})

HELP = (
    "Отправь голосовое, и я напечатаю его в чат WoW.\n\n"
    "Канал выбирается по смыслу или по словам в начале фразы: «в пати…», «в гильдию…», "
    "«шепни Арторну…», «ответь ему…», «крикни…», «в трейд…».\n\n"
    "Команды:\n"
    "/status — подключён ли агент на ПК\n"
    "/channel пати|гильдия|сказать|рейд — канал по умолчанию\n"
    "/confirm on|off — спрашивать перед отправкой\n"
    "/style <как писать> — переписывать сообщения (например: «как орк-шаман»), /style off — выключить\n"
    "/names Арторн, Лиана — ники, которые часто называешь"
)


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


def confirm_keyboard(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data=f"vf:send:{pid}"),
            InlineKeyboardButton(text="✖️ Отмена", callback_data=f"vf:cancel:{pid}"),
        ],
        [
            InlineKeyboardButton(text=LABELS[ch], callback_data=f"vf:ch:{pid}:{ch}")
            for ch in QUICK_CHANNELS
        ],
    ])


def describe(delivery: Delivery) -> str:
    msg = delivery.message
    if delivery.ack.ok:
        suffix = f" ({len(delivery.parts)} сообщ.)" if len(delivery.parts) > 1 else ""
        return f"✓ [{msg.label()}] {msg.text}{suffix}"
    base = ACK_TEXT.get(delivery.ack.status, ACK_TEXT[AckStatus.ERROR])
    detail = f": {delivery.ack.detail}" if delivery.ack.detail else ""
    return f"⚠️ {base}{detail}\n[{msg.label()}] {msg.text}"


def build_router(service: VoiceService, allowed: set[int]) -> Router:
    router = Router()
    pending = PendingMessages()
    allowed_filter = F.from_user.id.in_(allowed)

    @router.message(Command("start", "help"))
    async def start(message: Message) -> None:
        user = message.from_user
        if user is None or user.id not in allowed:
            uid = user.id if user else "?"
            await message.answer(
                f"Твой Telegram ID: {uid}. Добавь его в VF_USERS на сервере, чтобы пользоваться ботом."
            )
            return
        await message.answer(HELP)

    @router.message(Command("status"), allowed_filter)
    async def status(message: Message) -> None:
        uid = message.from_user.id
        s = service.store.get(uid)
        match service.hub.info(uid):
            case None:
                agent = "Агент не подключён."
            case info:
                agent = f"Агент подключён (v{info.version}, режим {info.mode})."
        await message.answer(
            f"{agent}\nКанал по умолчанию: {LABELS[s.default_channel]}\n"
            f"Подтверждение: {'вкл' if s.confirm else 'выкл'}\n"
            f"Стиль: {s.rewrite_style or 'выкл'}\n"
            f"Ники: {s.known_names or '—'}"
        )

    @router.message(Command("channel"), allowed_filter)
    async def set_channel(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip().lower()
        channel = CHANNEL_ALIASES.get(arg) or (Channel(arg) if arg in Channel else None)
        if channel not in DEFAULT_CHANNEL_CHOICES:
            await message.answer("Например: /channel пати, /channel гильдия, /channel сказать")
            return
        service.store.update(message.from_user.id, default_channel=channel)
        await message.answer(f"Канал по умолчанию: {LABELS[channel]}")

    @router.message(Command("confirm"), allowed_filter)
    async def set_confirm(message: Message, command: CommandObject) -> None:
        on = (command.args or "").strip().lower() in {"on", "вкл", "1", "да"}
        service.store.update(message.from_user.id, confirm=on)
        await message.answer(f"Подтверждение перед отправкой: {'вкл' if on else 'выкл'}")

    @router.message(Command("style"), allowed_filter)
    async def set_style(message: Message, command: CommandObject) -> None:
        if service.router is None or not service.router.allow_transform:
            await message.answer("Переписывание сообщений выключено на этом сервере.")
            return
        arg = (command.args or "").strip()
        style = "" if arg.lower() in {"", "off", "выкл"} else arg[:300]
        service.store.update(message.from_user.id, rewrite_style=style)
        await message.answer(
            f"Стиль: {style}" if style else "Стиль выключен, правлю только пунктуацию."
        )

    @router.message(Command("names"), allowed_filter)
    async def set_names(message: Message, command: CommandObject) -> None:
        names = ", ".join(n.strip() for n in (command.args or "").split(",") if n.strip())
        service.store.update(message.from_user.id, known_names=names[:500])
        await message.answer(f"Ники: {names or '—'}")

    @router.message(allowed_filter, F.voice | F.audio | F.video_note)
    async def on_voice(message: Message, bot: Bot) -> None:
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
            await message.reply("⚠️ Не получилось распознать речь, попробуй ещё раз.")
            return
        if not transcript:
            await message.reply("Не расслышал ни слова.")
            return
        await handle_phrase(message, transcript)

    @router.message(allowed_filter, F.text, ~F.text.startswith("/"))
    async def on_text(message: Message) -> None:
        await handle_phrase(message, message.text)

    async def handle_phrase(message: Message, transcript: str) -> None:
        uid = message.from_user.id
        result = await service.route(uid, transcript)
        note = f"\n({result.note})" if result.note else ""
        if result.decision is Decision.SKIP:
            await message.reply(f"Пропустил, не похоже на сообщение в чат: «{transcript}»")
            return
        if service.store.get(uid).confirm:
            pid = pending.add(uid, result.message)
            await message.reply(
                f"[{result.message.label()}] {result.message.text}{note}",
                reply_markup=confirm_keyboard(pid),
            )
            return
        delivery = await service.deliver(uid, result.message)
        await message.reply(describe(delivery) + note)

    @router.callback_query(F.data.startswith("vf:"), allowed_filter)
    async def on_button(query: CallbackQuery) -> None:
        match (query.data or "").split(":"):
            case ["vf", action, pid, *rest]:
                pass
            case _:
                await query.answer()
                return
        item = pending.get(pid)
        if item is None or item[0] != query.from_user.id:
            await query.answer("Это сообщение уже неактуально.")
            return
        _, msg = item
        match action, rest:
            case "cancel", _:
                pending.pop(pid)
                await query.message.edit_text(f"✖️ Отменено: {msg.text}")
            case "ch", [channel] if channel in Channel:
                msg = ChatMessage(Channel(channel), "", msg.text)
                pending.replace(pid, msg)
                await query.message.edit_text(
                    f"[{msg.label()}] {msg.text}", reply_markup=confirm_keyboard(pid)
                )
            case "send", _:
                pending.pop(pid)
                delivery = await service.deliver(query.from_user.id, msg)
                await query.message.edit_text(describe(delivery))
        await query.answer()

    return router
