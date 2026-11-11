"""Push-to-talk rules: which pad buttons can record, and when a press starts or ends a recording.

Pure logic, no SDL and no Windows calls. The addon applies the same rules to show its
recording badge in the game, so the two stay in step without talking to each other.
"""

from dataclasses import dataclass
from enum import StrEnum

from . import i18n

MIN_RECORD_S = 0.3  # shorter recordings are accidental presses and are dropped


class PttMode(StrEnum):
    TOGGLE = "toggle"  # press to start, press again to send
    HOLD = "hold"  # hold while talking, release to send


class Cmd(StrEnum):
    START = "start"
    SEND = "send"
    CANCEL = "cancel"  # too short: drop the recording


@dataclass(frozen=True, slots=True)
class PadButton:
    name: str  # value in config.toml
    sdl: int  # SDL_GamepadButton

    @property
    def xbox(self) -> str:
        """The button's name on an Xbox pad, in the window's language."""
        return i18n.t(f"padbtn.{self.name}")

    @property
    def playstation(self) -> str:
        """Its name on a DualSense; the same as on Xbox where the locale has no own one."""
        return i18n.strings(i18n.current()).get(f"padbtn.{self.name}.ps") or self.xbox


# The Guide / PS button is left out: Windows and Steam use it. Names: padbtn.* in ui/locales.
PAD_BUTTONS: tuple[PadButton, ...] = (
    PadButton("leftstick", 7),
    PadButton("rightstick", 8),
    PadButton("misc1", 15),
    PadButton("back", 4),
    PadButton("start", 6),
    PadButton("touchpad", 20),
    PadButton("a", 0),
    PadButton("b", 1),
    PadButton("x", 2),
    PadButton("y", 3),
    PadButton("leftshoulder", 9),
    PadButton("rightshoulder", 10),
    PadButton("dpup", 11),
    PadButton("dpdown", 12),
    PadButton("dpleft", 13),
    PadButton("dpright", 14),
    PadButton("paddle1", 16),
    PadButton("paddle2", 17),
    PadButton("paddle3", 18),
    PadButton("paddle4", 19),
)
BY_NAME: frozendict[str, PadButton] = frozendict({b.name: b for b in PAD_BUTTONS})
BY_SDL: frozendict[int, PadButton] = frozendict({b.sdl: b for b in PAD_BUTTONS})


def button_label(name: str, playstation: bool = False) -> str:
    button = BY_NAME.get(name)
    if button is None:
        return name
    return button.playstation if playstation else button.xbox


@dataclass(slots=True)
class PttMachine:
    """Turns presses of the chosen button into start / send / cancel.

    A press only starts a recording while the game is in front: the game, and with it the
    addon's badge, sees the pad only then. Ending a recording works from anywhere.
    """

    mode: PttMode = PttMode.TOGGLE
    max_s: float = 30.0
    started_at: float | None = None

    @property
    def recording(self) -> bool:
        return self.started_at is not None

    def elapsed(self, now: float) -> float:
        return 0.0 if self.started_at is None else now - self.started_at

    def press(self, now: float, game_focused: bool) -> Cmd | None:
        if self.started_at is None:
            if not game_focused:
                return None
            self.started_at = now
            return Cmd.START
        if self.mode == PttMode.TOGGLE:
            return self._finish(now)
        return None  # HOLD: a second press while held cannot happen; ignore it

    def release(self, now: float) -> Cmd | None:
        if self.mode == PttMode.HOLD and self.started_at is not None:
            return self._finish(now)
        return None

    def tick(self, now: float) -> Cmd | None:
        """Called often while recording: sends what was said once max_s has passed."""
        if self.started_at is not None and now - self.started_at >= self.max_s:
            return self._finish(now)
        return None

    def abort(self) -> Cmd | None:
        """Drop the recording (settings changed, pad unplugged, agent stopping)."""
        if self.started_at is None:
            return None
        self.started_at = None
        return Cmd.CANCEL

    def _finish(self, now: float) -> Cmd:
        elapsed = self.elapsed(now)
        self.started_at = None
        return Cmd.SEND if elapsed >= MIN_RECORD_S else Cmd.CANCEL
