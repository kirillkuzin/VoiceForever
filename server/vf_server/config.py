"""Server settings, read from environment variables (see .env.example)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self


def _bool(value: str | None, default: bool) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_users(raw: str) -> frozendict[int, str]:
    """Parse VF_USERS="telegram_id:agent_token,telegram_id:agent_token"."""
    users: dict[int, str] = {}
    for item in filter(None, (i.strip() for i in raw.split(","))):
        tg_id, sep, token = item.partition(":")
        if not sep or not tg_id.strip().isdigit() or len(token.strip()) < 16:
            raise ValueError(
                "VF_USERS entries must look like 123456789:<agent token of 16+ chars>"
            )
        users[int(tg_id.strip())] = token.strip()
    return frozendict(users)


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    telegram_token: str
    users: frozendict[int, str]  # telegram user id -> agent token

    # Speech-to-text: any OpenAI-compatible /audio/transcriptions endpoint
    # (OpenAI, Groq, or a self-hosted faster-whisper server such as speaches).
    stt_base_url: str = "https://api.openai.com/v1"
    stt_api_key: str = ""
    stt_model: str = "gpt-4o-mini-transcribe"
    stt_language: str = "ru"
    stt_prompt: str = ""

    # LLM router (Claude). The SDK reads ANTHROPIC_API_KEY itself.
    llm_enabled: bool = True
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "low"
    llm_timeout_s: float = 20.0

    # Rewriting the message (style, translation, role-play). Off = only punctuation fixes.
    allow_transform: bool = True

    max_message_bytes: int = 255
    ws_host: str = "0.0.0.0"
    ws_port: int = 8765
    ack_timeout_s: float = 30.0
    db_path: str = "/data/voiceforever.db"
    known_names: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Self:
        if not (token := env.get("VF_TELEGRAM_TOKEN", "")):
            raise ValueError("VF_TELEGRAM_TOKEN is required")
        if not (users := parse_users(env.get("VF_USERS", ""))):
            raise ValueError("VF_USERS is required, e.g. 123456789:long-random-agent-token")
        defaults = cls(telegram_token="", users=frozendict())
        return cls(
            telegram_token=token,
            users=users,
            stt_base_url=env.get("VF_STT_BASE_URL", defaults.stt_base_url).rstrip("/"),
            stt_api_key=env.get("VF_STT_API_KEY", ""),
            stt_model=env.get("VF_STT_MODEL", defaults.stt_model),
            stt_language=env.get("VF_STT_LANGUAGE", defaults.stt_language),
            stt_prompt=env.get("VF_STT_PROMPT", ""),
            llm_enabled=_bool(env.get("VF_LLM_ENABLED"), True),
            llm_model=env.get("VF_LLM_MODEL", defaults.llm_model),
            llm_effort=env.get("VF_LLM_EFFORT", defaults.llm_effort),
            llm_timeout_s=float(env.get("VF_LLM_TIMEOUT_S", defaults.llm_timeout_s)),
            allow_transform=_bool(env.get("VF_ALLOW_TRANSFORM"), True),
            max_message_bytes=int(env.get("VF_MAX_MESSAGE_BYTES", defaults.max_message_bytes)),
            ws_host=env.get("VF_WS_HOST", defaults.ws_host),
            ws_port=int(env.get("VF_WS_PORT", defaults.ws_port)),
            ack_timeout_s=float(env.get("VF_ACK_TIMEOUT_S", defaults.ack_timeout_s)),
            db_path=env.get("VF_DB_PATH", defaults.db_path),
            known_names=tuple(
                n.strip() for n in env.get("VF_KNOWN_NAMES", "").split(",") if n.strip()
            ),
        )
