"""Which of the two bots this process is.

The same code runs as two bots with separate tokens and one shared database:
  BOT_ROLE=host    posts and manages listings (the host bot)
  BOT_ROLE=renter  searches, books and pays (the customer bot)
"""
import logging
import os
import sys

from telegram import Bot

log = logging.getLogger("uzbekrentalsbot.roles")

HOST = "host"
RENTER = "renter"

ROLE = os.environ.get("BOT_ROLE", HOST).strip().lower()
if ROLE not in (HOST, RENTER):
    sys.exit("BOT_ROLE must be 'host' or 'renter'.")


def is_host() -> bool:
    return ROLE == HOST


def title() -> str:
    return "Host" if is_host() else "Renter"


def booking_bot_username(bot: Bot) -> str:
    """Username the channel card's "Book this" link opens: always the renter bot."""
    if not is_host():
        return bot.username
    name = os.environ.get("RENTER_BOT_USERNAME", "").strip().lstrip("@")
    if not name:
        log.error("RENTER_BOT_USERNAME is not set; channel cards will link to the host bot, which can't book.")
        return bot.username
    return name


def renter_bot() -> Bot | None:
    """A Bot for the renter bot, used by the host bot to message renters. None if not configured."""
    token = os.environ.get("RENTER_BOT_TOKEN", "").strip()
    return Bot(token) if token else None


def host_bot() -> Bot | None:
    """A Bot for the host bot, used by the renter bot to notify owners. None if not configured."""
    token = os.environ.get("HOST_BOT_TOKEN", "").strip()
    return Bot(token) if token else None
