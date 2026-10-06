import logging
import re

from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import account, booking, db, labels, manage, panels
from bot.search import begin_search

log = logging.getLogger("uzbekrentalsbot.menu")

NAME, PHONE = range(2)

HELP = (
    "Use the menu buttons below, or these commands:\n"
    "/start - registration and main menu\n"
    "/search - find a home or car by city\n"
    "/list - post your own listing to the channel\n"
    "/mybookings - your upcoming bookings (needs a linked account)\n"
    "/myaccount - which Vatan Rentals account is linked to this chat\n"
    "/help - show this message"
)


def _keyboard(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def role_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard([[labels.RENT, labels.HOST]])


def menu_keyboard(role: str | None = None) -> ReplyKeyboardMarkup:
    return _keyboard(
        [
            [labels.HOME, labels.SEARCH, labels.POST_LISTING],
            [labels.MY_BOOKINGS, labels.DELETE, labels.PAYMENTS],
            [labels.SETTINGS, labels.PROFILE, labels.HELP],
        ]
    )


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Share my phone number", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def ask_role(update: Update) -> None:
    await update.message.reply_text("Do you want to rent or host?", reply_markup=role_keyboard())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Deep link from a channel card: t.me/<bot>?start=book_<listing id>
    arg = context.args[0] if context.args else ""
    if arg.startswith("book_") and arg[5:].isdigit():
        await booking.start_booking(update.message, int(arg[5:]))
        return ConversationHandler.END

    user = db.get_user(update.effective_chat.id)
    if user is None:
        await update.message.reply_text(
            "Welcome to Uzbek Rentals! Let's get you registered. What's your name?",
            reply_markup=ReplyKeyboardRemove(),
        )
        return NAME
    if user["role"] is None:
        await ask_role(update)
    else:
        await update.message.reply_text(
            f"Welcome back, {user['name']}!", reply_markup=menu_keyboard()
        )
    return ConversationHandler.END


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = " ".join(update.message.text.split())
    if not 2 <= len(name) <= 60:
        await update.message.reply_text("Please send your name (2 to 60 characters).")
        return NAME
    context.user_data["reg_name"] = name
    await update.message.reply_text(
        f"Nice to meet you, {name}. Please share your phone number with the button below.",
        reply_markup=phone_keyboard(),
    )
    return PHONE


async def got_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contact = update.message.contact
    if contact.user_id != update.effective_user.id:
        await update.message.reply_text(
            "Please share your own number using the button below.", reply_markup=phone_keyboard()
        )
        return PHONE
    name = context.user_data.pop("reg_name", None) or update.effective_user.full_name
    phone = contact.phone_number if contact.phone_number.startswith("+") else f"+{contact.phone_number}"
    db.save_user(update.effective_chat.id, name, phone)
    await update.message.reply_text(f"Welcome, {name}! You're registered.")
    await ask_role(update)
    return ConversationHandler.END


async def need_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Tap \"Share my phone number\" below to continue.", reply_markup=phone_keyboard()
    )
    return PHONE


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reg_name", None)
    await update.message.reply_text("Registration cancelled. Send /start to begin again.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


registration = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
        PHONE: [
            MessageHandler(filters.CONTACT, got_contact),
            MessageHandler(filters.TEXT & ~filters.COMMAND, need_contact),
        ],
    },
    fallbacks=[CommandHandler("start", start), CommandHandler("cancel", cancel)],
)


def _registered(update: Update):
    return db.get_user(update.effective_chat.id)


async def _require_registration(update: Update) -> bool:
    if _registered(update) is None:
        await update.message.reply_text("Please send /start to register first.", reply_markup=ReplyKeyboardRemove())
        return False
    return True


async def on_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_registration(update):
        return
    role = "host" if update.message.text == labels.HOST else "renter"
    db.set_role(update.effective_chat.id, role)
    text = (
        "Great! As a host you can tap Post to list a place (photo, location, phone number and price); "
        "it will appear in our channel, where renters can book it."
        if role == "host"
        else "Great! Tap Search to find a place, or Post if you want to list one."
    )
    await update.message.reply_text(text, reply_markup=menu_keyboard())


def _guarded(handler):
    """Wrap a menu action so unregistered users are sent to /start first."""

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await _require_registration(update):
            await handler(update, context)

    return wrapper


async def on_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await begin_search(update.message)


async def on_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await account.show_bookings(update.message)


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


async def on_other_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _registered(update)
    if user is None:
        await update.message.reply_text("Please send /start to register first.")
    else:
        await update.message.reply_text("Use the menu buttons below.", reply_markup=menu_keyboard())


def _label(text: str) -> filters.BaseFilter:
    return filters.Regex(f"^{re.escape(text)}$")


handlers = [
    MessageHandler(_label(labels.RENT) | _label(labels.HOST), on_role),
    MessageHandler(_label(labels.HOME), _guarded(panels.home)),
    MessageHandler(_label(labels.SEARCH), _guarded(on_search)),
    MessageHandler(_label(labels.MY_BOOKINGS), _guarded(on_bookings)),
    MessageHandler(_label(labels.DELETE), _guarded(manage.show_listings)),
    MessageHandler(_label(labels.PAYMENTS), _guarded(panels.payments)),
    MessageHandler(_label(labels.SETTINGS), _guarded(panels.settings)),
    MessageHandler(_label(labels.PROFILE), _guarded(panels.profile)),
    MessageHandler(_label(labels.HELP), on_help),
]

# Registered last: anything typed that nothing else wanted.
fallback = MessageHandler(filters.TEXT & ~filters.COMMAND, on_other_text)
