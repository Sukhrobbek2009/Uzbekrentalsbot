import logging
import os
import sys

from telegram import Update
from bot.api_client import check_health
from bot import account, booking, chat, db, listing_flow, manage, menu, panels, payments, reviews, roles
from bot.logging_setup import setup_logging
from bot.search import handlers as search_handlers
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    TypeHandler,
    CommandHandler,
    ContextTypes,
)

log = logging.getLogger("uzbekrentalsbot")

PLACEHOLDERS = {
    "link": "Link my account is coming soon.",
}


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(menu.HELP)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(PLACEHOLDERS.get(query.data, "Unknown action."))


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log that an update arrived (chat id and kind only, never message text)."""
    kind = "callback" if update.callback_query else "message" if update.message else "other"
    chat = update.effective_chat.id if update.effective_chat else None
    log.info("update: %s from chat %s", kind, chat)
    # Forwarding a channel post to the bot reveals the channel's id (useful for CHANNEL_ID).
    origin = getattr(update.message, "forward_origin", None) if update.message else None
    origin_chat = getattr(origin, "chat", None)
    if origin_chat is not None:
        log.info("forwarded from channel/chat id %s", origin_chat.id)


async def on_startup(app: Application) -> None:
    db.init_db()
    await check_health()


def build_app(token: str) -> Application:
    app = (
        Application.builder()
        .token(token)
        .connect_timeout(20)
        .read_timeout(20)
        .post_init(on_startup)
        .build()
    )
    app.add_handler(TypeHandler(Update, log_update), group=-1)
    app.add_handler(menu.registration)
    app.add_handler(CommandHandler("help", help_command))
    # Conversations come before the menu buttons, so tapping a menu button during one
    # ends it (its fallback) instead of running the button and leaving the conversation open.
    if roles.is_host():
        app.add_handler(listing_flow.conversation)
        app.add_handler(chat.host_conversation)
        for handler in manage.handlers + payments.host_handlers:
            app.add_handler(handler)
    else:
        app.add_handler(reviews.conversation)
        app.add_handler(chat.renter_conversation)
        for handler in account.handlers + booking.handlers + search_handlers:
            app.add_handler(handler)
    for handler in panels.handlers:
        app.add_handler(handler)
    for handler in menu.handlers:
        app.add_handler(handler)
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(menu.fallback)
    return app


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        sys.exit("BOT_TOKEN environment variable is not set.")
    setup_logging(token, os.environ.get("HOST_BOT_TOKEN"), os.environ.get("RENTER_BOT_TOKEN"))
    app = build_app(token)
    log.info("Starting polling as the %s bot...", roles.ROLE)
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
