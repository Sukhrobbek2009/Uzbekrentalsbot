"""Questions between a renter and a host, relayed by the two bots.

Neither side sees the other's chat, phone number or username: the bot passes the text along with the
person's name and a Reply button, so a thread can go back and forth for as long as they like.
  Renter bot: q:<listing id>                  asks the listing's host
  Host bot:   hr:<listing id>:<renter chat>   answers (or writes first to) that renter
"""
import html
import logging

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import db, labels, payments, roles

log = logging.getLogger("uzbekrentalsbot.chat")

TEXT = 0
MAX_LEN = 500
MAX_PER_HOUR = 15
text_only = filters.TEXT & ~filters.COMMAND & ~filters.Regex(labels.MENU_REGEX)


# ---------------------------------------------------------------- renter bot

async def ask_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    listing = db.get_listing(int(query.data.split(":")[1]))
    if listing is None or listing["status"] == "deleted":
        await query.message.reply_text("Sorry, that listing no longer exists.")
        return ConversationHandler.END
    context.user_data["ask_listing"] = listing["id"]
    await query.message.reply_text(
        f"\U0001F4AC Type your message for the host of <b>{html.escape(listing['location'])}</b> "
        f"(up to {MAX_LEN} characters), or /cancel.\nYour phone number isn't shared.",
        parse_mode=ParseMode.HTML,
    )
    return TEXT


async def ask_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = " ".join(update.message.text.split())
    if len(text) > MAX_LEN:
        await update.message.reply_text(f"Please keep it under {MAX_LEN} characters.")
        return TEXT
    chat_id = update.effective_chat.id
    if db.recent_renter_messages(chat_id) >= MAX_PER_HOUR:
        context.user_data.pop("ask_listing", None)
        await update.message.reply_text("\u23F3 You've sent a lot of messages. Please wait a while before sending more.")
        return ConversationHandler.END
    listing = db.get_listing(context.user_data.pop("ask_listing", 0))
    if listing is None or listing["status"] == "deleted":
        await update.message.reply_text("Sorry, that listing no longer exists.")
        return ConversationHandler.END
    renter = db.get_user(chat_id)
    db.add_message(listing["id"], chat_id, "renter", text)
    delivered = await payments.notify_host(
        listing["owner_chat_id"],
        f"\U0001F4AC <b>Message from {html.escape(renter['name'] if renter else 'a renter')}</b>\n"
        f"\U0001F4CD {html.escape(listing['location'])}\n\n{html.escape(text)}",
        InlineKeyboardMarkup([[payments.message_renter_button(listing["id"], chat_id, "\u21A9\uFE0F Reply")]]),
    )
    await update.message.reply_text(
        "\u2705 Sent! The host's answer will arrive here."
        if delivered
        else "\u26A0\uFE0F I saved your message but couldn't reach the host right now. Please try again later."
    )
    return ConversationHandler.END


async def ask_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("ask_listing", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


renter_conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(ask_start, pattern=r"^q:\d+$")],
    states={TEXT: [MessageHandler(text_only, ask_send)]},
    fallbacks=[CommandHandler("cancel", ask_cancel), MessageHandler(filters.Regex(labels.MENU_REGEX), ask_cancel)],
)


# ------------------------------------------------------------------ host bot

async def reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, lid, renter = query.data.split(":")
    listing = db.get_listing(int(lid))
    renter_user = db.get_user(int(renter))
    if listing is None or listing["owner_chat_id"] != query.message.chat_id or renter_user is None:
        await query.message.reply_text("I couldn't find that conversation.")
        return ConversationHandler.END
    context.user_data["reply_to"] = (listing["id"], int(renter))
    await query.message.reply_text(
        f"\u270D\uFE0F Write your message to {html.escape(renter_user['name'])} about "
        f"<b>{html.escape(listing['location'])}</b> (up to {MAX_LEN} characters), or /cancel.",
        parse_mode=ParseMode.HTML,
    )
    return TEXT


async def reply_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = " ".join(update.message.text.split())
    if len(text) > MAX_LEN:
        await update.message.reply_text(f"Please keep it under {MAX_LEN} characters.")
        return TEXT
    listing_id, renter = context.user_data.pop("reply_to")
    listing = db.get_listing(listing_id)
    host = db.get_user(update.effective_chat.id)
    db.add_message(listing_id, renter, "host", text)
    delivered = await payments._tell(
        roles.renter_bot(),
        renter,
        f"\U0001F4AC <b>Message from the host, {html.escape(host['name'] if host else 'host')}</b>\n"
        f"\U0001F4CD {html.escape(listing['location'])}\n\n{html.escape(text)}",
        InlineKeyboardMarkup([[payments.InlineKeyboardButton("\u21A9\uFE0F Reply", callback_data=f"q:{listing_id}")]]),
    )
    await update.message.reply_text(
        "\u2705 Sent to the renter."
        if delivered
        else "\u26A0\uFE0F I couldn't reach the renter. They may have blocked the bot."
    )
    return ConversationHandler.END


async def reply_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reply_to", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


host_conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(reply_start, pattern=r"^hr:\d+:\d+$")],
    states={TEXT: [MessageHandler(text_only, reply_send)]},
    fallbacks=[CommandHandler("cancel", reply_cancel), MessageHandler(filters.Regex(labels.MENU_REGEX), reply_cancel)],
)
