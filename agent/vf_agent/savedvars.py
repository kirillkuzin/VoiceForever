"""Read what the addon saved: WTF/Account/<account>/SavedVariables/VoiceForever.lua.

The game writes this file on logout and on /reload. The addon puts the result of the
push-to-talk button check there, so the agent window can show it. Only the small part of
Lua that the game writes is understood: tables, strings, numbers, booleans and nil.
"""

import re
from pathlib import Path
from typing import Any

ADDON_FILE = "VoiceForever.lua"
VARIABLE = "VoiceForeverDB"

_TOKEN = re.compile(
    r"""
    (?P<space>\s+|--\[\[.*?\]\]|--[^\n]*)
  | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<number>-?(?:0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?))
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<punct>[{}\[\]=,;])
    """,
    re.VERBOSE | re.DOTALL,
)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "\n": "\n", "a": "\a", "b": "\b"}


class LuaError(ValueError):
    pass


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None:
            raise LuaError(f"unexpected {text[pos:pos + 10]!r}")
        pos = m.end()
        kind = m.lastgroup or ""
        if kind != "space":
            out.append((kind, m.group()))
    return out


def _unquote(literal: str) -> str:
    body, out, i = literal[1:-1], [], 0
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        nxt = body[i + 1] if i + 1 < len(body) else ""
        if nxt.isdigit():  # \ddd is a byte in decimal; the game writes UTF-8 this way
            digits = re.match(r"\d{1,3}", body[i + 1 :]).group()
            out.append(chr(int(digits)))
            i += 1 + len(digits)
        else:
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
    text = "".join(out)
    try:  # bytes written as \ddd come back as Latin-1 characters: put the UTF-8 together
        return text.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return text


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = _tokens(text)
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        token = self.peek()
        if token is None or (value is not None and token[1] != value):
            raise LuaError(f"expected {value or 'a value'}, got {token}")
        self.i += 1
        return token

    def assignments(self) -> dict[str, Any]:
        out = {}
        while self.peek() is not None:
            kind, name = self.take()
            if kind != "name":
                raise LuaError(f"expected a variable name, got {name!r}")
            self.take("=")
            out[name] = self.value()
        return out

    def value(self) -> Any:
        kind, text = self.take()
        match kind, text:
            case "string", _:
                return _unquote(text)
            case "number", _:
                number = float.fromhex(text) if "x" in text.lower() else float(text)
                return int(number) if number.is_integer() and "." not in text else number
            case "name", "true":
                return True
            case "name", "false":
                return False
            case "name", "nil":
                return None
            case "punct", "{":
                return self.table()
        raise LuaError(f"unexpected {text!r}")

    def table(self) -> dict[Any, Any] | list[Any]:
        items: dict[Any, Any] = {}
        index = 1
        while (token := self.peek()) is not None and token[1] != "}":
            if token[1] == "[":
                self.take("[")
                key = self.value()
                self.take("]")
                self.take("=")
                items[key] = self.value()
            elif token[0] == "name" and self.i + 1 < len(self.tokens) and self.tokens[self.i + 1][1] == "=":
                self.take()
                self.take("=")
                items[token[1]] = self.value()
            else:
                items[index] = self.value()
                index += 1
            if (sep := self.peek()) is not None and sep[1] in (",", ";"):
                self.take()
        self.take("}")
        if items and list(items) == list(range(1, len(items) + 1)):
            return list(items.values())
        return items


def parse(text: str) -> dict[str, Any]:
    """Global assignments of a SavedVariables file: {"VoiceForeverDB": {...}}."""
    return _Parser(text).assignments()


def addon_files(game_exe: Path) -> list[Path]:
    """The addon's saved files for every account, newest first. The WTF folder is next to the exe."""
    root = game_exe.parent / "WTF" / "Account"
    files = [p for p in root.glob(f"*/SavedVariables/{ADDON_FILE}") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def read_addon_db(path: Path) -> dict[str, Any]:
    data = parse(path.read_text(encoding="utf-8", errors="replace")).get(VARIABLE)
    return data if isinstance(data, dict) else {}
