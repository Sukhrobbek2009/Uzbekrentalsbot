"""Manual card payments: the renter sends a screenshot, the host confirms or rejects it.

Renter bot: pay:<booking id> starts the screenshot upload.
Host bot:   rv:ok / rv:no / rv:show:<booking id> review it; a rejection needs a written reason.
The two bots can't see each other's file ids or chats, so each one reaches the other side
through a Bot built from the other's token (HOST_BOT_TOKEN / RENTER_BOT_TOKEN).
"""
import datetime as dt
import html
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
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

from bot import db, labels, roles

log = logging.getLogger("uzbekrentalsbot.payments")

PROOF = 0
REASON = 0
REASON_MIN, REASON_MAX = 3, 300
text_only = filters.TEXT & ~filters.COMMAND & ~filters.Regex(labels.MENU_REGEX)

STATUS_LABEL = {
    "pending_payment": "⏳ Waiting for your payment",
    "pending_review": "\U0001F50E Payment sent, waiting for the host",
    "confirmed": "✅ Confirmed",
    "rejected": "❌ Payment rejected",
}


def fmt_card(number: str) -> str:
    return " ".join(number[i : i + 4] for i in range(0, len(number), 4))


def money(b) -> str:
    return f"{b['amount_minor'] / 100:.2f} {b['currency']}"


def instructions(b, owner) -> str:
    """How to pay for booking row b, to the card of its listing's owner (a bot_users row)."""
    holder = f"\n\U0001F464 Holder: {html.escape(owner['card_holder'])}" if owner["card_holder"] else ""
    return (
        f"\U0001F4C5 <b>{html.escape(b['location'])}</b> - {b['day']} at {b['slot']}\n"
        f"\U0001F4B5 Down payment: <b>{money(b)}</b>\n\n"
        f"\U0001F4B3 Send it to card:\n<code>{fmt_card(owner['card_number'])}</code>{holder}\n\n"
        "Then send me a screenshot of the transfer."
    )


def _pay_button(booking_id: int, text: str = "\U0001F4B0 Make payment") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data=f"pay:{booking_id}")]])


def ask_host_button(listing_id: int) -> InlineKeyboardButton:
    """Renter bot: start (or continue) a conversation with the listing's host."""
    return InlineKeyboardButton("\U0001F4AC Ask the host", callback_data=f"q:{listing_id}")


def message_renter_button(listing_id: int, renter_chat_id: int, text: str = "\U0001F4AC Message renter") -> InlineKeyboardButton:
    """Host bot: write to the renter who asked about (or booked) this listing."""
    return InlineKeyboardButton(text, callback_data=f"hr:{listing_id}:{renter_chat_id}")


def _review_markup(booking_id: int) -> InlineKeyboardMarkup:
    b = db.booking_with_listing(booking_id)
    rows = [
        [
            InlineKeyboardButton("\u2705 Confirm", callback_data=f"rv:ok:{booking_id}"),
            InlineKeyboardButton("\u274C Reject", callback_data=f"rv:no:{booking_id}"),
        ]
    ]
    if b is not None:
        rows.append([message_renter_button(b["listing_id"], b["renter_chat_id"])])
    return InlineKeyboardMarkup(rows)


async def notify_host(host_chat_id: int, text: str, markup=None) -> bool:
    return await _tell(roles.host_bot(), host_chat_id, text, markup)


async def _tell(bot, chat_id: int, text: str, markup=None) -> bool:
    """Send a message through the other bot. False if it isn't configured or Telegram refused."""
    if bot is None:
        log.error("The other bot's token is not configured; could not message chat %s", chat_id)
        return False
    try:
        async with bot:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return True
    except TelegramError as e:
        log.error("Could not message chat %s through the other bot: %s", chat_id, e)
        return False


# ---------------------------------------------------------------- renter bot

async def make_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.payable_bookings(update.effective_chat.id, dt.date.today().isoformat())
    if not rows:
        await update.message.reply_text(
            "\U0001F4ED Nothing to pay right now. Pick a place from the channel and book a time first."
        )
        return
    buttons = [
        [
            InlineKeyboardButton(
                f"{'❌ ' if r['status'] == 'rejected' else ''}{r['day']} {r['slot']} - {r['location']}"[:60],
                callback_data=f"pay:{r['id']}",
            )
        ]
        for r in rows[:10]
    ]
    await update.message.reply_text(
        "\U0001F4B0 Which booking do you want to pay for?", reply_markup=InlineKeyboardMarkup(buttons)
    )


def _payable(b, chat_id: int) -> str | None:
    """Why booking row b can't be paid right now, or None if it can."""
    if b is None or b["renter_chat_id"] != chat_id:
        return "I couldn't find that booking."
    if b["status"] not in ("pending_payment", "rejected"):
        return "That booking doesn't need a payment right now."
    if b["day"] < dt.date.today().isoformat():
        return "That booking's date has passed."
    listing = db.get_listing(b["listing_id"])
    if listing["status"] != "active":
        return "Sorry, that listing is not available any more."
    if b["slot"] in db.taken_slots(b["listing_id"], b["day"]):
        return "Sorry, that time was just taken by someone else. Please book another time."
    owner = db.get_user(listing["owner_chat_id"])
    if owner is None or not owner["card_number"]:
        return "The host hasn't set up payment details yet. Please try again later."
    return None


async def begin_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    b = db.booking_with_listing(int(query.data.split(":")[1]))
    problem = _payable(b, query.message.chat_id)
    if problem:
        await query.message.reply_text(problem)
        return ConversationHandler.END
    owner = db.get_user(b["owner_chat_id"])
    note = f"\n\n❌ The host rejected your last screenshot: {html.escape(b['reject_reason'] or '')}" if b["status"] == "rejected" else ""
    context.user_data["pay_booking"] = b["id"]
    await query.message.reply_text(
        instructions(b, owner) + note + "\n\n\U0001F4F8 Send the screenshot now, or /cancel to stop.",
        parse_mode=ParseMode.HTML,
    )
    return PROOF


async def need_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("\U0001F4F8 Please send the screenshot as a picture, or /cancel to stop.")
    return PROOF


async def got_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    chat_id = update.effective_chat.id
    b = db.booking_with_listing(context.user_data.get("pay_booking", 0))
    problem = _payable(b, chat_id)
    if problem:
        context.user_data.pop("pay_booking", None)
        await message.reply_text(problem)
        return ConversationHandler.END
    context.user_data.pop("pay_booking", None)

    db.submit_proof(b["id"], file_id)
    renter = db.get_user(chat_id)
    caption = (
        f"\U0001F4B8 <b>Payment to review</b>\n"
        f"\U0001F4C5 {html.escape(b['location'])} - {b['day']} at {b['slot']}\n"
        f"\U0001F4B5 Amount: {money(b)}\n"
        f"\U0001F464 From: {html.escape(renter['name'])} ({html.escape(renter['phone'])})"
    )
    delivered = await _send_proof_to_host(context, b, file_id, caption)
    await message.reply_text(
        "✅ Thanks! Your screenshot was sent to the host. I'll message you when they confirm or reject it."
        if delivered
        else "✅ Your screenshot is saved, but I couldn't reach the host right now. "
        "They'll see it in their Payments tab."
    )
    return ConversationHandler.END


async def _send_proof_to_host(context, b, file_id: str, caption: str) -> bool:
    host = roles.host_bot()
    if host is None:
        log.error("HOST_BOT_TOKEN is not set; host of booking %s was not notified", b["id"])
        return False
    try:
        photo = await context.bot.get_file(file_id)
        data = bytes(await photo.download_as_bytearray())
        async with host:
            sent = await host.send_photo(
                b["owner_chat_id"], data, caption=caption, parse_mode=ParseMode.HTML, reply_markup=_review_markup(b["id"])
            )
        db.set_host_proof(b["id"], sent.photo[-1].file_id)
        return True
    except TelegramError as e:
        log.error("Could not send proof for booking %s to the host: %s", b["id"], e)
        return False


async def cancel_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("pay_booking", None)
    await update.message.reply_text("Cancelled. Tap \U0001F4B0 Make payment when you're ready.")
    return ConversationHandler.END


pay_conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(begin_pay, pattern=r"^pay:\d+$")],
    states={
        PROOF: [
            MessageHandler(filters.PHOTO | filters.Document.IMAGE, got_proof),
            MessageHandler(text_only, need_photo),
        ]
    },
    fallbacks=[
        CommandHandler("cancel", cancel_pay),
        MessageHandler(filters.Regex(labels.MENU_REGEX), cancel_pay),
    ],
)


def booking_lines(rows) -> list[str]:
    return [
        f"{STATUS_LABEL.get(r['status'], r['status'])}\n\U0001F4C5 {html.escape(r['location'])} - {r['day']} at {r['slot']}"
        f" - {money(r)}" + (f"\n❌ Reason: {html.escape(r['reject_reason'])}" if r["status"] == "rejected" and r["reject_reason"] else "")
        for r in rows
    ]


# ------------------------------------------------------------------ host bot

def _reviewable(b, chat_id: int) -> str | None:
    if b is None or b["owner_chat_id"] != chat_id:
        return "I couldn't find that payment."
    if b["status"] != "pending_review":
        return f"That payment was already handled ({b['status'].replace('_', ' ')})."
    return None


async def show_pending(message: Message) -> None:
    rows = db.pending_reviews(message.chat_id)
    if not rows:
        return
    buttons = [
        [InlineKeyboardButton(f"\U0001F50E {r['day']} {r['slot']} - {r['location']}"[:60], callback_data=f"rv:show:{r['id']}")]
        for r in rows[:10]
    ]
    await message.reply_text(
        f"\U0001F4E5 {len(rows)} payment(s) waiting for your review:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def on_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    b = db.booking_with_listing(int(query.data.split(":")[2]))
    problem = _reviewable(b, query.message.chat_id)
    if problem:
        await query.message.reply_text(problem)
        return
    renter = db.get_user(b["renter_chat_id"])
    text = (
        f"\U0001F4B8 <b>Payment to review</b>\n"
        f"\U0001F4C5 {html.escape(b['location'])} - {b['day']} at {b['slot']}\n"
        f"\U0001F4B5 Amount: {money(b)}\n"
        f"\U0001F464 From: {html.escape(renter['name'] if renter else 'unknown')}"
    )
    if b["host_proof_file_id"]:
        await query.message.reply_photo(
            b["host_proof_file_id"], caption=text, parse_mode=ParseMode.HTML, reply_markup=_review_markup(b["id"])
        )
    else:
        await query.message.reply_text(
            text + "\n\n(The screenshot didn't reach this chat. Ask the renter to send it again.)",
            parse_mode=ParseMode.HTML,
        )


async def _close_review(message: Message, note: str) -> None:
    try:
        await message.edit_caption(caption=(message.caption_html or "") + f"\n\n{note}", parse_mode=ParseMode.HTML)
    except TelegramError:
        pass


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    b = db.booking_with_listing(int(query.data.split(":")[2]))
    problem = _reviewable(b, query.message.chat_id)
    if problem:
        await query.message.reply_text(problem)
        return
    if not db.confirm_booking(b["id"]):
        await query.message.reply_text(
            "⚠️ That time is already confirmed for another renter. Please reject this payment "
            "and refund the renter yourself."
        )
        return
    await _close_review(query.message, "✅ Confirmed")
    await query.message.reply_text("✅ Confirmed. I've told the renter.")
    await _tell(
        roles.renter_bot(),
        b["renter_chat_id"],
        f"✅ <b>Payment confirmed!</b>\n"
        f"\U0001F4C5 {html.escape(b['location'])} - {b['day']} at {b['slot']}\n"
        f"\U0001F4DE Host contact: {html.escape(b['listing_phone'])}",
    )


async def begin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    b = db.booking_with_listing(int(query.data.split(":")[2]))
    problem = _reviewable(b, query.message.chat_id)
    if problem:
        await query.message.reply_text(problem)
        return ConversationHandler.END
    context.user_data["reject_booking"] = (b["id"], query.message.chat_id, query.message.message_id)
    await query.message.reply_text(
        "✍️ Write the reason for rejecting this payment (the renter will see it), or /cancel."
    )
    return REASON


async def got_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reason = " ".join(update.message.text.split())
    if not REASON_MIN <= len(reason) <= REASON_MAX:
        await update.message.reply_text(f"Please write between {REASON_MIN} and {REASON_MAX} characters.")
        return REASON
    booking_id, chat_id, message_id = context.user_data.pop("reject_booking")
    b = db.booking_with_listing(booking_id)
    problem = _reviewable(b, chat_id)
    if problem:
        await update.message.reply_text(problem)
        return ConversationHandler.END
    db.reject_booking(booking_id, reason)
    try:
        await context.bot.edit_message_reply_markup(chat_id, message_id, reply_markup=InlineKeyboardMarkup([]))
    except TelegramError:
        pass
    await update.message.reply_text("❌ Rejected. I've sent your reason to the renter.")
    await _tell(
        roles.renter_bot(),
        b["renter_chat_id"],
        f"❌ <b>Payment rejected</b>\n"
        f"\U0001F4C5 {html.escape(b['location'])} - {b['day']} at {b['slot']}\n"
        f"✍️ Reason: {html.escape(reason)}\n\nYou can send a new screenshot.",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("\U0001F4F8 Send a new screenshot", callback_data=f"pay:{booking_id}")],
                [ask_host_button(b["listing_id"])],
            ]
        ),
    )
    return ConversationHandler.END


async def cancel_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reject_booking", None)
    await update.message.reply_text("Cancelled. The payment is still waiting for your review.")
    return ConversationHandler.END


reject_conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(begin_reject, pattern=r"^rv:no:\d+$")],
    states={REASON: [MessageHandler(text_only, got_reason)]},
    fallbacks=[
        CommandHandler("cancel", cancel_reject),
        MessageHandler(filters.Regex(labels.MENU_REGEX), cancel_reject),
    ],
)

host_handlers = [
    reject_conversation,
    CallbackQueryHandler(on_confirm, pattern=r"^rv:ok:\d+$"),
    CallbackQueryHandler(on_show, pattern=r"^rv:show:\d+$"),
]
