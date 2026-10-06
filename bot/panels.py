import datetime as dt
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from bot import account, db

log = logging.getLogger("uzbekrentalsbot.panels")

SHOWN = 10


def _role_name(role: str | None) -> str:
    return "Host" if role == "host" else "Renter"


async def home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = db.get_user(chat_id)
    counts = db.home_counts(chat_id, dt.date.today().isoformat())
    await update.message.reply_text(
        f"Home - hi {user['name']}!\n\n"
        f"Active listings: {counts['active_listings']}\n"
        f"Upcoming bookings you made: {counts['bookings_made']}\n"
        f"Upcoming bookings on your listings: {counts['bookings_received']}\n\n"
        "Use the menu below: Search to find a place, Post to list yours."
    )


def _money(rows, key: str = "amount_minor") -> str:
    totals: dict[str, int] = {}
    for r in rows:
        totals[r["currency"]] = totals.get(r["currency"], 0) + r[key]
    return ", ".join(f"{v / 100:.2f} {c}" for c, v in totals.items())


def _lines(rows) -> list[str]:
    out = []
    for r in rows[:SHOWN]:
        flag = " (needs refund)" if r["status"] == "needs_refund" else ""
        out.append(f"{r['day']} {r['slot']} - {r['location']} - {r['amount_minor'] / 100:.2f} {r['currency']}{flag}")
    if len(rows) > SHOWN:
        out.append(f"...and {len(rows) - SHOWN} more")
    return out


async def payments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    made, received = db.payments_made(chat_id), db.payments_received(chat_id)
    if not made and not received:
        await update.message.reply_text(
            "No payments yet. Down payments you make or receive through the channel will show up here."
        )
        return
    parts = []
    if made:
        parts.append(f"Paid by you ({_money(made)}):\n" + "\n".join(_lines(made)))
    if received:
        parts.append(f"Received on your listings ({_money(received)}):\n" + "\n".join(_lines(received)))
    await update.message.reply_text("\n\n".join(parts))


def settings_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user = db.get_user(chat_id)
    other = "Renter" if user["role"] == "host" else "Host"
    notify = bool(user["notify"])
    text = (
        "Settings\n\n"
        f"Role: {_role_name(user['role'])}\n"
        f"Booking notifications: {'On' if notify else 'Off'}"
    )
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Switch to {other}", callback_data="st:role")],
            [InlineKeyboardButton(f"Turn notifications {'off' if notify else 'on'}", callback_data="st:notify")],
        ]
    )
    return text, markup


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
    if query.data == "st:role":
        db.set_role(chat_id, "renter" if user["role"] == "host" else "host")
    else:
        db.set_notify(chat_id, not user["notify"])
    text, markup = settings_view(chat_id)
    await query.edit_message_text(text, reply_markup=markup)


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = db.get_user(chat_id)
    link = db.get_link(chat_id)
    lines = [
        "Your profile",
        "",
        f"Name: {user['name']}",
        f"Phone: {user['phone']}",
        f"Role: {_role_name(user['role'])}",
        f"Registered: {user['registered_at'][:10]}",
        "",
    ]
    if link is None:
        lines.append("Vatan Rentals account: not linked")
        await update.message.reply_text("\n".join(lines), reply_markup=account.LINK_PROMPT)
    else:
        lines.append(f"Vatan Rentals account: {link['full_name']} ({link['email']})")
        await update.message.reply_text("\n".join(lines))


handlers = [CallbackQueryHandler(on_setting, pattern=r"^st:(role|notify)$")]
