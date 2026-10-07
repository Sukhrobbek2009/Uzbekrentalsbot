from telegram import KeyboardButton, ReplyKeyboardMarkup

from bot import labels, roles


def _keyboard(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def menu_keyboard() -> ReplyKeyboardMarkup:
    """Six buttons, two per row."""
    if roles.is_host():
        rows = [
            [labels.HOME, labels.POST_LISTING],
            [labels.DELETE, labels.PAYMENTS],
            [labels.SETTINGS, labels.PROFILE],
        ]
    else:
        rows = [
            [labels.HOME, labels.SEARCH],
            [labels.MY_BOOKINGS, labels.MAKE_PAYMENT],
            [labels.SETTINGS, labels.PROFILE],
        ]
    return _keyboard(rows)


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("\U0001F4F1 Share my phone number", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
