"""VoiceForever Agent: VoiceForeverAgent.exe opens the agent window; --headless runs without it."""

import argparse
import asyncio
import contextlib
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .client import Agent, AuthError, ReplacedError
from . import i18n
from .config import AgentConfig, ConfigError, default_path, peek_language, write_template
from .plan import SendCommand

# Loaded on first use only (PEP 810), so the Windows-only modules are not imported when the
# agent stops early with a config error or runs its tests on another OS.
lazy from .gui import app as gui_app
lazy from .gui import winapi
lazy from .winio import WindowsBackend

log = logging.getLogger("vf_agent")


def setup_logging(log_path: Path) -> None:
    handlers: list[logging.Handler] = []
    with contextlib.suppress(OSError):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"))
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", handlers=handlers, force=True
    )


def pause_and_fail(message: str) -> int:
    log.error(message)
    if sys.stdin is not None and sys.stdin.isatty():
        with contextlib.suppress(EOFError):
            input("Press Enter to exit...")
    return 1


def run_headless(path: Path, test: bool) -> int:
    """The agent without a window, as before 0.3: for servers, scripts and CI."""
    i18n.set_language(peek_language(path))  # for the reasons it reports (a bad setting, a lost connection)
    if not path.exists():
        write_template(path)
        return pause_and_fail(f"Created {path}\nFill in server_url and token, then start the agent again.")
    try:
        config = AgentConfig.load(path)
    except ConfigError as e:
        return pause_and_fail(f"Config error in {path}: {e}")

    if sys.platform != "win32":
        log.info("The agent types into the game window and works only on Windows.")
        return 1

    backend = WindowsBackend(config.window_titles, config.process_names, config.key_delay_ms)
    agent = Agent(config, backend)

    if test:
        log.info(f"Switch to WoW within {config.focus_wait_s:g} s: a test line goes to /say...")
        result = asyncio.run(agent.handle(SendCommand("test", "say", "", ("VoiceForever test",))))
        log.info(f"test result: {result}")
        return 0

    log.info(f"VoiceForever Agent, mode: {config.mode}. Leave this window open while you play.")
    try:
        asyncio.run(agent.run_forever())
    except AuthError as e:
        return pause_and_fail(f"Error: {e}. Check token in {path}.")
    except ReplacedError as e:
        return pause_and_fail(f"Stopped: {e}.")
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="VoiceForever Agent")
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument("--test", action="store_true", help="type a test line into WoW /say and exit")
    parser.add_argument("--headless", action="store_true", help="run without the window, log to the console")
    parser.add_argument("--minimized", action="store_true", help="start in the tray (used by autostart)")
    args = parser.parse_args()

    if sys.platform == "win32" and (args.headless or args.test):
        winapi.attach_parent_console()  # the exe has no console of its own
    path = (args.config or default_path()).resolve()
    log_path = path.parent / "agent.log"
    setup_logging(log_path)

    if args.headless or args.test or sys.platform != "win32":
        return run_headless(path, args.test)
    return gui_app.run(path, log_path, start_hidden=args.minimized)


if __name__ == "__main__":
    sys.exit(main())
