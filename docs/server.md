# Running the server

The VoiceForever server (Telegram bot, speech recognition, Claude and the WebSocket for the Windows app) ships as a ready-made Docker image:

```
ghcr.io/kirillkuzin/voiceforever-server
```

| Tag | What it is |
|---|---|
| `latest` | the latest version from the `master` branch |
| `0.2.0`, `0.2` | releases, from `v*` tags |
| `sha-<commit>` | a specific commit |

GitHub Actions builds the image after the tests pass ([server-image.yml](../.github/workflows/server-image.yml)), for `linux/amd64` only.

## What you need

- a VPS with Docker and Compose v2 (`docker compose version`);
- a bot token from [@BotFather](https://t.me/BotFather);
- a Claude API key from [console.anthropic.com](https://console.anthropic.com);
- speech recognition: an OpenAI API key or your own Whisper with an OpenAI-compatible API;
- for the Windows app: a domain for `wss://`, or an SSH tunnel from your PC (a tunnel is enough for testing).

## 1. Files

The VPS only needs `docker-compose.yml` and `.env`:

```
mkdir -p ~/voiceforever && cd ~/voiceforever
curl -fsSLO https://raw.githubusercontent.com/kirillkuzin/VoiceForever/master/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/kirillkuzin/VoiceForever/master/.env.example
```

While the repository is private, `curl` gets a 404. In that case `git clone` it (GitHub will ask you to sign in) and work inside the repository folder.

## 2. Access to the image while it is private

The image of a private repository is private too, so Docker has to sign in to GitHub:

1. GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token, with the `read:packages` scope.
2. On the VPS:

   ```
   echo <token> | docker login ghcr.io -u <GitHub login> --password-stdin
   ```

Once the repository is public, make the image public as well: the package page on GitHub → Package settings → Change visibility → Public. After that no sign-in is needed.

## 3. Configure `.env`

Required variables:

| Variable | What to put there |
|---|---|
| `VF_TELEGRAM_TOKEN` | the bot token from BotFather; leave it empty to run without the bot, the Windows app only |
| `VF_USERS` | `<Telegram ID>:<agent token>`; make a token with `python3 -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `ANTHROPIC_API_KEY` | your Claude API key |
| `VF_STT_BASE_URL`, `VF_STT_API_KEY`, `VF_STT_MODEL` | speech recognition, OpenAI `gpt-4o-mini-transcribe` by default |

You learn your Telegram ID from the bot itself, so start with `VF_USERS=1:<agent token>`, run the server, send `/start` to the bot and replace `1` with the number from its answer.

Everything else is explained in the comments in `.env`: the Claude model, known character names, the message length limit, the longest gamepad recording (`VF_MAX_VOICE_S`), messages per player per minute (`VF_RATE_PER_MIN`).

To serve many players with accounts and limits, replace `VF_USERS` with a service of your own: [access-api.md](access-api.md).

### Your own Whisper

Inside the container `127.0.0.1` is the container itself, so the Whisper address depends on how Whisper runs. Check `docker ps`:

- The port is published on the host (`0.0.0.0:8000->8000/tcp`): `VF_STT_BASE_URL=http://host.docker.internal:8000/v1`.
- The port is not published, or only on `127.0.0.1`: attach VoiceForever to the Whisper network. `docker inspect <Whisper container>` shows the network name; uncomment the `networks` block in `docker-compose.yml` and set `VF_STT_BASE_URL=http://<container name>:<port>/v1`.

Leave `VF_STT_API_KEY` empty and take `VF_STT_MODEL` from `curl http://<address>/v1/models`. Whisper needs an OpenAI-compatible `POST /v1/audio/transcriptions` (speaches, faster-whisper-server, LocalAI, vLLM).

## 4. Start

```
docker compose pull
docker compose up -d
docker compose logs -f
```

Check that it works:

- `curl http://127.0.0.1:8765/health` answers `{"ok": true, "agents_online": 0}`;
- after half a minute `docker compose ps` shows `healthy`;
- the bot answers `/start` with the setup checklist (if your Telegram app does not show widgets, send `/widgets off`).

After editing `.env`, restart: `docker compose up -d --force-recreate`.

## 5. Connect the Windows app

Port 8765 is open only on the server's `127.0.0.1`. Do not expose it to the internet.

**For testing**, an SSH tunnel from the gaming PC is enough (PowerShell, keep the window open):

```
ssh -N -L 8765:127.0.0.1:8765 <user>@<VPS address>
```

In the app's settings (or `config.toml`): `server_url = "ws://127.0.0.1:8765/ws"`.

**For everyday use**, put TLS in front of the server, for example [Caddy](https://caddyserver.com/docs/install) on the host. It gets a certificate for your domain by itself:

```
vf.example.com {
    reverse_proxy /ws 127.0.0.1:8765
}
```

In the app's settings: `server_url = "wss://vf.example.com/ws"`.

## Speech recognition

Any service with an OpenAI-compatible `/audio/transcriptions` works:

| Option | `VF_STT_BASE_URL` | `VF_STT_MODEL` |
|---|---|---|
| OpenAI (default) | `https://api.openai.com/v1` | `gpt-4o-mini-transcribe` or `gpt-4o-transcribe` |
| Groq | `https://api.groq.com/openai/v1` | `whisper-large-v3-turbo` |
| Your own server (speaches / faster-whisper) | `http://speaches:8000/v1` | `Systran/faster-whisper-large-v3` |

`VF_STT_LANGUAGE` locks recognition to one language (a code such as `en` or `ru`); when it is empty, the model detects the spoken language. `VF_STT_PROMPT` is a hint with words that recognition often gets wrong, such as game slang or character names.

The hint also gets up to 30 nicknames: the player's `/names`, then the likeliest ones from the game (the people they whisper with, their group, guild and friends online) and the names of their chat channels. Whisper models read only about 224 tokens of hint, so the list stays short. The nicknames from the game live in the server's memory only: a restart forgets them until the Windows app sends them again, and the log shows only how many there are.

## Claude

Claude picks the chat channel, removes the routing words and fixes punctuation. The default model is `claude-opus-5-5` with `effort=low`, with server-side fallbacks in case the model refuses. If Claude is unavailable or answers in the wrong format, a simple keyword parser takes over (it knows Russian phrases such as «в пати…», «шепни…»), so the message still goes out.

Change the model with `VF_LLM_MODEL`. For a hosted service it is worth comparing the cost and latency of `claude-haiku-5-5`. `VF_ALLOW_TRANSFORM=false` turns off message rewriting (`/style`).

## Updating

```
docker compose pull && docker compose up -d
```

User settings (`/channel`, `/confirm`, `/style`, `/names`) live in the `vf-data` volume and survive updates. To pin a version, set `VF_IMAGE=ghcr.io/kirillkuzin/voiceforever-server:0.2` in `.env`.

## Building from source

From the repository root:

```
docker build -t ghcr.io/kirillkuzin/voiceforever-server:latest ./server
docker compose up -d
```

The build takes a few minutes: pydantic-core, in the version aiogram allows, has no prebuilt packages for Python 3.15, so the Dockerfile compiles it from Rust sources.

Without Docker: `cd server && uv run --env-file ../.env voiceforever-server`. For the same reason the machine needs Rust 1.88 or newer ([rustup](https://rustup.rs)) and `gcc`. Outside Docker also add `VF_DB_PATH=./voiceforever.db` and `VF_WS_HOST=127.0.0.1` to `.env`.

## Security

- The bot answers only users listed in `VF_USERS` (or known to the access service); each Windows app connects with its own secret token.
- The server and the app strip `|` and a leading `/` or `.` from every message, so a voice message cannot run a game command.
