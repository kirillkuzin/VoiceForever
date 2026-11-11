# Запуск сервера

Сервер VoiceForever (Telegram-бот, распознавание речи, Claude и WebSocket для агента) выходит готовым Docker-образом:

```
ghcr.io/kirillkuzin/voiceforever-server
```

| Тег | Что это |
|---|---|
| `latest` | последняя версия из ветки `master` |
| `0.2.0`, `0.2` | релизы по тегам `v*` |
| `sha-<коммит>` | конкретный коммит |

Образ собирает GitHub Actions ([server-image.yml](../.github/workflows/server-image.yml)) после тестов, только для `linux/amd64`.

## Что понадобится

- VPS с Docker и Compose v2 (`docker compose version`);
- токен бота от [@BotFather](https://t.me/BotFather);
- ключ Claude из [console.anthropic.com](https://console.anthropic.com);
- распознавание речи: ключ OpenAI или свой Whisper с OpenAI-совместимым API;
- для агента: домен под `wss://` или SSH-туннель с ПК (для проверки хватит туннеля).

## 1. Файлы

На VPS нужны только `docker-compose.yml` и `.env`:

```
mkdir -p ~/voiceforever && cd ~/voiceforever
curl -fsSLO https://raw.githubusercontent.com/kirillkuzin/VoiceForever/master/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/kirillkuzin/VoiceForever/master/.env.example
```

Пока репозиторий приватный, `curl` получит 404. Тогда сделай `git clone` (GitHub попросит войти) и работай в папке репозитория.

## 2. Доступ к образу, пока он приватный

Образ приватного репозитория тоже приватный, и Docker должен войти в GitHub:

1. GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token, право `read:packages`.
2. На VPS:

   ```
   echo <токен> | docker login ghcr.io -u <логин GitHub> --password-stdin
   ```

Когда репозиторий станет публичным, открой и образ: страница пакета на GitHub → Package settings → Change visibility → Public. После этого вход не нужен.

## 3. Настройка `.env`

Обязательные переменные:

| Переменная | Что указать |
|---|---|
| `VF_TELEGRAM_TOKEN` | токен бота от BotFather |
| `VF_USERS` | `<Telegram ID>:<токен агента>`; токен: `python3 -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `ANTHROPIC_API_KEY` | ключ Claude |
| `VF_STT_BASE_URL`, `VF_STT_API_KEY`, `VF_STT_MODEL` | распознавание речи, по умолчанию OpenAI `gpt-4o-mini-transcribe` |

Свой Telegram ID ты узнаешь от бота, поэтому сначала поставь `VF_USERS=1:<токен агента>`, запусти сервер, напиши боту `/start` и замени `1` на число из ответа.

Остальное описано в комментариях `.env`: модель Claude, известные ники, лимит длины сообщения, самая длинная запись с геймпада (`VF_MAX_VOICE_S`).

### Свой Whisper

Внутри контейнера `127.0.0.1` означает сам контейнер, поэтому адрес Whisper зависит от того, как он запущен. Посмотри `docker ps`:

- Порт опубликован на хост (`0.0.0.0:8000->8000/tcp`): `VF_STT_BASE_URL=http://host.docker.internal:8000/v1`.
- Порт не опубликован или опубликован только на `127.0.0.1`: подключи VoiceForever к сети Whisper. Имя сети покажет `docker inspect <контейнер Whisper>`, раскомментируй блок `networks` в `docker-compose.yml` и укажи `VF_STT_BASE_URL=http://<имя контейнера>:<порт>/v1`.

`VF_STT_API_KEY` оставь пустым, `VF_STT_MODEL` возьми из `curl http://<адрес>/v1/models`. Нужен OpenAI-совместимый `POST /v1/audio/transcriptions` (speaches, faster-whisper-server, LocalAI, vLLM).

## 4. Запуск

```
docker compose pull
docker compose up -d
docker compose logs -f
```

Проверка:

- `curl http://127.0.0.1:8765/health` отвечает `{"ok": true, "agents_online": 0}`;
- `docker compose ps` через полминуты показывает `healthy`;
- бот отвечает на `/start`.

После правки `.env` перезапусти: `docker compose up -d --force-recreate`.

## 5. Подключение агента

Порт 8765 открыт только на `127.0.0.1` сервера, наружу его не выставляй.

**Для проверки** хватит SSH-туннеля с игрового ПК (PowerShell, окно не закрывать):

```
ssh -N -L 8765:127.0.0.1:8765 <пользователь>@<адрес VPS>
```

В `config.toml` агента: `server_url = "ws://127.0.0.1:8765/ws"`.

**Постоянно** поставь перед сервером TLS, например [Caddy](https://caddyserver.com/docs/install) на хосте. Он сам получит сертификат для домена:

```
vf.example.com {
    reverse_proxy /ws 127.0.0.1:8765
}
```

В `config.toml` агента: `server_url = "wss://vf.example.com/ws"`.

## Обновление

```
docker compose pull && docker compose up -d
```

Настройки пользователей (`/channel`, `/confirm`, `/style`, `/names`) хранятся в томе `vf-data` и переживают обновление. Чтобы закрепить версию, поставь в `.env` `VF_IMAGE=ghcr.io/kirillkuzin/voiceforever-server:0.2`.

## Сборка из исходников

Из корня репозитория:

```
docker build -t ghcr.io/kirillkuzin/voiceforever-server:latest ./server
docker compose up -d
```

Сборка занимает несколько минут: у pydantic-core для версии, которую разрешает aiogram, нет готовых пакетов под Python 3.15, и Dockerfile компилирует его из исходников на Rust.

Без Docker: `cd server && uv run --env-file ../.env voiceforever-server`. По той же причине на машине нужны Rust 1.88 или новее ([rustup](https://rustup.rs)) и `gcc`. Для запуска вне Docker добавь в `.env` `VF_DB_PATH=./voiceforever.db` и `VF_WS_HOST=127.0.0.1`.
