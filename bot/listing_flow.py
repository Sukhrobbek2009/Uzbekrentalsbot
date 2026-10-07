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

from bot import db, labels, roles

log = logging.getLogger("uzbekrentalsbot.listing")

PHOTO, LOCATION, PRICE, INFO, PHONE = range(5)
CAPTION_LIMIT = 980  # Telegram allows 1024; manage.py appends a short note
INFO_MAX = 500
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
    await message.reply_text("\U0001F4F8 Let's post your listing. First, send a photo. (Send /cancel to stop.)")
    return PHOTO


async def got_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_listing"]["photo"] = update.message.photo[-1].file_id
    await update.message.reply_text("Great. Now send the location: type an address or city (for example: Seattle, WA) or share a Telegram location pin.")
    return LOCATION


async def need_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Please send a photo (as a picture, not a file).")
    return PHOTO


async def got_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pin = update.message.location
    if pin is not None:
        context.user_data["new_listing"].update(
            location="Pinned location", latitude=pin.latitude, longitude=pin.longitude
        )
    else:
        text = update.message.text.strip()
        if not 2 <= len(text) <= 160:
            await update.message.reply_text("Please send a location between 2 and 160 characters, or a location pin.")
            return LOCATION
        context.user_data["new_listing"]["location"] = text
    await update.message.reply_text("Now the price per night/day, as a number (for example: 120).")
    return PRICE


async def got_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.message.text.strip().replace(",", "."))
    except ValueError:
        price = 0
    if not 0 < price <= 1_000_000:
        await update.message.reply_text("Please send a price as a positive number, for example: 120")
        return PRICE
    context.user_data["new_listing"]["price"] = price
    await update.message.reply_text(
        f"Add some information about the place: rooms, rules, what's included (up to {INFO_MAX} characters)."
    )
    return INFO


async def got_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = " ".join(update.message.text.split())
    if not 5 <= len(text) <= INFO_MAX:
        await update.message.reply_text(f"Please write between 5 and {INFO_MAX} characters.")
        return INFO
    context.user_data["new_listing"]["description"] = text
    await update.message.reply_text(
        "Last: a contact phone number. Note: it will be visible to everyone in the channel."
    )
    return PHONE


async def got_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not PHONE_RE.match(text):
        await update.message.reply_text("That doesn't look like a phone number. Example: +1 206 555 0100")
        return PHONE

    data = context.user_data.pop("new_listing")
    listing_id = db.add_listing(
        update.effective_chat.id,
        data["photo"],
        data["location"],
        text,
        data["price"],
        data["description"],
        data.get("latitude"),
        data.get("longitude"),
    )
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
    host = db.get_user(update.effective_chat.id)
    note = "" if host and host["card_number"] else (
        "\n\n\u26A0\uFE0F You haven't set a payment card yet. Add one in \u2699\ufe0f Settings so renters can book and pay."
    )
    await update.message.reply_text("\u2705 Done! Your listing is live in the channel." + note)
    return ConversationHandler.END


def caption_for(listing) -> str:
    lines = [f"\U0001F4CD <b>{html.escape(listing['location'])}</b>"]
    if listing["latitude"] is not None and listing["longitude"] is not None:
        lat, lon = listing["latitude"], listing["longitude"]
        lines.append(f'\U0001F5FA <a href="https://maps.google.com/?q={lat:.6f},{lon:.6f}">Open on map</a>')
    lines.append(f"\U0001F4B5 <b>${listing['price']:g}</b> per night")
    average, count = db.review_stats(listing["id"])
    if count:
        stars = "\u2B50" * round(average)
        lines.append(f"{stars} <b>{average:.1f}</b> ({count} review{'s' if count != 1 else ''})")
    lines.append(f"\U0001F4DE {html.escape(listing['phone'])}")
    # Keep the card under Telegram's caption limit by trimming the free text only.
    quote = db.latest_review(listing["id"]) if count else None
    if quote is not None:
        text = quote["comment"] if len(quote["comment"]) <= 120 else quote["comment"][:119].rstrip() + "\u2026"
        who = html.escape(quote["name"].split()[0]) if quote["name"] else "A renter"
        lines.append(f"\U0001F4AC <i>\u201C{html.escape(text)}\u201D</i> - {who}")
    head = "\n".join(lines)
    room = CAPTION_LIMIT - len(head) - 4
    info = html.escape(listing["description"] or "")
    if info:
        head += "\n\n\U0001F4DD " + (info if len(info) <= room else info[: max(room - 1, 0)].rstrip() + "\u2026")
    return head


def book_markup(bot_username: str, listing_id: int) -> InlineKeyboardMarkup:
    link = f"https://t.me/{bot_username}?start=book_{listing_id}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F511 Book this", url=link)]])


async def post_to_channel(context: ContextTypes.DEFAULT_TYPE, listing_id: int):
    listing = db.get_listing(listing_id)
    markup = book_markup(roles.booking_bot_username(context.bot), listing_id)
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
        LOCATION: [MessageHandler(filters.LOCATION | text_only, got_location)],
        PRICE: [MessageHandler(text_only, got_price)],
        INFO: [MessageHandler(text_only, got_info)],
        PHONE: [MessageHandler(text_only, got_phone)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel),
        # Tapping a menu button mid-form abandons the form instead of being read as an answer.
        MessageHandler(filters.Regex(labels.MENU_REGEX), abandon),
    ],
)
