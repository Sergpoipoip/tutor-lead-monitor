"""The only module importing the Telegram client library."""

import asyncio
import logging
from datetime import timedelta

import httpx
from sqlalchemy import Engine
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.error import (
    BadRequest,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
)
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

from tutor_lead_monitor.application.bot import OwnerAccess, callback, command
from tutor_lead_monitor.config import AppConfig
from tutor_lead_monitor.notifications.base import DeliveryError, FailureKind, Message

logger = logging.getLogger(__name__)


def classify_error(error: Exception) -> DeliveryError:
    if isinstance(error, RetryAfter):
        seconds = error.retry_after
        return DeliveryError(
            FailureKind.RATE_LIMITED,
            retry_after=seconds.total_seconds()
            if isinstance(seconds, timedelta)
            else float(seconds),
        )
    if isinstance(error, (Forbidden, InvalidToken)):
        return DeliveryError(FailureKind.AUTHORIZATION)
    if isinstance(error, BadRequest):
        return DeliveryError(FailureKind.MALFORMED)
    if isinstance(error, (NetworkError, TimeoutError)):
        # Only connection/pool failures prove no request reached Telegram.
        cause = error.__cause__
        if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return DeliveryError(FailureKind.RETRYABLE)
        return DeliveryError(FailureKind.AMBIGUOUS)
    if isinstance(error, TelegramError):
        return DeliveryError(FailureKind.PERMANENT)
    return DeliveryError(FailureKind.AMBIGUOUS)


class TelegramNotifier:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, recipient: int, message: Message) -> str:
        keyboard = None
        if message.buttons:
            buttons = [InlineKeyboardButton(b.label, callback_data=b.data) for b in message.buttons]
            keyboard = InlineKeyboardMarkup([buttons[i : i + 4] for i in range(0, len(buttons), 4)])
        try:
            async with asyncio.timeout(45):
                sent = await self.bot.send_message(
                    chat_id=recipient,
                    text=message.html,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    connect_timeout=10,
                    read_timeout=20,
                    write_timeout=20,
                    pool_timeout=10,
                )
            return str(sent.message_id)
        except Exception as error:
            raise classify_error(error) from None

    async def answer(self, callback_id: str, text: str) -> None:
        try:
            await self.bot.answer_callback_query(
                callback_id,
                text=text,
                connect_timeout=10,
                read_timeout=20,
                write_timeout=20,
                pool_timeout=10,
            )
        except Exception as error:
            raise classify_error(error) from None


def run_bot(engine: Engine, config: AppConfig, token: str, access: OwnerAccess) -> None:
    app = (
        ApplicationBuilder()
        .token(token)
        .connect_timeout(10)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(10)
        .get_updates_connect_timeout(10)
        .get_updates_read_timeout(35)
        .get_updates_write_timeout(10)
        .build()
    )
    notifier = TelegramNotifier(app.bot)

    async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, chat, message = update.effective_user, update.effective_chat, update.effective_message
        if user and chat and message and message.text:
            name = message.text.split()[0].split("@")[0].removeprefix("/")
            await command(engine, config, access, notifier, actor=user.id, chat=chat.id, name=name)

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query:
            await callback(
                engine,
                access,
                notifier,
                actor=query.from_user.id,
                chat=query.message.chat.id if query.message else 0,
                callback_id=query.id,
                data=query.data if isinstance(query.data, str) else "",
            )

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("telegram_update_failed")

    app.add_handler(
        CommandHandler(["start", "help", "status", "digest", "pause", "resume"], on_command)
    )
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(
        poll_interval=1,
        timeout=30,
        allowed_updates=["message", "callback_query"],
        bootstrap_retries=0,
    )
