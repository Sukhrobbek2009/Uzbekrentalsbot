"""Host bot: edit a posted listing (price, location, information, phone, photo).

The channel card is edited in place, so reviews and the Book button stay on it. Bookings that already
exist keep the amount they were made with; the new price applies to new bookings.
"""
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import db, labels, manage, roles
from bot.listing_flow import INFO_MAX, PHONE_RE, book_markup, caption_for, channel_id

log = logging.getLogger("uzbekrentalsbot.edit")

VALUE = 0
FIELDS = {
    "price": ("\U0001F4B5 Price", "Send the new price per night as a number, for example: 120"),
    "location": ("\U0001F4CD Location", "Send the new location as text, or share a Telegram location pin."),
    "info": ("\U0001F4DD Information", f"Send the new information about the place (5 to {INFO_MAX} characters)."),
    "phone": ("\U0001F4DE Phone", "Send the new contact phone number. Note: it is visible to everyone in the channel."),
    "photo": ("\U0001F4F8 Photo", "Send the new photo (as a picture, not a file)."),
}
text_only = filters.TEXT & ~filters.COMMAND & ~filters.Regex(labels.MENU_REGEX)


def _owned(listing, chat_id: int) -> bool:
    return listing is not None and listing["owner_chat_id"] == chat_id and listing["status"] != "deleted"


async def choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    listing = db.get_listing(int(query.data.split(":")[1]))
    if not _owned(listing, query.message.chat_id):
        await query.edit_message_caption(caption="This listing no longer exists.", reply_markup=manage.NO_MARKUP)
        return
    rows = [[InlineKeyboardButton(label, callback_data=f"ef:{key}:{listing['id']}")] for key, (label, _) in FIELDS.items()]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"mg:no:{listing['id']}")])
    await query.edit_message_caption(caption="✏️ What do you want to change?", reply_markup=InlineKeyboardMarkup(rows))


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, field, raw_id = query.data.split(":")
    listing = db.get_listing(int(raw_id))
    if not _owned(listing, query.message.chat_id):
        await query.message.reply_text("This listing no longer exists.")
        return ConversationHandler.END
    context.user_data["edit_post"] = (listing["id"], field)
    await query.message.reply_text(f"{FIELDS[field][1]}\n/cancel to stop.")
    return VALUE


def _parse(field: str, message):
    """(columns to change, error text). Exactly one of them is None."""
    if field == "photo":
        if not message.photo:
            return None, "Please send a photo (as a picture, not a file)."
        return {"photo_file_id": message.photo[-1].file_id, "renter_photo_file_id": None}, None
    if field == "location" and message.location is not None:
        return {"latitude": message.location.latitude, "longitude": message.location.longitude}, None
    if not message.text:
        return None, "Please send text."
    text = message.text.strip()
    if field == "price":
        try:
            price = float(text.replace(",", "."))
        except ValueError:
            price = 0
        if not 0 < price <= 1_000_000:
            return None, "Please send a price as a positive number, for example: 120"
        return {"price": price}, None
    if field == "location":
        if not 2 <= len(text) <= 160:
            return None, "Please send a location between 2 and 160 characters, or a location pin."
        return {"location": text}, None
    if field == "info":
        text = " ".join(text.split())
        if not 5 <= len(text) <= INFO_MAX:
            return None, f"Please write between 5 and {INFO_MAX} characters."
        return {"description": text}, None
    if not PHONE_RE.match(text):
        return None, "That doesn't look like a phone number. Example: +1 206 555 0100"
    return {"phone": text}, None


async def got_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    listing_id, field = context.user_data["edit_post"]
    fields, error = _parse(field, update.message)
    if error:
        await update.message.reply_text(error)
        return VALUE
    context.user_data.pop("edit_post")
    listing = db.get_listing(listing_id)
    if not _owned(listing, update.effective_chat.id):
        await update.message.reply_text("This listing no longer exists.")
        return ConversationHandler.END
    db.update_listing(listing_id, **fields)
    listing = db.get_listing(listing_id)
    ok = await _refresh_channel(context, listing, photo_changed=field == "photo")
    await update.message.reply_text(
        "✅ Updated, and the channel card now shows it."
        if ok
        else "✅ Saved, but I couldn't update the channel card. Make sure the bot is still an admin there."
    )
    await update.message.reply_photo(
        listing["photo_file_id"], caption=manage.card_text(listing), reply_markup=manage.card_markup(listing)
    )
    return ConversationHandler.END


async def _refresh_channel(context: ContextTypes.DEFAULT_TYPE, listing, photo_changed: bool) -> bool:
    chat, message_id = channel_id(), listing["channel_message_id"]
    if not chat or not message_id:
        return True
    active = listing["status"] == "active"
    caption = caption_for(listing) + ("" if active else "\n\n<i>No longer available</i>")
    markup = book_markup(roles.booking_bot_username(context.bot), listing["id"]) if active else manage.NO_MARKUP
    try:
        if photo_changed:
            await context.bot.edit_message_media(
                InputMediaPhoto(listing["photo_file_id"], caption=caption, parse_mode=ParseMode.HTML),
                chat_id=chat,
                message_id=message_id,
                reply_markup=markup,
            )
        else:
            await context.bot.edit_message_caption(
                chat, message_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup
            )
        return True
    except TelegramError as e:
        log.error("Could not update channel card for listing %s: %s", listing["id"], e)
        return False


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("edit_post", None)
    await update.message.reply_text("Cancelled. Nothing was changed.")
    return ConversationHandler.END


conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(begin, pattern=r"^ef:(price|location|info|phone|photo):\d+$")],
    allow_reentry=True,
    states={VALUE: [MessageHandler(text_only | filters.PHOTO | filters.LOCATION, got_value)]},
    fallbacks=[CommandHandler("cancel", cancel), MessageHandler(filters.Regex(labels.MENU_REGEX), cancel)],
)

handlers = [conversation, CallbackQueryHandler(choose_field, pattern=r"^ed:\d+$")]
