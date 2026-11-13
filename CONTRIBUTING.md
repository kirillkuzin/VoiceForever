# Contributing

Bug reports, ideas and pull requests are welcome.

## Issues

- Search the [existing issues](https://github.com/kirillkuzin/VoiceForever/issues) first.
- For a bug, name the part (Windows app, addon, server or Telegram bot) and the versions, and say what you did and what happened. The app's log is `agent.log` in `%APPDATA%\VoiceForever`; remove your token before you paste it.
- Vulnerabilities go privately: see [SECURITY.md](SECURITY.md).

## Pull requests

- One change per pull request. For anything bigger than a fix, open an issue first.
- Setup, tests and builds: [docs/development.md](docs/development.md). Run the tests of every part you changed (`server/`, `agent/`, `addon/`).
- Text the player sees lives in the locale files: `agent/vf_agent/ui/locales/`, `server/vf_server/locales/`, `addon/VoiceForever/Locales.lua`. Add each new string in all five languages (en, ru, de, es, fr); the tests check this.
- If you change the window or a Telegram widget, [render the screenshots](docs/development.md#screenshots) again.
- If you change behaviour or settings, update the matching page in [docs](docs/README.md) and `.env.example`.
- Code, comments and docs are in English.
- Leave the version numbers alone: they change at release time.

## License

By sending a pull request you agree that your contribution is licensed under [AGPL-3.0-only](LICENSE), like the rest of VoiceForever.
