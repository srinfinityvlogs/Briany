"""All configuration comes from environment variables. No secrets in code."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Variables the bot cannot run without. Each may have alias names (the first
# one found wins), e.g. Vercel's Redis integration creates KV_REDIS_URL.
REQUIRED = {
    "telegram_bot_token": ("TELEGRAM_BOT_TOKEN",),
    "telegram_webhook_secret": ("TELEGRAM_WEBHOOK_SECRET",),
    "groq_api_key": ("GROQ_API_KEY",),
    "redis_url": ("REDIS_URL", "KV_REDIS_URL"),
}

SECRET_FIELDS = ("telegram_bot_token", "telegram_webhook_secret",
                 "groq_api_key", "gemini_api_key", "redis_url")


class ConfigError(RuntimeError):
    """Raised when required settings are missing or invalid."""


def _first(env: dict, names: tuple[str, ...]) -> str:
    for n in names:
        v = (env.get(n) or "").strip()
        if v:
            return v
    return ""


def _float(env: dict, name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from e


def _id_set(env: dict, name: str) -> frozenset[int]:
    raw = (env.get(name) or "").strip()
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise ConfigError(f"{name} must be comma-separated numbers, got {part!r}")
        out.add(int(part))
    return frozenset(out)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    groq_api_key: str = ""
    redis_url: str = ""
    gemini_api_key: str = ""
    gemini_model: str = ""                      # empty = auto-pick at runtime
    groq_model: str = "openai/gpt-oss-120b"
    allowed_telegram_ids: frozenset = field(default_factory=frozenset)
    nse_min_interval_seconds: float = 1.0       # throttle between NSE calls
    redis_max_memory_mb: float = 0.0            # Redis plan cap; 0 = unknown (Upstash does not report it)
    market: str = "IN"
    timezone: str = "Asia/Kolkata"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            telegram_bot_token=_first(env, REQUIRED["telegram_bot_token"]),
            telegram_webhook_secret=_first(env, REQUIRED["telegram_webhook_secret"]),
            groq_api_key=_first(env, REQUIRED["groq_api_key"]),
            redis_url=_first(env, REQUIRED["redis_url"]),
            gemini_api_key=_first(env, ("GEMINI_API_KEY",)),
            gemini_model=_first(env, ("GEMINI_MODEL",)),
            groq_model=_first(env, ("GROQ_MODEL",)) or "openai/gpt-oss-120b",
            allowed_telegram_ids=_id_set(env, "ALLOWED_TELEGRAM_IDS"),
            nse_min_interval_seconds=_float(env, "NSE_MIN_INTERVAL_SECONDS", 1.0),
            redis_max_memory_mb=_float(env, "REDIS_MAX_MEMORY_MB", 0.0),
            market=_first(env, ("MARKET",)) or "IN",
            timezone=_first(env, ("TIMEZONE",)) or "Asia/Kolkata",
            log_level=(_first(env, ("LOG_LEVEL",)) or "INFO").upper(),
        )

    def missing(self) -> list[str]:
        """Names of required environment variables that are not set."""
        return [REQUIRED[f][0] for f in REQUIRED if not getattr(self, f)]

    def validate(self) -> "Settings":
        miss = self.missing()
        if miss:
            raise ConfigError("Missing required environment variables: " + ", ".join(miss))
        return self

    def secret_values(self) -> list[str]:
        return [getattr(self, f) for f in SECRET_FIELDS if getattr(self, f)]

    def redacted(self) -> dict:
        """Safe to log: secrets shown only as set / not set."""
        out = {}
        for k, v in self.__dict__.items():
            if k in SECRET_FIELDS:
                out[k] = "set" if v else "NOT SET"
            elif k == "allowed_telegram_ids":
                out[k] = f"{len(v)} id(s)"
            else:
                out[k] = v
        return out
