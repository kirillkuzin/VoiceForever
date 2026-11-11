"""Per-user settings in SQLite, recent chat history in memory."""

import sqlite3
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from .chat import Channel
from .router import SentRecord


@dataclass(frozen=True, slots=True)
class UserSettings:
    default_channel: Channel = Channel.SAY
    confirm: bool = False
    rewrite_style: str = ""
    known_names: str = ""  # comma-separated

    def names(self) -> list[str]:
        return [n.strip() for n in self.known_names.split(",") if n.strip()]


_COLUMNS = tuple(f.name for f in fields(UserSettings))


@dataclass(frozen=True, slots=True)
class UiState:
    """Telegram widget state: which messages to keep up to date, setup progress."""

    widgets: bool = True  # False: plain text with inline buttons, as before widgets
    panel_msg_id: int = 0  # the pinned control panel
    setup_msg_id: int = 0  # the setup checklist from /start
    setup_test: str = ""  # "" | "busy" | "ask" | "help" | "fail" | "ok"
    setup_voice: str = ""  # first message delivered after the test, shown in the checklist
    custom_style: str = ""  # last /style text, so the panel can switch back to it


_UI_COLUMNS = tuple(f.name for f in fields(UiState))


class Store:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, autocommit=True)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS user_settings (
                   user_id INTEGER PRIMARY KEY,
                   default_channel TEXT NOT NULL DEFAULT 'say',
                   confirm INTEGER NOT NULL DEFAULT 0,
                   rewrite_style TEXT NOT NULL DEFAULT '',
                   known_names TEXT NOT NULL DEFAULT ''
               ) STRICT"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS ui_state (
                   user_id INTEGER PRIMARY KEY,
                   widgets INTEGER NOT NULL DEFAULT 1,
                   panel_msg_id INTEGER NOT NULL DEFAULT 0,
                   setup_msg_id INTEGER NOT NULL DEFAULT 0,
                   setup_test TEXT NOT NULL DEFAULT '',
                   setup_voice TEXT NOT NULL DEFAULT '',
                   custom_style TEXT NOT NULL DEFAULT ''
               ) STRICT"""
        )
        self._recent: defaultdict[int, deque[SentRecord]] = defaultdict(lambda: deque(maxlen=20))

    def get(self, user_id: int) -> UserSettings:
        row = self.db.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return UserSettings()
        channel, confirm, style, names = row
        return UserSettings(Channel.parse(channel), bool(confirm), style, names)

    def update(self, user_id: int, **changes: object) -> UserSettings:
        if unknown := changes.keys() - set(_COLUMNS):
            raise KeyError(", ".join(sorted(unknown)))
        current = replace(self.get(user_id), **changes)
        values = asdict(current)
        values["confirm"] = int(values["confirm"])
        self.db.execute(
            f"""INSERT INTO user_settings (user_id, {', '.join(_COLUMNS)})
                VALUES (:user_id, {', '.join(f':{c}' for c in _COLUMNS)})
                ON CONFLICT(user_id) DO UPDATE SET
                {', '.join(f'{c} = excluded.{c}' for c in _COLUMNS)}""",
            {"user_id": user_id, **values},
        )
        return current

    def ui(self, user_id: int) -> UiState:
        row = self.db.execute(
            f"SELECT {', '.join(_UI_COLUMNS)} FROM ui_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return UiState()
        widgets, panel, setup, test, voice, style = row
        return UiState(bool(widgets), panel, setup, test, voice, style)

    def update_ui(self, user_id: int, **changes: object) -> UiState:
        if unknown := changes.keys() - set(_UI_COLUMNS):
            raise KeyError(", ".join(sorted(unknown)))
        current = replace(self.ui(user_id), **changes)
        values = asdict(current)
        values["widgets"] = int(values["widgets"])
        self.db.execute(
            f"""INSERT INTO ui_state (user_id, {', '.join(_UI_COLUMNS)})
                VALUES (:user_id, {', '.join(f':{c}' for c in _UI_COLUMNS)})
                ON CONFLICT(user_id) DO UPDATE SET
                {', '.join(f'{c} = excluded.{c}' for c in _UI_COLUMNS)}""",
            {"user_id": user_id, **values},
        )
        return current

    def remember(self, user_id: int, channel: Channel, target: str, text: str) -> None:
        self._recent[user_id].append(SentRecord(time.time(), channel, target, text))

    def recent(self, user_id: int) -> list[SentRecord]:
        return list(self._recent[user_id])
