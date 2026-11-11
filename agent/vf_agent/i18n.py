"""The agent's languages: English, Russian, German, Spanish and French.

The window, the tray and the messages from Python share one table per language in
ui/locales/<code>.json. English is the source: a string missing from another table falls
back to it. Placeholders are {name}, filled the same way here and in app.js.

The first language comes from the Windows display language; the window can switch it and
keeps the choice in config.toml (language = "" follows Windows).
"""

import ctypes
import json
import locale
import os
import re
import sys
from functools import cache
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent / "ui" / "locales"
SOURCE = "en"

# Code -> the language's own name, in the order the window lists them.
LANGUAGES: frozendict[str, str] = frozendict({
    "en": "English",
    "ru": "Русский",
    "de": "Deutsch",
    "es": "Español",
    "fr": "Français",
})

# Windows primary language ids (the low 10 bits of a LANGID).
WINDOWS_LANGUAGES: frozendict[int, str] = frozendict({0x09: "en", 0x19: "ru", 0x07: "de", 0x0A: "es", 0x0C: "fr"})

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_current = SOURCE


@cache
def strings(lang: str) -> frozendict[str, str]:
    """Every string in `lang`, English where the table has no translation."""
    table = _read(SOURCE)
    if lang != SOURCE and lang in LANGUAGES:
        table |= _read(lang)
    return frozendict(table)


def _read(lang: str) -> dict[str, str]:
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def fill(text: str, values: dict[str, object]) -> str:
    return _PLACEHOLDER.sub(lambda m: str(values[m[1]]) if m[1] in values else m[0], text)


def t(key: str, /, **values: object) -> str:
    """The string `key` in the current language, with {placeholders} filled in."""
    return fill(strings(_current).get(key, key), values)


def current() -> str:
    return _current


def from_tag(tag: str | None) -> str | None:
    """Our code for a language tag such as "ru_RU.UTF-8", "de-AT" or "es"; None if we lack it."""
    if not tag:
        return None
    code = re.split(r"[-_.@:]", tag.strip().lower(), maxsplit=1)[0]
    return code if code in LANGUAGES else None


def system_language() -> str:
    """The language Windows shows its own menus in; English when we have no translation."""
    if sys.platform == "win32":
        try:
            langid = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
        except (AttributeError, OSError):
            langid = 0
        if langid:
            return WINDOWS_LANGUAGES.get(langid & 0x3FF, SOURCE)
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if lang := from_tag(os.environ.get(name)):
            return lang
    try:
        return from_tag(locale.getlocale()[0]) or SOURCE
    except ValueError:
        return SOURCE


def resolve(setting: str) -> str:
    """The language a config value means: one of LANGUAGES, or "" for the system's."""
    return setting if setting in LANGUAGES else system_language()


def set_language(setting: str) -> str:
    """Switch to the language of a config value; returns the code now in use."""
    global _current
    _current = resolve(setting)
    return _current
