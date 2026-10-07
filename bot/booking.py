import datetime as dt
import html
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
)

from bot import db, payments, roles
from bot.listing_flow import caption_for

log = logging.getLogger("uzbekrentalsbot.booking")

SLOTS = ["10:00", "12:00", "14:00", "16:00", "18:00"]
DAYS_AHEAD = 7


def currency() -> str:
    return os.environ.get("CURRENCY", "USD").strip().upper() or "USD"


def down_payment_percent() -> float:
    try:
        return float(os.environ.get("DOWN_PAYMENT_PERCENT", "20"))
    except ValueError:
        return 20.0


def down_payment_minor(price: float) -> int:
    return round(price * down_payment_percent())  # price * pct/100 dollars * 100 cents


def day_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    today = dt.date.today()
    days = [today + dt.timedelta(days=i) for i in range(DAYS_AHEAD)]
    buttons = [
        InlineKeyboardButton(d.strftime("%a %d %b"), callback_data=f"bk:d:{listing_id}:{d.isoformat()}")
        for d in days
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([payments.ask_host_button(listing_id)])
    return InlineKeyboardMarkup(rows)


def slot_keyboard(listing_id: int, day: str) -> InlineKeyboardMarkup | None:
    taken = db.taken_slots(listing_id, day)
    free = [s for s in SLOTS if s not in taken]
    if not free:
        return None
    buttons = [InlineKeyboardButton(s, callback_data=f"bk:t:{listing_id}:{day}:{s[:2]}") for s in free]
    return InlineKeyboardMarkup([buttons[i : i + 3] for i in range(0, len(buttons), 3)])


async def start_booking(message: Message, listing_id: int) -> None:
    listing = db.get_listing(listing_id)
    if listing is None or listing["status"] == "deleted":
        await message.reply_text("Sorry, that listing no longer exists.")
        return
    if listing["status"] != "active":
        await message.reply_text("Sorry, that listing is not available right now.")
        return
    owner = db.get_user(listing["owner_chat_id"])
    if owner is None or not owner["card_number"]:
        await message.reply_text("\U0001F6AB The host hasn't set up payment details yet. Please try again later.")
        return
    await _show_card(message, listing)
    await message.reply_text(
        "\U0001F4C5 Pick a day for your visit / pick-up:", reply_markup=day_keyboard(listing_id)
    )


async def _show_card(message: Message, listing) -> None:
    """Show the listing's photo and details. Never blocks the booking if the photo can't be shown.

    Telegram file ids only work for the bot that received the photo. Listings are posted through
    the host bot, so the renter bot fetches the image through it once and keeps its own file id.
    """
    caption = caption_for(listing)
    photo = listing["renter_photo_file_id"]
    try:
        if photo is None:
            host = roles.host_bot()
            if host is not None:
                async with host:
                    file = await host.get_file(listing["photo_file_id"])
                    photo = bytes(await file.download_as_bytearray())
        if photo is not None:
            sent = await message.reply_photo(photo, caption=caption, parse_mode=ParseMode.HTML)
            if listing["renter_photo_file_id"] is None:
                db.set_renter_photo(listing["id"], sent.photo[-1].file_id)
            return
    except TelegramError as e:
        log.error("Could not show the photo of listing %s: %s", listing["id"], e)
    await message.reply_text(caption, parse_mode=ParseMode.HTML)


async def on_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, lid, day = query.data.split(":", 3)
    keyboard = slot_keyboard(int(lid), day)
    if keyboard is None:
        await query.edit_message_text("No free times that day. Pick another day:", reply_markup=day_keyboard(int(lid)))
        return
    await query.edit_message_text(f"{day} - pick a time:", reply_markup=keyboard)


def _valid_day(day: str) -> bool:
    try:
        d = dt.date.fromisoformat(day)
    except ValueError:
        return False
    today = dt.date.today()
    return today <= d <= today + dt.timedelta(days=DAYS_AHEAD)


def _check_choice(listing_id: int, day: str, slot: str):
    """(listing, owner, problem) for a day and time the renter picked; problem is None if it's bookable."""
    listing = db.get_listing(listing_id)
    if listing is None or listing["status"] != "active" or slot not in SLOTS or not _valid_day(day):
        return listing, None, "Sorry, that option is no longer valid. Open the listing again from the channel."
    owner = db.get_user(listing["owner_chat_id"])
    if owner is None or not owner["card_number"]:
        return listing, owner, "\U0001F6AB The host hasn't set up payment details yet. Please try again later."
    return listing, owner, None


async def on_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The renter picked a time: ask them to confirm before anything is booked."""
    query = update.callback_query
    await query.answer()
    _, _, lid, day, hour = query.data.split(":", 4)
    listing_id, slot = int(lid), f"{hour}:00"
    listing, _, problem = _check_choice(listing_id, day, slot)
    if problem:
        await query.edit_message_text(problem)
        return
    if slot in db.taken_slots(listing_id, day):
        await query.edit_message_text("Sorry, that time was just taken. Pick another:", reply_markup=slot_keyboard(listing_id, day) or day_keyboard(listing_id))
        return
    amount = down_payment_minor(listing["price"])
    await query.edit_message_text(
        "\U0001F4CB <b>Please confirm your booking</b>\n\n"
        f"\U0001F4CD {html.escape(listing['location'])}\n"
        f"\U0001F4C5 {day} at {slot}\n"
        f"\U0001F4B5 Price: ${listing['price']:g} per night\n"
        f"\U0001F4B3 Down payment now: <b>{amount / 100:.2f} {currency()}</b> ({down_payment_percent():g}%)",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("\u2705 Confirm booking", callback_data=f"bk:c:{listing_id}:{day}:{hour}"),
                    InlineKeyboardButton("\u2716\uFE0F Cancel", callback_data="bk:x"),
                ],
                [payments.ask_host_button(listing_id)],
            ]
        ),
    )


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Booking cancelled. Tap Book this on a channel card whenever you're ready.")


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, lid, day, hour = query.data.split(":", 4)
    listing_id, slot = int(lid), f"{hour}:00"
    listing, owner, problem = _check_choice(listing_id, day, slot)
    if problem:
        await query.edit_message_text(problem)
        return
    if slot in db.taken_slots(listing_id, day):
        await query.edit_message_text("Sorry, that time was just taken. Pick another:", reply_markup=slot_keyboard(listing_id, day) or day_keyboard(listing_id))
        return

    chat_id = query.message.chat_id
    amount = down_payment_minor(listing["price"])
    booking_id = db.create_booking(listing_id, chat_id, day, slot, amount, currency())
    b = db.booking_with_listing(booking_id)
    await query.edit_message_text(
        "\U0001F389 <b>Booked!</b> Your time is saved. It becomes final once the host accepts your payment.\n\n"
        + payments.instructions(b, owner),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("\U0001F4F8 Send payment screenshot", callback_data=f"pay:{booking_id}")],
                [payments.ask_host_button(listing_id)],
            ]
        ),
    )
    if owner["notify"]:
        renter = db.get_user(chat_id)
        name = html.escape(renter["name"]) if renter else "A renter"
        await payments.notify_host(
            owner["chat_id"],
            f"\U0001F514 <b>New booking!</b>\n"
            f"\U0001F4CD {html.escape(listing['location'])}\n"
            f"\U0001F4C5 {day} at {slot}\n"
            f"\U0001F464 {name}" + (f" ({html.escape(renter['phone'])})" if renter else "") + "\n"
            f"\U0001F4B5 Down payment expected: {amount / 100:.2f} {currency()}\n\n"
            "I'll send you the payment screenshot to review as soon as the renter uploads it.",
            InlineKeyboardMarkup([[payments.message_renter_button(listing_id, chat_id)]]),
        )


handlers = [
    payments.pay_conversation,
    CallbackQueryHandler(on_day, pattern=r"^bk:d:\d+:\d{4}-\d{2}-\d{2}$"),
    CallbackQueryHandler(on_time, pattern=r"^bk:t:\d+:\d{4}-\d{2}-\d{2}:\d{2}$"),
    CallbackQueryHandler(on_confirm, pattern=r"^bk:c:\d+:\d{4}-\d{2}-\d{2}:\d{2}$"),
    CallbackQueryHandler(on_cancel, pattern=r"^bk:x$"),
]
