import asyncio
import logging
import os
import sys

from telegram.ext import ApplicationBuilder

import state_store
from cleanup import cleanup_loop
from handlers import register_handlers
from i18n import t
from logging_setup import configure_logging
from koyura_client import validate_startup_config
from subscription_sweep import subscription_sweep_loop

# Keeps httpx request lines (which contain the bot token) and any other
# accidental secret out of the logs; see bot/logging_setup.py.
configure_logging()
logger = logging.getLogger(__name__)

# Telegram's command menu is **per bot**, not per chat, so it is published once in
# the source language. The words still live in the catalogue (``menu.*``) so the
# menu and the answers cannot drift apart.
MENU = (
    ("start", "menu.start"),
    ("help", "menu.help"),
    ("mp3", "menu.mp3"),
    ("mp4", "menu.mp4"),
    ("best", "menu.best"),
    ("platforms", "menu.platforms"),
    ("settings", "menu.settings"),
    ("setquality", "menu.setquality"),
    ("setformat", "menu.setformat"),
    ("stats", "menu.stats"),
)


def menu_commands(lang: str = "en") -> list[tuple[str, str]]:
    """``(command, description)`` pairs, as ``setMyCommands`` wants them."""
    return [(name, t(key, lang)) for name, key in MENU]


async def wait_for_bot_api(url: str, max_wait: int = 60):
    """Wait for the self-hosted Bot API server to become reachable."""
    import httpx
    for i in range(max_wait):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=2)
                logger.info("Bot API server ready (attempt %d)", i + 1)
                return
        except Exception:
            if i % 5 == 0:
                logger.info("Waiting for Bot API server at %s... (attempt %d)", url, i + 1)
            await asyncio.sleep(1)
    logger.warning("Bot API server not reachable after %ds, starting anyway", max_wait)


def main():
    bot_token = os.environ.get("BOT_TOKEN")
    if not bot_token:
        logger.error("BOT_TOKEN environment variable is required")
        sys.exit(1)

    validate_startup_config()
    state_store.init()

    api_url = os.environ.get("TELEGRAM_BOT_API_URL", "http://telegram-bot-api:8081")
    base_url = f"{api_url}/bot"
    base_file_url = f"{api_url}/file/bot"

    logger.info("Starting bot with API server: %s", api_url)

    asyncio.run(wait_for_bot_api(api_url))

    app = (
        ApplicationBuilder()
        .token(bot_token)
        .base_url(base_url)
        .base_file_url(base_file_url)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .build()
    )

    register_handlers(app)

    async def post_init(application):
        asyncio.create_task(cleanup_loop())
        # The nightly subscription sweep: re-checks the users the panel reports
        # as active, so a lapsed subscription stops being trusted after a day.
        # It is a no-op when the gate is off, and it starts ten minutes late so
        # the panel is up before the first check.
        asyncio.create_task(subscription_sweep_loop(application.bot))
        from telegram import BotCommand
        await application.bot.set_my_commands(
            [BotCommand(name, description) for name, description in menu_commands()]
        )
        logger.info("Bot commands registered")

    app.post_init = post_init
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
