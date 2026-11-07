import logging

import pytest

from briany.config import ConfigError, Settings
from briany.logging_setup import setup_logging

FULL = {
    "TELEGRAM_BOT_TOKEN": "123456:ABCDEF-token",
    "TELEGRAM_WEBHOOK_SECRET": "webhooksecret123",
    "GROQ_API_KEY": "gsk_testkey_123456",
    "KV_REDIS_URL": "rediss://default:pw@host:6379",
}


def test_missing_reported_by_name():
    s = Settings.from_env({})
    assert set(s.missing()) == {"TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET",
                                "GROQ_API_KEY", "REDIS_URL"}
    with pytest.raises(ConfigError):
        s.validate()


def test_redis_alias_and_defaults():
    s = Settings.from_env(FULL).validate()
    assert s.redis_url.startswith("rediss://")
    assert s.groq_model == "openai/gpt-oss-120b"
    assert s.timezone == "Asia/Kolkata" and s.market == "IN"
    assert s.nse_min_interval_seconds == 1.0


def test_allowlist_parsing_and_errors():
    s = Settings.from_env({**FULL, "ALLOWED_TELEGRAM_IDS": "11, 22,"})
    assert s.allowed_telegram_ids == frozenset({11, 22})
    with pytest.raises(ConfigError):
        Settings.from_env({**FULL, "ALLOWED_TELEGRAM_IDS": "abc"})
    with pytest.raises(ConfigError):
        Settings.from_env({**FULL, "NSE_MIN_INTERVAL_SECONDS": "fast"})


def test_redacted_hides_secrets():
    text = str(Settings.from_env(FULL).redacted())
    for secret in FULL.values():
        assert secret not in text


def test_logs_never_contain_secrets(caplog):
    s = Settings.from_env(FULL)
    log = setup_logging("INFO", s.secret_values())
    caplog.handler.addFilter(logging.getLogger().handlers[0].filters[0])
    with caplog.at_level(logging.INFO):
        log.info("token is %s", FULL["TELEGRAM_BOT_TOKEN"])
    assert FULL["TELEGRAM_BOT_TOKEN"] not in caplog.text


def test_redis_memory_cap_setting():
    assert Settings.from_env(FULL).redis_max_memory_mb == 0.0
    assert Settings.from_env({**FULL, "REDIS_MAX_MEMORY_MB": "30"}).redis_max_memory_mb == 30.0
    with pytest.raises(ConfigError):
        Settings.from_env({**FULL, "REDIS_MAX_MEMORY_MB": "lots"})


def test_retention_setting_default_and_floor():
    assert Settings.from_env(FULL).price_retention_days == 380
    assert Settings.from_env({**FULL, "PRICE_RETENTION_DAYS": "400"}).price_retention_days == 400
    with pytest.raises(ConfigError):
        Settings.from_env({**FULL, "PRICE_RETENTION_DAYS": "200"})
