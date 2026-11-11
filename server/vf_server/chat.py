"""WoW chat rules: channels, text sanitising and splitting into chat-sized parts."""

import re
from dataclasses import dataclass
from enum import StrEnum

from .i18n import SOURCE, t


class Channel(StrEnum):
    """Semantic channels. GROUP lets the in-game addon pick party / raid / instance chat."""

    SAY = "say"
    YELL = "yell"
    EMOTE = "emote"
    GROUP = "group"
    PARTY = "party"
    RAID = "raid"
    RAID_WARNING = "raid_warning"
    INSTANCE = "instance"
    GUILD = "guild"
    OFFICER = "officer"
    WHISPER = "whisper"
    REPLY = "reply"
    CHANNEL = "channel"

    @classmethod
    def parse(cls, value: str, default: Channel | None = None) -> Channel:
        try:
            return cls(value)
        except ValueError:
            return default if default is not None else cls.SAY


def channel_name(channel: Channel, lang: str = SOURCE) -> str:
    """The channel's name for people, e.g. "Guild" or "Гильдия"."""
    return t(lang, f"chan.{channel}")


_NAME_RE = re.compile(r"^[^\s\d\W][^\s\W]{1,11}(-[\w']{2,30})?$")


@dataclass(frozen=True, slots=True)
class ChatMessage:
    channel: Channel
    target: str  # whisper recipient or channel number, "" otherwise
    text: str

    def label(self, lang: str = SOURCE) -> str:
        """Where the message goes, for people: "Guild", "Whisper → Arthorn", "Channel 2"."""
        match self.channel:
            case Channel.WHISPER if self.target:
                return t(lang, "chan.whisper_to", name=self.target)
            case Channel.CHANNEL if self.target:
                return t(lang, "chan.numbered", n=self.target)
            case _:
                return channel_name(self.channel, lang)


def validate(
    channel: str, target: str, text: str, default_channel: Channel = Channel.SAY
) -> ChatMessage:
    """Build a safe ChatMessage, falling back when the router returns something unusable."""
    ch = Channel.parse(channel, default_channel)
    target = target.strip()
    match ch:
        case Channel.WHISPER if not _NAME_RE.match(target):
            ch, target = Channel.REPLY, ""
        case Channel.CHANNEL if not target.isdigit():
            ch, target = default_channel, ""
        case Channel.WHISPER | Channel.CHANNEL:
            pass
        case _:
            target = ""
    return ChatMessage(ch, target, sanitize(text))


def sanitize(text: str) -> str:
    """Make text safe to paste into a WoW edit box.

    * one line only (Enter would send early);
    * no "|" (WoW escape character: links, colours, textures);
    * never starts with "/" or "." so it cannot run a slash command or a server command.
    """
    text = text.replace("|", "/")
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.lstrip("/. ")


def split_message(text: str, max_bytes: int = 255) -> list[str]:
    """Split text into parts of at most max_bytes UTF-8 bytes, on word borders."""
    text = text.strip()
    if not text:
        return []
    if len(text.encode()) <= max_bytes:
        return [text]

    parts: list[str] = []
    current = ""
    for word in re.split(r"(?<=\s)", text):  # keep trailing spaces with words
        candidate = current + word
        if len(candidate.rstrip().encode()) <= max_bytes:
            current = candidate
            continue
        if current.strip():
            parts.append(current.strip())
        current = word
        # A single "word" longer than the limit: cut it by characters.
        while len(current.rstrip().encode()) > max_bytes:
            cut = _cut_at_bytes(current, max_bytes)
            parts.append(current[:cut].strip())
            current = current[cut:]
    if current.strip():
        parts.append(current.strip())
    return parts


def _cut_at_bytes(text: str, max_bytes: int) -> int:
    size = 0
    for i, ch in enumerate(text):
        size += len(ch.encode())
        if size > max_bytes:
            return max(i, 1)
    return len(text)
