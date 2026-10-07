import os
import sqlite3
from contextlib import closing
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_chat_id INTEGER NOT NULL,
    photo_file_id TEXT NOT NULL,
    location TEXT NOT NULL,
    phone TEXT NOT NULL,
    price REAL NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    renter_photo_file_id TEXT,
    latitude REAL,
    longitude REAL,
    status TEXT NOT NULL DEFAULT 'active',
    channel_message_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    renter_chat_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    slot TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_payment',
    charge_id TEXT,
    proof_file_id TEXT,
    host_proof_file_id TEXT,
    reject_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    renter_chat_id INTEGER NOT NULL,
    sender TEXT NOT NULL CHECK (sender IN ('renter', 'host')),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    booking_id INTEGER NOT NULL UNIQUE REFERENCES bookings(id),
    renter_chat_id INTEGER NOT NULL,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bot_users (
    chat_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    role TEXT,
    notify INTEGER NOT NULL DEFAULT 1,
    card_number TEXT,
    card_holder TEXT,
    registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS account_links (
    chat_id INTEGER PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    user_id TEXT NOT NULL,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL,
    linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- A slot can only be confirmed once, even if two renters pay at the same moment.
CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_slot
    ON bookings(listing_id, day, slot) WHERE status = 'confirmed';
"""


def _connect() -> sqlite3.Connection:
    path = Path(os.environ.get("BOT_DB_PATH", "data/bot.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Add a column to a table created by an earlier version of the schema."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    with closing(_connect()) as conn, conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "listings", "status", "TEXT NOT NULL DEFAULT 'active'")
        _ensure_column(conn, "bot_users", "card_number", "TEXT")
        _ensure_column(conn, "bot_users", "card_holder", "TEXT")
        _ensure_column(conn, "bookings", "proof_file_id", "TEXT")
        _ensure_column(conn, "bookings", "host_proof_file_id", "TEXT")
        _ensure_column(conn, "bookings", "reject_reason", "TEXT")
        _ensure_column(conn, "listings", "description", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "listings", "renter_photo_file_id", "TEXT")
        _ensure_column(conn, "listings", "latitude", "REAL")
        _ensure_column(conn, "listings", "longitude", "REAL")
        _ensure_column(conn, "bot_users", "notify", "INTEGER NOT NULL DEFAULT 1")


def add_listing(
    owner_chat_id: int,
    photo_file_id: str,
    location: str,
    phone: str,
    price: float,
    description: str = "",
    latitude: float | None = None,
    longitude: float | None = None,
) -> int:
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO listings (owner_chat_id, photo_file_id, location, phone, price, description, latitude, longitude)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_chat_id, photo_file_id, location, phone, price, description, latitude, longitude),
        )
        return cur.lastrowid


def set_renter_photo(listing_id: int, file_id: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE listings SET renter_photo_file_id = ? WHERE id = ?", (file_id, listing_id))


def set_channel_message(listing_id: int, message_id: int) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE listings SET channel_message_id = ? WHERE id = ?", (message_id, listing_id))


def get_listing(listing_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()


def taken_slots(listing_id: int, day: str) -> set[str]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT slot FROM bookings WHERE listing_id = ? AND day = ? AND status IN ('confirmed', 'pending_review')",
            (listing_id, day),
        ).fetchall()
    return {r["slot"] for r in rows}


def create_booking(listing_id: int, renter_chat_id: int, day: str, slot: str, amount_minor: int, currency: str) -> int:
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO bookings (listing_id, renter_chat_id, day, slot, amount_minor, currency) VALUES (?, ?, ?, ?, ?, ?)",
            (listing_id, renter_chat_id, day, slot, amount_minor, currency),
        )
        return cur.lastrowid


def get_booking(booking_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()


def confirm_booking(booking_id: int) -> bool:
    """Host accepted the payment. False if another booking already holds the confirmed slot."""
    try:
        with closing(_connect()) as conn, conn:
            conn.execute("UPDATE bookings SET status = 'confirmed' WHERE id = ?", (booking_id,))
        return True
    except sqlite3.IntegrityError:
        return False


def submit_proof(booking_id: int, proof_file_id: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE bookings SET status = 'pending_review', proof_file_id = ?, host_proof_file_id = NULL,"
            " reject_reason = NULL WHERE id = ?",
            (proof_file_id, booking_id),
        )


def set_host_proof(booking_id: int, file_id: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE bookings SET host_proof_file_id = ? WHERE id = ?", (file_id, booking_id))


def reject_booking(booking_id: int, reason: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE bookings SET status = 'rejected', reject_reason = ? WHERE id = ?", (reason, booking_id))


def booking_with_listing(booking_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT b.*, l.location, l.phone AS listing_phone, l.owner_chat_id, l.price"
            " FROM bookings b JOIN listings l ON l.id = b.listing_id WHERE b.id = ?",
            (booking_id,),
        ).fetchone()


def payable_bookings(chat_id: int, today: str) -> list[sqlite3.Row]:
    """A renter's bookings that still need a payment screenshot (new or rejected)."""
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT b.*, l.location FROM bookings b JOIN listings l ON l.id = b.listing_id"
            " WHERE b.renter_chat_id = ? AND b.status IN ('pending_payment', 'rejected') AND b.day >= ?"
            " AND l.status = 'active' ORDER BY b.day, b.slot",
            (chat_id, today),
        ).fetchall()


def renter_bookings(chat_id: int, today: str) -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT b.*, l.location FROM bookings b JOIN listings l ON l.id = b.listing_id"
            " WHERE b.renter_chat_id = ? AND b.day >= ? ORDER BY b.day, b.slot",
            (chat_id, today),
        ).fetchall()


def pending_reviews(owner_chat_id: int) -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT b.*, l.location FROM bookings b JOIN listings l ON l.id = b.listing_id"
            " WHERE l.owner_chat_id = ? AND b.status = 'pending_review' ORDER BY b.id",
            (owner_chat_id,),
        ).fetchall()


def set_name(chat_id: int, name: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE bot_users SET name = ? WHERE chat_id = ?", (name, chat_id))


def set_phone(chat_id: int, phone: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE bot_users SET phone = ? WHERE chat_id = ?", (phone, chat_id))


def set_card(chat_id: int, number: str | None, holder: str | None) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE bot_users SET card_number = ?, card_holder = ? WHERE chat_id = ?", (number, holder, chat_id))


def save_link(chat_id: int, access_token: str, refresh_token: str, user: dict) -> None:
    """Link a Telegram chat to a Vatan Rentals account (user is the API's UserOut)."""
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO account_links (chat_id, access_token, refresh_token, user_id, full_name, email, role)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, access_token, refresh_token, user["id"], user["full_name"], user["email"], user["role"]),
        )


def get_link(chat_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM account_links WHERE chat_id = ?", (chat_id,)).fetchone()


def update_access_token(chat_id: int, access_token: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE account_links SET access_token = ? WHERE chat_id = ?", (access_token, chat_id))


def delete_link(chat_id: int) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM account_links WHERE chat_id = ?", (chat_id,))


def save_user(chat_id: int, name: str, phone: str) -> None:
    """Register (or re-register) a Telegram user; an existing role is kept."""
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO bot_users (chat_id, name, phone) VALUES (?, ?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET name = excluded.name, phone = excluded.phone",
            (chat_id, name, phone),
        )


def get_user(chat_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM bot_users WHERE chat_id = ?", (chat_id,)).fetchone()


def set_role(chat_id: int, role: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE bot_users SET role = ? WHERE chat_id = ?", (role, chat_id))


# --- listing management (status: active / inactive / deleted; deleted rows are
# kept so booking and payment history still has something to point at) ---

def owner_listings(chat_id: int) -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT * FROM listings WHERE owner_chat_id = ? AND status != 'deleted' ORDER BY id DESC", (chat_id,)
        ).fetchall()


def set_listing_status(listing_id: int, status: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE listings SET status = ? WHERE id = ?", (status, listing_id))


def upcoming_confirmed_count(listing_id: int, today: str) -> int:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE listing_id = ? AND status = 'confirmed' AND day >= ?",
            (listing_id, today),
        ).fetchone()[0]


# --- payments / home summary ---

def payments_made(chat_id: int) -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT b.*, l.location FROM bookings b JOIN listings l ON l.id = b.listing_id"
            " WHERE b.renter_chat_id = ? AND b.status IN ('confirmed', 'needs_refund') ORDER BY b.id DESC",
            (chat_id,),
        ).fetchall()


def payments_received(chat_id: int) -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT b.*, l.location FROM bookings b JOIN listings l ON l.id = b.listing_id"
            " WHERE l.owner_chat_id = ? AND b.status = 'confirmed' ORDER BY b.id DESC",
            (chat_id,),
        ).fetchall()


def home_counts(chat_id: int, today: str) -> dict[str, int]:
    with closing(_connect()) as conn:
        one = lambda sql: conn.execute(sql, (chat_id, today)).fetchone()[0]
        return {
            "active_listings": conn.execute(
                "SELECT COUNT(*) FROM listings WHERE owner_chat_id = ? AND status = 'active'", (chat_id,)
            ).fetchone()[0],
            "bookings_made": one(
                "SELECT COUNT(*) FROM bookings WHERE renter_chat_id = ? AND status = 'confirmed' AND day >= ?"
            ),
            "bookings_received": one(
                "SELECT COUNT(*) FROM bookings b JOIN listings l ON l.id = b.listing_id"
                " WHERE l.owner_chat_id = ? AND b.status = 'confirmed' AND b.day >= ?"
            ),
        }


def set_notify(chat_id: int, on: bool) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE bot_users SET notify = ? WHERE chat_id = ?", (1 if on else 0, chat_id))


# --- reviews (one per booking, only for confirmed bookings whose day has come) ---

def reviewable_bookings(chat_id: int, today: str) -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT b.*, l.location FROM bookings b JOIN listings l ON l.id = b.listing_id"
            " WHERE b.renter_chat_id = ? AND b.status = 'confirmed' AND b.day <= ?"
            " AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.booking_id = b.id) ORDER BY b.day DESC",
            (chat_id, today),
        ).fetchall()


def add_review(booking_id: int, rating: int, comment: str) -> bool:
    """Save a review for a booking. False if that booking already has one."""
    try:
        with closing(_connect()) as conn, conn:
            conn.execute(
                "INSERT INTO reviews (listing_id, booking_id, renter_chat_id, rating, comment)"
                " SELECT listing_id, id, renter_chat_id, ?, ? FROM bookings WHERE id = ?",
                (rating, comment, booking_id),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def review_stats(listing_id: int) -> tuple[float, int]:
    """(average rating, number of reviews); (0, 0) when there are none."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT AVG(rating), COUNT(*) FROM reviews WHERE listing_id = ?", (listing_id,)
        ).fetchone()
    return (row[0] or 0.0, row[1])


def latest_review(listing_id: int) -> sqlite3.Row | None:
    """Newest review that has a written comment."""
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT r.*, u.name FROM reviews r LEFT JOIN bot_users u ON u.chat_id = r.renter_chat_id"
            " WHERE r.listing_id = ? AND r.comment != '' ORDER BY r.id DESC LIMIT 1",
            (listing_id,),
        ).fetchone()


# --- renter <-> host questions (relayed by the two bots; neither sees the other's chat) ---

def add_message(listing_id: int, renter_chat_id: int, sender: str, text: str) -> int:
    with closing(_connect()) as conn, conn:
        return conn.execute(
            "INSERT INTO messages (listing_id, renter_chat_id, sender, text) VALUES (?, ?, ?, ?)",
            (listing_id, renter_chat_id, sender, text),
        ).lastrowid


def recent_renter_messages(renter_chat_id: int, minutes: int = 60) -> int:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM messages WHERE renter_chat_id = ? AND sender = 'renter'"
            " AND created_at >= datetime('now', ?)",
            (renter_chat_id, f"-{minutes} minutes"),
        ).fetchone()[0]
