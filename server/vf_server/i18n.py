"""The bot's languages: English, Russian, German, Spanish and French.

Replies, inline buttons and the text inside the widgets come from locales/<code>.json.
English is the source: a string missing from another table falls back to it. Placeholders
are {name}. Each player gets the language of their Telegram app (language_code) until they
pick one with /language or on the panel; the choice is kept per user in the database.
"""

import json
import re
from functools import cache
from importlib import resources

SOURCE = "en"

# Code -> the language's own name, in the order the bot lists them.
LANGUAGES: frozendict[str, str] = frozendict({
    "en": "English",
    "ru": "Русский",
    "de": "Deutsch",
    "es": "Español",
    "fr": "Français",
})

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@cache
def strings(lang: str) -> frozendict[str, str]:
    """Every string in `lang`, English where the table has no translation."""
    table = _read(SOURCE)
    if lang != SOURCE and lang in LANGUAGES:
        table |= _read(lang)
    return frozendict(table)


def _read(lang: str) -> dict[str, str]:
    return json.loads((resources.files("vf_server") / "locales" / f"{lang}.json").read_text(encoding="utf-8"))


def t(lang: str, key: str, /, **values: object) -> str:
    """The string `key` in `lang`, with {placeholders} filled in."""
    text = strings(lang).get(key, key)
    return _PLACEHOLDER.sub(lambda m: str(values[m[1]]) if m[1] in values else m[0], text)


def from_code(code: str | None) -> str | None:
    """Our code for a Telegram language_code such as "ru", "en-US" or "pt-br"; None if we lack it."""
    if not code:
        return None
    lang = re.split(r"[-_]", code.strip().lower(), maxsplit=1)[0]
    return lang if lang in LANGUAGES else None
