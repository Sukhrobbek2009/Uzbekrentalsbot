import datetime as dt
import html
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from bot import db
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


def provider_token() -> str | None:
    return os.environ.get("PAYMENT_PROVIDER_TOKEN", "").strip() or None


def down_payment_minor(price: float) -> int:
    return round(price * down_payment_percent())  # price * pct/100 dollars * 100 cents


def day_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    today = dt.date.today()
    days = [today + dt.timedelta(days=i) for i in range(DAYS_AHEAD)]
    buttons = [
        InlineKeyboardButton(d.strftime("%a %d %b"), callback_data=f"bk:d:{listing_id}:{d.isoformat()}")
        for d in days
    ]
    return InlineKeyboardMarkup([buttons[i : i + 2] for i in range(0, len(buttons), 2)])


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
    await message.reply_photo(listing["photo_file_id"], caption=caption_for(listing), parse_mode=ParseMode.HTML)
    await message.reply_text(
        "Pick a day for your visit / pick-up:", reply_markup=day_keyboard(listing_id)
    )


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


async def on_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, lid, day, hour = query.data.split(":", 4)
    listing_id, slot = int(lid), f"{hour}:00"
    listing = db.get_listing(listing_id)
    if listing is None or listing["status"] != "active" or slot not in SLOTS or not _valid_day(day):
        await query.edit_message_text("Sorry, that option is no longer valid. Open the listing again from the channel.")
        return
    if slot in db.taken_slots(listing_id, day):
        await query.edit_message_text("Sorry, that time was just taken. Pick another:", reply_markup=slot_keyboard(listing_id, day) or day_keyboard(listing_id))
        return
    token = provider_token()
    if not token:
        await query.edit_message_text("Payments aren't set up yet, so I can't take a down payment right now.")
        return

    amount = down_payment_minor(listing["price"])
    booking_id = db.create_booking(listing_id, query.message.chat_id, day, slot, amount, currency())
    await query.edit_message_text(f"Booking {day} at {slot}. Please pay the down payment below to confirm.")
    try:
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title="Down payment",
            description=f"{down_payment_percent():g}% down payment for {listing['location']} on {day} at {slot}"[:255],
            payload=f"booking:{booking_id}",
            provider_token=token,
            currency=currency(),
            prices=[LabeledPrice(f"Down payment ({down_payment_percent():g}%)", amount)],
        )
    except TelegramError as e:
        log.error("Could not send invoice for booking %s: %s", booking_id, e)
        await query.message.reply_text("Sorry, I couldn't create the payment. Please try again later.")


async def on_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.pre_checkout_query
    booking = None
    if q.invoice_payload.startswith("booking:") and q.invoice_payload[8:].isdigit():
        booking = db.get_booking(int(q.invoice_payload[8:]))
    if booking is None or booking["status"] != "pending_payment":
        await q.answer(ok=False, error_message="This booking is no longer valid. Please start again.")
        return
    if q.total_amount != booking["amount_minor"] or q.currency != booking["currency"]:
        await q.answer(ok=False, error_message="The amount changed. Please start again.")
        return
    if booking["slot"] in db.taken_slots(booking["listing_id"], booking["day"]):
        await q.answer(ok=False, error_message="Sorry, that time was just taken.")
        return
    await q.answer(ok=True)


async def on_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    payment = update.message.successful_payment
    booking = db.get_booking(int(payment.invoice_payload.split(":", 1)[1]))
    if booking is None:
        log.error("Payment %s received for unknown booking", payment.telegram_payment_charge_id)
        return
    if not db.confirm_booking(booking["id"], payment.telegram_payment_charge_id):
        log.error("Booking %s slot conflict after payment; needs refund (charge %s)", booking["id"], payment.telegram_payment_charge_id)
        await update.message.reply_text(
            "Sorry, someone else just booked that time. Your payment will be refunded; please pick another time."
        )
        return
    listing = db.get_listing(booking["listing_id"])
    await update.message.reply_text(
        f"Booked! {booking['day']} at {booking['slot']} - {listing['location']}.\n"
        f"Contact the owner: {listing['phone']}"
    )
    user = update.effective_user
    who = html.escape(user.full_name) + (f" (@{html.escape(user.username)})" if user.username else "")
    owner = db.get_user(listing["owner_chat_id"])
    if owner is not None and not owner["notify"]:
        return
    try:
        await context.bot.send_message(
            listing["owner_chat_id"],
            f"New booking for {html.escape(listing['location'])}: {booking['day']} at {booking['slot']} by {who}. "
            f"Down payment received: {booking['amount_minor'] / 100:.2f} {booking['currency']}.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        log.error("Could not notify owner of booking %s: %s", booking["id"], e)


handlers = [
    CallbackQueryHandler(on_day, pattern=r"^bk:d:\d+:\d{4}-\d{2}-\d{2}$"),
    CallbackQueryHandler(on_time, pattern=r"^bk:t:\d+:\d{4}-\d{2}-\d{2}:\d{2}$"),
    PreCheckoutQueryHandler(on_precheckout),
    MessageHandler(filters.SUCCESSFUL_PAYMENT, on_paid),
]
