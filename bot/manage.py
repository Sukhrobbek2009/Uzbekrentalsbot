import datetime as dt
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, ContextTypes

from bot import db, roles
from bot.listing_flow import book_markup, caption_for, channel_id

log = logging.getLogger("uzbekrentalsbot.manage")

NO_MARKUP = InlineKeyboardMarkup([])


def card_text(listing) -> str:
    state = "\U0001F7E2 Active" if listing["status"] == "active" else "\u23F8 Inactive (hidden from renters)"
    return f"\U0001F4CD {listing['location']}\n\U0001F4B5 ${listing['price']:g} per night\n{state}"


def card_markup(listing) -> InlineKeyboardMarkup:
    first = (
        InlineKeyboardButton("\u23F8 Deactivate", callback_data=f"mg:off:{listing['id']}")
        if listing["status"] == "active"
        else InlineKeyboardButton("\u25B6\uFE0F Reactivate", callback_data=f"mg:on:{listing['id']}")
    )
    return InlineKeyboardMarkup([[first, InlineKeyboardButton("\U0001F5D1 Delete", callback_data=f"mg:del:{listing['id']}")]])


async def sync_channel(context: ContextTypes.DEFAULT_TYPE, listing, state: str) -> bool:
    """Make the channel card match the listing's new state. False if Telegram refused."""
    chat, message_id = channel_id(), listing["channel_message_id"]
    if not chat or not message_id:
        return True
    try:
        if state == "deleted":
            await context.bot.delete_message(chat, message_id)
        elif state == "inactive":
            await context.bot.edit_message_caption(
                chat,
                message_id,
                caption=caption_for(listing) + "\n\n<i>No longer available</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=NO_MARKUP,
            )
        else:
            await context.bot.edit_message_caption(
                chat,
                message_id,
                caption=caption_for(listing),
                parse_mode=ParseMode.HTML,
                reply_markup=book_markup(roles.booking_bot_username(context.bot), listing["id"]),
            )
        return True
    except TelegramError as e:
        log.error("Could not update channel card for listing %s: %s", listing["id"], e)
        return False


async def show_listings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    listings = db.owner_listings(update.effective_chat.id)
    if not listings:
        await update.message.reply_text("You have no active or inactive listings. Tap Post to create one.")
        return
    await update.message.reply_text("Your listings. Deactivate hides one from renters; Delete removes it.")
    for listing in listings:
        await update.message.reply_photo(
            listing["photo_file_id"], caption=card_text(listing), reply_markup=card_markup(listing)
        )


async def on_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, action, raw_id = query.data.split(":")
    listing = db.get_listing(int(raw_id))
    if listing is None or listing["owner_chat_id"] != query.message.chat_id or listing["status"] == "deleted":
        await query.edit_message_caption(caption="This listing no longer exists.", reply_markup=NO_MARKUP)
        return
    lid = listing["id"]

    if action in ("off", "on"):
        state = "inactive" if action == "off" else "active"
        db.set_listing_status(lid, state)
        listing = db.get_listing(lid)
        ok = await sync_channel(context, listing, state)
        note = "" if ok else "\n(Couldn't update the channel card - make sure the bot is still an admin there.)"
        await query.edit_message_caption(caption=card_text(listing) + note, reply_markup=card_markup(listing))
    elif action == "del":
        await query.edit_message_caption(
            caption=f"Delete {listing['location']}? It will be removed from the channel and can't be undone.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("\u2705 Yes, delete", callback_data=f"mg:yes:{lid}"),
                  InlineKeyboardButton("\u2716\uFE0F Cancel", callback_data=f"mg:no:{lid}")]]
            ),
        )
    elif action == "no":
        await query.edit_message_caption(caption=card_text(listing), reply_markup=card_markup(listing))
    elif action == "yes":
        if db.upcoming_confirmed_count(lid, dt.date.today().isoformat()) > 0:
            # Same rule as the website: a listing with upcoming paid bookings can't vanish.
            db.set_listing_status(lid, "inactive")
            listing = db.get_listing(lid)
            await sync_channel(context, listing, "inactive")
            await query.edit_message_caption(
                caption=card_text(listing)
                + "\n\nIt has upcoming paid bookings, so it can't be deleted. It was deactivated instead.",
                reply_markup=card_markup(listing),
            )
            return
        db.set_listing_status(lid, "deleted")
        ok = await sync_channel(context, listing, "deleted")
        note = "" if ok else " (Couldn't remove the channel card - delete it there manually.)"
        await query.edit_message_caption(caption=f"Deleted {listing['location']}.{note}", reply_markup=NO_MARKUP)


handlers = [CallbackQueryHandler(on_manage, pattern=r"^mg:(off|on|del|yes|no):\d+$")]
