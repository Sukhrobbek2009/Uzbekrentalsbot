import datetime as dt
import html
import logging

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from bot import db
from bot.api_client import LinkExpired, NotLinked, authed_get

log = logging.getLogger("uzbekrentalsbot.account")

UPCOMING_STATUSES = {"pending", "confirmed"}
MAX_SHOWN = 10

LINK_PROMPT = InlineKeyboardMarkup([[InlineKeyboardButton("Link my account", callback_data="link")]])


def _fmt_date(d: dt.date) -> str:
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def format_booking(b: dict) -> str:
    start = dt.date.fromisoformat(b["start_date"])
    end = dt.date.fromisoformat(b["end_date"])
    where = f" ({html.escape(b['listing_location'])})" if b.get("listing_location") else ""
    return (
        f"<b>{html.escape(b['listing_title'])}</b>{where}\n"
        f"{_fmt_date(start)} - {_fmt_date(end)} | {b['status']} | ${b['total_price']:g} total"
    )


def upcoming(bookings: list[dict], today: dt.date | None = None) -> list[dict]:
    today = today or dt.date.today()
    keep = [
        b for b in bookings
        if b["status"] in UPCOMING_STATUSES and dt.date.fromisoformat(b["end_date"]) >= today
    ]
    return sorted(keep, key=lambda b: b["start_date"])


async def show_bookings(message: Message) -> None:
    try:
        bookings = await authed_get(message.chat_id, "/api/bookings/mine")
    except NotLinked:
        await message.reply_text(
            "Link your Vatan Rentals account first to see your bookings.", reply_markup=LINK_PROMPT
        )
        return
    except LinkExpired:
        await message.reply_text(
            "Your account link has expired. Please link your account again.", reply_markup=LINK_PROMPT
        )
        return
    except httpx.HTTPError as e:
        log.error("Could not load bookings: %s %s", e.__class__.__name__, e)
        await message.reply_text("Sorry, I couldn't load your bookings right now. Please try again later.")
        return

    items = upcoming(bookings)
    if not items:
        await message.reply_text("You have no upcoming bookings. Use /search to find a place.")
        return
    lines = [format_booking(b) for b in items[:MAX_SHOWN]]
    extra = len(items) - MAX_SHOWN
    if extra > 0:
        lines.append(f"...and {extra} more on the website.")
    await message.reply_text(
        "Your upcoming bookings:\n\n" + "\n\n".join(lines), parse_mode=ParseMode.HTML
    )


async def mybookings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_bookings(update.message)


async def on_bookings_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await show_bookings(query.message)


async def myaccount_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    link = db.get_link(update.effective_chat.id)
    if link is None:
        await update.message.reply_text(
            "No Vatan Rentals account is linked to this chat.", reply_markup=LINK_PROMPT
        )
        return
    await update.message.reply_text(
        "Linked Vatan Rentals account:\n"
        f"Name: {link['full_name']}\n"
        f"Email: {link['email']}\n"
        f"Role: {link['role']}\n"
        f"Linked on: {link['linked_at'][:10]}"
    )


handlers = [
    CommandHandler("mybookings", mybookings_command),
    CommandHandler("myaccount", myaccount_command),
    CallbackQueryHandler(on_bookings_button, pattern=r"^bookings$"),
]
