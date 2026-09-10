import asyncio
import logging
import socket
import sys
from urllib.parse import urlparse

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from bot.config import (
    BOT_TOKEN,
    IGNORED_CLOUD_MAX,
    LOCAL_API_FILE_MODE,
    TELEGRAM_API_URL,
    UPLOAD_TIMEOUT,
    USING_LOCAL_API,
)
from bot.downloader import ensure_downloads_dir, ffmpeg_available
from bot.handlers import help_cmd, on_cancel_job, on_quality, on_text, sites_cmd, start
from bot.logging_setup import setup_logging


def _ensure_local_api() -> None:
    if not USING_LOCAL_API or not TELEGRAM_API_URL:
        return
    parsed = urlparse(TELEGRAM_API_URL)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=3):
            return
    except OSError:
        sys.stderr.write(
            f"Локальный Bot API не отвечает на {TELEGRAM_API_URL}\n"
            "Бот сам по себе этот сервер не запускает. Сначала:\n"
            "  colima start   # если Docker ещё не запущен\n"
            "  docker-compose -f docker-compose.api.yml up -d\n"
            "Проверка: curl -sS http://127.0.0.1:8081 || docker-compose -f docker-compose.api.yml logs\n"
        )
        sys.exit(1)


async def _post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Как пользоваться"),
            BotCommand("help", "Подсказка"),
            BotCommand("sites", "Какие ссылки работают"),
        ]
    )


def _ensure_event_loop() -> None:
    # Python 3.14 no longer creates a loop in get_event_loop().
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def main() -> None:
    setup_logging()

    if not BOT_TOKEN or BOT_TOKEN.startswith("123456"):
        sys.stderr.write(
            "Укажите BOT_TOKEN в файле .env (токен от @BotFather).\n"
        )
        sys.exit(1)

    if IGNORED_CLOUD_MAX:
        logging.warning(
            "MAX_FILE_MB в .env больше 49, но TELEGRAM_API_URL пустой. "
            "Облако Telegram всё равно принимает только ~50 МБ. "
            "Для 200 МБ+ поднимите локальный Bot API (docker-compose.api.yml)."
        )

    if not ffmpeg_available():
        logging.warning(
            "ffmpeg не найден на этой машине. "
            "Без него часто доступен только ~360p. В Docker он уже входит в образ."
        )

    ensure_downloads_dir()
    _ensure_event_loop()

    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .pool_timeout(30)
        .read_timeout(UPLOAD_TIMEOUT)
        .write_timeout(UPLOAD_TIMEOUT)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
    )
    if USING_LOCAL_API:
        logging.info("Using Bot API at %s (uploads up to ~2 GB)", TELEGRAM_API_URL)
        _ensure_local_api()
        builder = builder.base_url(f"{TELEGRAM_API_URL}/bot").base_file_url(
            f"{TELEGRAM_API_URL}/file/bot"
        )
        if LOCAL_API_FILE_MODE:
            builder = builder.local_mode(True)

    app = builder.post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("sites", sites_cmd))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(on_cancel_job, pattern=r"^x:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logging.info("Bot started")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
