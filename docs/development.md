# Development

Python 3.15, dependencies through [uv](https://docs.astral.sh/uv/). uv downloads the right Python and the exact library versions from each `uv.lock` by itself.

## Layout

| Folder | What it is |
|---|---|
| `server/` | `vf_server`: Telegram bot (aiogram), speech recognition client, Claude router, WebSocket hub for the Windows apps, SQLite store, Telegram widgets (`html/`) |
| `agent/` | `vf_agent`: the Windows app. Connection to the server, typing into the game (`winio.py`), gamepad and microphone through SDL3 (`sdl.py`, `devices.py`), the window (`gui/`, page in `ui/`) |
| `addon/` | the WoW addon (`VoiceForever/`) and its tests |
| `docs/` | these guides and the screenshots |

## Tests

```
cd server && uv run pytest
cd agent && uv run pytest
cd addon && uv run --no-project --python 3.13 --with lupa --with pytest pytest   # the addon runs in Lua 5.1 against stubs of the WoW API
```

`agent/tests/test_devices.py` drives the real `SDL3.dll` (a virtual gamepad and the dummy microphone) and is skipped when the library is missing.

## Builds

- Server image: `docker build -t ghcr.io/kirillkuzin/voiceforever-server:latest ./server`; see [Building from source](server.md#building-from-source).
- Windows exe: `agent\build.bat` → `agent\dist\VoiceForeverAgent.exe`; see [From source](agent.md#from-source).

CI ([.github/workflows](../.github/workflows)) tests everything, publishes the server image to GHCR and builds the exe on Windows.

## Releases

1. Set the new version in `agent/pyproject.toml`, `agent/vf_agent/__init__.py`, `server/pyproject.toml`, `server/vf_server/__init__.py`, `addon/VoiceForever/VoiceForever.toc`, and run `uv lock` in `agent/` and `server/`.
2. Render the screenshots again (they show the version) and merge to `master`.
3. Publish a release on GitHub with the tag `v<version>` on `master`. CI attaches the exe, the addon zip and `SHA256SUMS.txt` under the same names every time, so `releases/latest/download/<name>` always serves the newest, and tags the server image `<version>` and `<major>.<minor>`.

## Screenshots

The pictures in [docs/images](images) are rendered from the real agent page and Telegram widgets, with fake data in place of the server, the game and Telegram:

```
uv run --no-project --python 3.13 --with playwright playwright install chromium   # once
uv run --no-project --python 3.13 --with playwright python docs/images/render.py
```

Run it again after changing the UI. `VF_LANG=ru` (or `de`, `es`, `fr`) renders another language, `VF_CHROMIUM=<path>` uses a Chromium that is already installed.
