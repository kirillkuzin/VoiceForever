"""VoiceForever Agent: run `python -m vf_agent` or VoiceForeverAgent.exe."""

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path

from .client import Agent, AuthError
from .config import AgentConfig, default_path, write_template
from .plan import SendCommand

# Loaded on first use only (PEP 810), so the module, which needs Windows, is not
# imported when the agent stops early with a config error or runs on another OS.
lazy from .winio import WindowsBackend


def pause_and_fail(message: str) -> int:
    print(message)
    with contextlib.suppress(EOFError):  # no console attached
        input("Press Enter to exit...")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="VoiceForever Agent")
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument(
        "--test", action="store_true", help="type a test line into WoW /say and exit"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    path = args.config or default_path()
    if not path.exists():
        write_template(path)
        return pause_and_fail(
            f"Created {path}\nFill in server_url and token, then start the agent again."
        )
    try:
        config = AgentConfig.load(path)
    except (ValueError, TypeError) as e:
        return pause_and_fail(f"Config error in {path}: {e}")

    if sys.platform != "win32":
        print("The agent types into the game window and works only on Windows.")
        return 1

    backend = WindowsBackend(config.window_titles, config.process_names, config.key_delay_ms)
    agent = Agent(config, backend)

    if args.test:
        print(f"Switch to WoW within {config.focus_wait_s:g} s: a test line goes to /say...")
        print(asyncio.run(agent.handle(SendCommand("test", "say", "", ("VoiceForever test",)))))
        return 0

    print(f"VoiceForever Agent, mode: {config.mode}. Leave this window open while you play.")
    try:
        asyncio.run(agent.run_forever())
    except AuthError as e:
        return pause_and_fail(f"Error: {e}. Check token in {path}.")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
