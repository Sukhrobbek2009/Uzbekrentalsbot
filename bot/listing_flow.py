import html
import logging
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import db, labels

log = logging.getLogger("uzbekrentalsbot.listing")

PHOTO, LOCATION, PHONE, PRICE = range(4)
PHONE_RE = re.compile(r"^\+?\d[\d ()-]{6,18}$")


def channel_id() -> str | None:
    return os.environ.get("CHANNEL_ID", "").strip() or None


async def list_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Entered from /list or from the "Post a listing" button shown to hosts.
    if update.callback_query:
        await update.callback_query.answer()
    message = update.effective_message
    if update.effective_chat.type != "private":
        await message.reply_text("Please message me privately to post a listing.")
        return ConversationHandler.END
    if not channel_id():
        await message.reply_text("Posting isn't set up yet (CHANNEL_ID is not configured).")
        return ConversationHandler.END
    context.user_data["new_listing"] = {}
    await message.reply_text("Let's post your listing. First, send a photo. (Send /cancel to stop.)")
    return PHOTO


async def got_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_listing"]["photo"] = update.message.photo[-1].file_id
    await update.message.reply_text("Great. Now send the location (for example: Seattle, WA).")
    return LOCATION


async def need_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Please send a photo (as a picture, not a file).")
    return PHOTO


async def got_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not 2 <= len(text) <= 160:
        await update.message.reply_text("Please send a location between 2 and 160 characters.")
        return LOCATION
    context.user_data["new_listing"]["location"] = text
    await update.message.reply_text(
        "Now send a contact phone number. Note: it will be visible to everyone in the channel."
    )
    return PHONE


async def got_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not PHONE_RE.match(text):
        await update.message.reply_text("That doesn't look like a phone number. Example: +1 206 555 0100")
        return PHONE
    context.user_data["new_listing"]["phone"] = text
    await update.message.reply_text("Last: the price per night/day, as a number (for example: 120).")
    return PRICE


async def got_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.message.text.strip().replace(",", "."))
    except ValueError:
        price = 0
    if not 0 < price <= 1_000_000:
        await update.message.reply_text("Please send a price as a positive number, for example: 120")
        return PRICE

    data = context.user_data.pop("new_listing")
    listing_id = db.add_listing(update.effective_chat.id, data["photo"], data["location"], data["phone"], price)
    try:
        sent = await post_to_channel(context, listing_id)
    except TelegramError as e:
        log.error("Could not post listing %s to channel: %s", listing_id, e)
        await update.message.reply_text(
            "Your listing was saved, but I couldn't post it to the channel. "
            "Make sure the bot is an admin of the channel, then try again."
        )
        return ConversationHandler.END
    db.set_channel_message(listing_id, sent.message_id)
    await update.message.reply_text("Done! Your listing is live in the channel.")
    return ConversationHandler.END


def caption_for(listing) -> str:
    return (
        f"<b>{html.escape(listing['location'])}</b>\n"
        f"Price: ${listing['price']:g} per night\n"
        f"Contact: {html.escape(listing['phone'])}"
    )


def book_markup(bot_username: str, listing_id: int) -> InlineKeyboardMarkup:
    link = f"https://t.me/{bot_username}?start=book_{listing_id}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("Book this", url=link)]])


async def post_to_channel(context: ContextTypes.DEFAULT_TYPE, listing_id: int):
    listing = db.get_listing(listing_id)
    markup = book_markup(context.bot.username, listing_id)
    return await context.bot.send_photo(
        channel_id(),
        listing["photo_file_id"],
        caption=caption_for(listing),
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_listing", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def abandon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_listing", None)
    await update.message.reply_text("Listing cancelled. Tap the button again to continue.")
    return ConversationHandler.END


text_only = filters.TEXT & ~filters.COMMAND & ~filters.Regex(labels.MENU_REGEX)

conversation = ConversationHandler(
    entry_points=[
        CommandHandler("list", list_start),
        MessageHandler(filters.Regex(f"^{re.escape(labels.POST_LISTING)}$"), list_start),
    ],
    states={
        PHOTO: [MessageHandler(filters.PHOTO, got_photo), MessageHandler(text_only, need_photo)],
        LOCATION: [MessageHandler(text_only, got_location)],
        PHONE: [MessageHandler(text_only, got_phone)],
        PRICE: [MessageHandler(text_only, got_price)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel),
        # Tapping a menu button mid-form abandons the form instead of being read as an answer.
        MessageHandler(filters.Regex(labels.MENU_REGEX), abandon),
    ],
)
