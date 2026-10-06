import html
import logging
from urllib.parse import urlparse

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from bot.api_client import listing_page_url, search_listings

log = logging.getLogger("uzbekrentalsbot.search")

RESULT_LIMIT = 3

PROPERTY_TYPES = {"home": "Home", "car": "Car"}
CITIES = {
    "newyork": "New York",
    "philadelphia": "Philadelphia",
    "sacramento": "Sacramento",
    "seattle": "Seattle",
}

# In-progress choices per chat: {chat_id: {"type": "home", "city": "seattle"}}.
# In-memory only, so it is lost on restart.
search_state: dict[int, dict[str, str]] = {}


def type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"type:{key}") for key, label in PROPERTY_TYPES.items()]]
    )


def city_keyboard() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(label, callback_data=f"city:{key}") for key, label in CITIES.items()]
    return InlineKeyboardMarkup([buttons[:2], buttons[2:]])


async def begin_search(message: Message) -> None:
    search_state[message.chat_id] = {}
    await message.reply_text("What are you looking for?", reply_markup=type_keyboard())


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await begin_search(update.message)


async def on_search_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await begin_search(query.message)


async def on_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    if key not in PROPERTY_TYPES:
        return
    search_state.setdefault(query.message.chat_id, {})["type"] = key
    await query.edit_message_text(
        f"{PROPERTY_TYPES[key]} - which city?", reply_markup=city_keyboard()
    )


async def on_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    if key not in CITIES:
        return
    state = search_state.get(query.message.chat_id, {})
    if "type" not in state:
        # Stale button (e.g. bot restarted): start over.
        await query.edit_message_text("Let's start again. What are you looking for?", reply_markup=type_keyboard())
        search_state[query.message.chat_id] = {}
        return
    state["city"] = key
    chat_id = query.message.chat_id
    await query.edit_message_text(
        f"Searching {PROPERTY_TYPES[state['type']]} listings in {CITIES[key]}..."
    )
    search_state.pop(chat_id, None)
    await send_results(query.message, state["type"], key)


def _public_https(url: str | None) -> bool:
    """Telegram rejects URL buttons that aren't public https links (e.g. localhost)."""
    if not url:
        return False
    parts = urlparse(url)
    return parts.scheme == "https" and parts.hostname not in (None, "localhost", "127.0.0.1")


def _photo_url(listing: dict) -> str | None:
    for candidate in [*(listing.get("photos") or []), listing.get("image_url")]:
        if candidate and candidate.startswith(("http://", "https://")):
            return candidate
    return None


def format_listing(listing: dict, page_url: str | None, link_in_text: bool) -> str:
    rating = listing.get("rating_avg")
    count = listing.get("review_count") or 0
    if rating is None:
        stars = "No reviews yet"
    else:
        stars = f"{rating:.1f} ({count} review{'s' if count != 1 else ''})"
    lines = [
        f"<b>{html.escape(listing['title'])}</b>",
        f"City: {html.escape(listing['location'])}",
        f"Price: ${listing['price']:g} per {listing.get('price_unit', 'night')}",
        f"Rating: \u2605 {stars}",
    ]
    if link_in_text and page_url:
        lines.append(f'<a href="{html.escape(page_url)}">View on site</a>')
    return "\n".join(lines)


ALL_CITIES = "all"


def _where(city_key: str) -> str:
    return "all cities" if city_key == ALL_CITIES else CITIES[city_key]


def nav_keyboard(listing_type: str, city_key: str, page: int, total: int) -> InlineKeyboardMarkup:
    row = []
    if page > 1:
        row.append(InlineKeyboardButton("\u2190 Back", callback_data=f"pg:{listing_type}:{city_key}:{page - 1}"))
    if page * RESULT_LIMIT < total:
        row.append(InlineKeyboardButton(f"Next {RESULT_LIMIT} \u2192", callback_data=f"pg:{listing_type}:{city_key}:{page + 1}"))
    rows = [row] if row else []
    rows.append([InlineKeyboardButton("New search", callback_data="search")])
    return InlineKeyboardMarkup(rows)


def empty_keyboard(listing_type: str, city_key: str) -> InlineKeyboardMarkup:
    other_type = "car" if listing_type == "home" else "home"
    rows = []
    if city_key != ALL_CITIES:
        rows.append([InlineKeyboardButton("Search all cities", callback_data=f"nr:all:{listing_type}")])
    rows.append([InlineKeyboardButton("Pick another city", callback_data=f"nr:city:{listing_type}")])
    rows.append(
        [InlineKeyboardButton(f"Try {PROPERTY_TYPES[other_type]}s instead", callback_data=f"nr:type:{other_type}")]
    )
    return InlineKeyboardMarkup(rows)


async def send_results(message: Message, listing_type: str, city_key: str, page: int = 1) -> None:
    city = None if city_key == ALL_CITIES else CITIES[city_key]
    try:
        listings, total = await search_listings(listing_type, city, page, RESULT_LIMIT)
    except httpx.HTTPError as e:
        log.error("Listing search failed: %s %s", e.__class__.__name__, e)
        await message.reply_text("Sorry, I couldn't reach the listings service. Please try again later.")
        return
    except Exception:
        log.exception("Unexpected error during listing search")
        await message.reply_text("Sorry, something went wrong while searching.")
        return

    kind = PROPERTY_TYPES[listing_type].lower()
    if not listings and page == 1:
        await message.reply_text(
            f"No {kind} listings in {_where(city_key)} right now. "
            "Want to widen your search?",
            reply_markup=empty_keyboard(listing_type, city_key),
        )
        return
    if not listings:
        # Page past the end (e.g. listings were removed since the last page).
        await message.reply_text(
            "That's all the results.", reply_markup=nav_keyboard(listing_type, city_key, page, 0)
        )
        return

    for listing in listings[:RESULT_LIMIT]:
        page_url = listing_page_url(listing)
        use_button = _public_https(page_url)
        row = []
        if use_button:
            row.append(InlineKeyboardButton("View on site", url=page_url))
        row.append(InlineKeyboardButton("Book this", callback_data=f"book:{listing['id']}"))
        markup = InlineKeyboardMarkup([row])
        caption = format_listing(listing, page_url, link_in_text=not use_button)
        photo = _photo_url(listing)
        try:
            if photo:
                await message.reply_photo(photo, caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup)
                continue
        except TelegramError as e:
            log.warning("Could not send photo for listing %s: %s", listing["id"], e)
        await message.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=markup)

    first = (page - 1) * RESULT_LIMIT + 1
    last = first + len(listings) - 1
    shown = str(first) if first == last else f"{first}-{last}"
    await message.reply_text(
        f"Showing {shown} of {total} {kind} listings in {_where(city_key)}.",
        reply_markup=nav_keyboard(listing_type, city_key, page, total),
    )


async def on_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, listing_type, city_key, page = query.data.split(":")
    if listing_type not in PROPERTY_TYPES or (city_key not in CITIES and city_key != ALL_CITIES) or not page.isdigit():
        return
    # Retire the old buttons so a page can't be paged twice from the same message.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass
    await send_results(query.message, listing_type, city_key, max(int(page), 1))


async def on_no_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action, arg = parts[1], parts[2]
    if action == "all" and arg in PROPERTY_TYPES:
        await query.edit_message_text(f"Searching all cities for {PROPERTY_TYPES[arg].lower()} listings...")
        await send_results(query.message, arg, ALL_CITIES)
    elif action == "city" and arg in PROPERTY_TYPES:
        search_state[query.message.chat_id] = {"type": arg}
        await query.edit_message_text(f"{PROPERTY_TYPES[arg]} - which city?", reply_markup=city_keyboard())
    elif action == "type" and arg in PROPERTY_TYPES:
        search_state[query.message.chat_id] = {"type": arg}
        await query.edit_message_text(f"{PROPERTY_TYPES[arg]} - which city?", reply_markup=city_keyboard())


async def on_book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    listing_id = query.data.split(":", 1)[1]
    log.info("Book requested for listing %s", listing_id)
    await query.message.reply_text("Booking from Telegram is coming soon. For now, use \"View on site\" to book.")


handlers = [
    CommandHandler("search", search_command),
    CallbackQueryHandler(on_search_button, pattern=r"^search$"),
    CallbackQueryHandler(on_type, pattern=r"^type:"),
    CallbackQueryHandler(on_city, pattern=r"^city:"),
    CallbackQueryHandler(on_book, pattern=r"^book:"),
    CallbackQueryHandler(on_page, pattern=r"^pg:"),
    CallbackQueryHandler(on_no_results, pattern=r"^nr:"),
]
