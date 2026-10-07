"""Renter bot: leave a star rating and a comment for a booking; the channel card shows the result."""
import datetime as dt
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

from bot import db, labels, payments, roles
from bot.listing_flow import book_markup, caption_for, channel_id

log = logging.getLogger("uzbekrentalsbot.reviews")

RATING, COMMENT = range(2)
COMMENT_MAX = 300
text_only = filters.TEXT & ~filters.COMMAND & ~filters.Regex(labels.MENU_REGEX)


def review_buttons(rows) -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton(f"⭐ Review: {r['location']} ({r['day']})"[:60], callback_data=f"rt:{r['id']}")]
        for r in rows[:10]
    ]


def _problem(b, chat_id: int) -> str | None:
    if b is None or b["renter_chat_id"] != chat_id or b["status"] != "confirmed":
        return "I couldn't find a confirmed booking to review."
    if b["day"] > dt.date.today().isoformat():
        return "You can review a place once your booking date has come."
    if any(r["id"] == b["id"] for r in db.reviewable_bookings(chat_id, dt.date.today().isoformat())) is False:
        return "You've already reviewed that booking. Thank you!"
    return None


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    b = db.booking_with_listing(int(query.data.split(":")[1]))
    problem = _problem(b, query.message.chat_id)
    if problem:
        await query.message.reply_text(problem)
        return ConversationHandler.END
    context.user_data["review"] = {"booking": b["id"]}
    stars = [InlineKeyboardButton(f"{n} ⭐", callback_data=f"rs:{n}") for n in range(1, 6)]
    await query.message.reply_text(
        f"⭐ How was <b>{html.escape(b['location'])}</b>? Tap a rating, or /cancel.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([stars]),
    )
    return RATING


async def got_rating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["review"]["rating"] = int(query.data.split(":")[1])
    await query.edit_message_reply_markup(InlineKeyboardMarkup([]))
    await query.message.reply_text(
        f"\U0001F4AC Add a short comment (up to {COMMENT_MAX} characters), or send - to skip."
    )
    return COMMENT


async def got_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    comment = " ".join(update.message.text.split())
    if comment == "-":
        comment = ""
    elif len(comment) > COMMENT_MAX:
        await update.message.reply_text(f"Please keep it under {COMMENT_MAX} characters, or send - to skip.")
        return COMMENT
    data = context.user_data.pop("review")
    b = db.booking_with_listing(data["booking"])
    if not db.add_review(b["id"], data["rating"], comment):
        await update.message.reply_text("You've already reviewed that booking.")
        return ConversationHandler.END
    await update.message.reply_text("\U0001F64F Thank you! Your review is saved and shown on the listing's channel card.")
    await refresh_channel_card(b["listing_id"], context.bot.username)
    owner = db.get_user(b["owner_chat_id"])
    if owner is None or owner["notify"]:
        renter = db.get_user(update.effective_chat.id)
        await payments.notify_host(
            b["owner_chat_id"],
            f"⭐ <b>New review</b> for {html.escape(b['location'])}\n"
            f"{'⭐' * data['rating']} from {html.escape(renter['name'] if renter else 'a renter')}"
            + (f"\n\U0001F4AC {html.escape(comment)}" if comment else ""),
        )
    return ConversationHandler.END


async def refresh_channel_card(listing_id: int, renter_bot_username: str) -> bool:
    """Edit the channel card so it shows the latest rating. The host bot is the channel admin."""
    listing = db.get_listing(listing_id)
    chat, message_id = channel_id(), listing["channel_message_id"] if listing else None
    host = roles.host_bot()
    if listing is None or not chat or not message_id or host is None or listing["status"] == "deleted":
        return False
    active = listing["status"] == "active"
    caption = caption_for(listing) + ("" if active else "\n\n<i>No longer available</i>")
    markup = book_markup(renter_bot_username, listing_id) if active else InlineKeyboardMarkup([])
    try:
        async with host:
            await host.edit_message_caption(chat, message_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup)
        return True
    except TelegramError as e:
        log.error("Could not refresh channel card for listing %s: %s", listing_id, e)
        return False


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("review", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(begin, pattern=r"^rt:\d+$")],
    states={
        RATING: [CallbackQueryHandler(got_rating, pattern=r"^rs:[1-5]$")],
        COMMENT: [MessageHandler(text_only, got_comment)],
    },
    fallbacks=[CommandHandler("cancel", cancel), MessageHandler(filters.Regex(labels.MENU_REGEX), cancel)],
)
