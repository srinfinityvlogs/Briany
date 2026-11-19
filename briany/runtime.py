# SPDX-License-Identifier: Apache-2.0
"""Builds the Telegram handler from environment variables. Used by the web app (app.py); nothing here prints a secret."""
from __future__ import annotations

from briany.config import ConfigError, Settings
from briany.logging_setup import setup_logging


def build_handler(env=None, client=None, api_post=None):
    """Settings -> Redis -> instrument list -> Bot -> Handler. `client` and `api_post` exist so tests can inject fakes."""
    from briany.bot import Bot
    from briany.evidence import Assembler
    from briany.llm import Chain, GeminiClient, GroqClient
    from briany.news import NewsClient
    from briany.nse import NseClient, RedisCache
    from briany.store import RedisStore
    from briany.symbols import Resolver
    from briany.telegram_bot import Handler, State, TelegramApi, default_post

    settings = Settings.from_env(env).validate()
    setup_logging(settings.log_level, settings.secret_values())
    if client is None:
        from briany.ingest import redis_url_problem, redis_url_structure_problem
        problem = redis_url_problem(settings.redis_url) or redis_url_structure_problem(settings.redis_url)
        if problem:
            raise ConfigError(problem)                                   # the message never contains the value
        import redis
        client = redis.Redis.from_url(settings.redis_url, decode_responses=True,
                                      socket_timeout=10, socket_connect_timeout=5)
    store = RedisStore(client)
    _day, instruments = store.get_instruments()
    if not instruments:
        raise ConfigError("no instrument list stored in Redis; run ingest-prices with refresh_instruments = yes")
    resolver = Resolver(instruments)
    providers = [GroqClient(settings.groq_api_key, model=settings.groq_model)]
    if settings.gemini_api_key:
        providers.append(GeminiClient(settings.gemini_api_key, model=settings.gemini_model))
    cache = RedisCache(client)
    assembler = Assembler(store, NseClient(cache=cache, min_interval=settings.nse_min_interval_seconds),
                          NewsClient(cache=cache), resolver)
    bot = Bot(assembler, Chain(providers), resolver)
    state = State(client, context_ttl=settings.context_ttl_seconds, per_minute=settings.rate_limit_per_minute,
                  per_day=settings.rate_limit_per_day)
    api = TelegramApi(settings.telegram_bot_token, post=api_post or default_post)
    return Handler(bot, api, state, settings.allowed_telegram_ids)
