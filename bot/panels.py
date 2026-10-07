import datetime as dt
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import account, db, labels, payments, roles
from bot.keyboards import menu_keyboard, phone_keyboard
from bot.listing_flow import PHONE_RE

log = logging.getLogger("uzbekrentalsbot.panels")

SHOWN = 10
EDIT_NAME, EDIT_PHONE, CARD_NUMBER, CARD_HOLDER = range(4)
text_only = filters.TEXT & ~filters.COMMAND & ~filters.Regex(labels.MENU_REGEX)


async def home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = db.get_user(chat_id)
    counts = db.home_counts(chat_id, dt.date.today().isoformat())
    if roles.is_host():
        waiting = len(db.pending_reviews(chat_id))
        body = (
            f"\U0001F3E1 Active listings: {counts['active_listings']}\n"
            f"\U0001F4C5 Upcoming bookings on your listings: {counts['bookings_received']}\n"
            f"\U0001F4E5 Payments waiting for review: {waiting}\n\n"
            "Tap Post to list a place, or Payments to review screenshots."
        )
    else:
        owing = len(db.payable_bookings(chat_id, dt.date.today().isoformat()))
        body = (
            f"\U0001F4C5 Upcoming confirmed bookings: {counts['bookings_made']}\n"
            f"⏳ Bookings waiting for your payment: {owing}\n\n"
            "Tap Search to find a place, or Make payment to pay for a booking."
        )
    await update.message.reply_text(f"\U0001F44B Hi {user['name']}!\n\n{body}")


def _money(rows, key: str = "amount_minor") -> str:
    totals: dict[str, int] = {}
    for r in rows:
        totals[r["currency"]] = totals.get(r["currency"], 0) + r[key]
    return ", ".join(f"{v / 100:.2f} {c}" for c, v in totals.items())


def _lines(rows) -> list[str]:
    out = []
    for r in rows[:SHOWN]:
        out.append(f"{r['day']} {r['slot']} - {r['location']} - {r['amount_minor'] / 100:.2f} {r['currency']}")
    if len(rows) > SHOWN:
        out.append(f"...and {len(rows) - SHOWN} more")
    return out


async def payments_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Host bot: payments waiting for review, then the ones already confirmed."""
    chat_id = update.effective_chat.id
    await payments.show_pending(update.message)
    rows = db.payments_received(chat_id)
    if rows:
        await update.message.reply_text(
            f"✅ Received on your listings ({_money(rows)}):\n" + "\n".join(_lines(rows))
        )
    elif not db.pending_reviews(chat_id):
        await update.message.reply_text(
            "\U0001F4ED No payments yet. Down payments renters make on your listings will show up here."
        )


# ---------------------------------------------------------------- settings

def settings_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user = db.get_user(chat_id)
    notify = bool(user["notify"])
    what = "New booking" if roles.is_host() else "Booking"
    text = f"⚙️ Settings\n\n\U0001F916 Bot: {roles.title()}\n\U0001F514 {what} notifications: {'On' if notify else 'Off'}"
    rows = [[InlineKeyboardButton(f"\U0001F514 Turn notifications {'off' if notify else 'on'}", callback_data="st:notify")]]
    if roles.is_host():
        if user["card_number"]:
            holder = f" ({user['card_holder']})" if user["card_holder"] else ""
            text += f"\n\U0001F4B3 Payment card: {payments.fmt_card(user['card_number'])}{holder}"
            rows.append([InlineKeyboardButton("\U0001F4B3 Change card", callback_data="st:card"),
                         InlineKeyboardButton("\U0001F5D1 Remove card", callback_data="st:nocard")])
        else:
            text += "\n\U0001F4B3 Payment card: not set (renters can't book until you add one)"
            rows.append([InlineKeyboardButton("\U0001F4B3 Set payment card", callback_data="st:card")])
    return text, InlineKeyboardMarkup(rows)


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = settings_view(update.effective_chat.id)
    await update.message.reply_text(text, reply_markup=markup)


async def on_setting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    user = db.get_user(chat_id)
    if user is None:
        await query.edit_message_text("Please send /start to register first.")
        return
    if query.data == "st:notify":
        db.set_notify(chat_id, not user["notify"])
    elif query.data == "st:nocard":
        db.set_card(chat_id, None, None)
    text, markup = settings_view(chat_id)
    await query.edit_message_text(text, reply_markup=markup)


async def card_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "\U0001F4B3 Send your card number (13-19 digits). Renters will see it and pay to it.\nSend /cancel to stop."
    )
    return CARD_NUMBER


async def card_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    digits = re.sub(r"[\s-]", "", update.message.text)
    if not (digits.isdigit() and 13 <= len(digits) <= 19):
        await update.message.reply_text("That doesn't look like a card number. Send 13-19 digits, e.g. 8600 1234 5678 9012.")
        return CARD_NUMBER
    context.user_data["card_number"] = digits
    await update.message.reply_text("\U0001F464 Now the card holder's name, or send - to skip.")
    return CARD_HOLDER


async def card_holder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    holder = " ".join(update.message.text.split())
    if holder == "-":
        holder = ""
    elif not 2 <= len(holder) <= 60:
        await update.message.reply_text("Please send a name of 2-60 characters, or - to skip.")
        return CARD_HOLDER
    db.set_card(update.effective_chat.id, context.user_data.pop("card_number"), holder or None)
    text, markup = settings_view(update.effective_chat.id)
    await update.message.reply_text("✅ Card saved. Renters will see it when they book.")
    await update.message.reply_text(text, reply_markup=markup)
    return ConversationHandler.END


async def card_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("card_number", None)
    await update.message.reply_text("Cancelled.", reply_markup=menu_keyboard())
    return ConversationHandler.END


card_conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(card_start, pattern=r"^st:card$")],
    states={
        CARD_NUMBER: [MessageHandler(text_only, card_number)],
        CARD_HOLDER: [MessageHandler(text_only, card_holder)],
    },
    fallbacks=[CommandHandler("cancel", card_cancel), MessageHandler(filters.Regex(labels.MENU_REGEX), card_cancel)],
)


# ----------------------------------------------------------------- profile

def _profile_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✏️ Edit name", callback_data="pf:name"),
          InlineKeyboardButton("\U0001F4F1 Edit phone", callback_data="pf:phone")]]
    )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = db.get_user(chat_id)
    link = db.get_link(chat_id)
    lines = [
        "\U0001F464 Your profile",
        "",
        f"\U0001F4DB Name: {user['name']}",
        f"\U0001F4F1 Phone: {user['phone']}",
        f"\U0001F916 Bot: {roles.title()}",
        f"\U0001F4C6 Registered: {user['registered_at'][:10]}",
        "",
        f"\U0001F517 Vatan Rentals account: {link['full_name'] + ' (' + link['email'] + ')' if link else 'not linked'}",
    ]
    markup = _profile_markup()
    if link is None:
        markup = InlineKeyboardMarkup(markup.inline_keyboard + account.LINK_PROMPT.inline_keyboard)
    await update.message.reply_text("\n".join(lines), reply_markup=markup)


async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "pf:name":
        await query.message.reply_text("✏️ Send your new name (2-60 characters), or /cancel.")
        return EDIT_NAME
    await query.message.reply_text(
        "\U0001F4F1 Share your new phone number with the button, or type it (e.g. +1 206 555 0100). /cancel to stop.",
        reply_markup=phone_keyboard(),
    )
    return EDIT_PHONE


async def edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = " ".join(update.message.text.split())
    if not 2 <= len(name) <= 60:
        await update.message.reply_text("Please send a name of 2-60 characters.")
        return EDIT_NAME
    db.set_name(update.effective_chat.id, name)
    await update.message.reply_text(f"✅ Name updated to {name}.", reply_markup=menu_keyboard())
    return ConversationHandler.END


async def edit_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contact = update.message.contact
    if contact is not None:
        if contact.user_id != update.effective_user.id:
            await update.message.reply_text("Please share your own number.", reply_markup=phone_keyboard())
            return EDIT_PHONE
        phone = contact.phone_number if contact.phone_number.startswith("+") else f"+{contact.phone_number}"
    else:
        phone = update.message.text.strip()
        if not PHONE_RE.match(phone):
            await update.message.reply_text("That doesn't look like a phone number. Example: +1 206 555 0100")
            return EDIT_PHONE
    db.set_phone(update.effective_chat.id, phone)
    await update.message.reply_text(f"✅ Phone updated to {phone}.", reply_markup=menu_keyboard())
    return ConversationHandler.END


async def edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.", reply_markup=menu_keyboard())
    return ConversationHandler.END


edit_conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(edit_start, pattern=r"^pf:(name|phone)$")],
    states={
        EDIT_NAME: [MessageHandler(text_only, edit_name)],
        EDIT_PHONE: [MessageHandler(filters.CONTACT | text_only, edit_phone)],
    },
    fallbacks=[CommandHandler("cancel", edit_cancel), MessageHandler(filters.Regex(labels.MENU_REGEX), edit_cancel)],
)

handlers = [
    card_conversation,
    edit_conversation,
    CallbackQueryHandler(on_setting, pattern=r"^st:(notify|nocard)$"),
]
