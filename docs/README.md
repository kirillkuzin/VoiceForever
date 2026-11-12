# Running VoiceForever yourself

These guides are for hosting VoiceForever on your own server with your own keys. If you just want to play, start from the [main page](../README.md).

## The parts

```
Telegram voice message ──────────────┐
gamepad button + PC microphone ──────┴→ server: speech recognition → Claude picks the channel and fixes the text
  (recorded by the Windows app)        → WebSocket → Windows app on your PC → addon in WoW → chat
```

- **Server** (`server/`): the Telegram bot, speech recognition through any OpenAI-compatible API, Claude to decide where a phrase goes and to fix punctuation, and a WebSocket for the Windows app. Ships as a Docker image.
- **Windows app**, also called the agent (`agent/`): stays connected to the server and types messages into the game window. On a gamepad button it records the microphone and sends the recording to the server. It never reads game memory.
- **Addon** (`addon/VoiceForever`): an invisible input field in the game. The chat window never opens; the addon picks party, raid or instance chat by itself and replies to the last whisper. It shows a recording badge in the game.

## Guides

1. [Server](server.md): run it on a VPS with Docker, use your own Whisper, put TLS in front, pick the speech recognition and the Claude model.
2. [Addon](addon.md): install it into WoW Forever, slash commands.
3. [Windows app](agent.md): install and set up, voice from the gamepad, tray, settings file, running from source.
4. [Telegram bot](telegram-bot.md): commands and widgets.
5. [Development](development.md): layout, tests, builds, screenshots.
6. [Access API](access-api.md): accounts, plans and limits from a service of your own instead of `VF_USERS`.

## Downloads

- Server: Docker image `ghcr.io/kirillkuzin/voiceforever-server`.
- Windows app and addon: `VoiceForeverAgent.exe` and `VoiceForever-addon-<version>.zip` on [Releases](https://github.com/kirillkuzin/VoiceForever/releases).

## Quick start

1. **Server.** Download `docker-compose.yml` and `.env.example` (as `.env`) to your VPS. In `.env`, fill in the bot token from [@BotFather](https://t.me/BotFather), `VF_USERS` (`<Telegram ID>:<agent token>`), your Claude key and the speech recognition settings. Run `docker compose pull && docker compose up -d`, then put TLS (for example Caddy) in front and use `wss://<domain>/ws`. Details: [server.md](server.md).
2. **Addon.** Copy `VoiceForever` into `Interface\AddOns` of WoW Forever. Details: [addon.md](addon.md).
3. **Windows app.** Run `VoiceForeverAgent.exe`; the first-run setup asks for the server address and the agent token, then the microphone and the gamepad button, and checks the button in the game. Details: [agent.md](agent.md).

## Safety and the game's rules

- The app presses keys only when the WoW window is active, and only for a message you said. It does not read or change game memory and automates nothing in gameplay.
- The server and the app strip `|` and a leading `/` or `.` from the text, so a voice message cannot run a game command.
- The bot answers only users in `VF_USERS`; the app connects with a secret token.
- Blizzard's rules forbid automating gameplay. Typing what you said is close to Windows voice typing, but there is no official permission for tools like this. Use it at your own risk.
