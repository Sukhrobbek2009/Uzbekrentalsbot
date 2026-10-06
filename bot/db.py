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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bot_users (
    chat_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    role TEXT,
    notify INTEGER NOT NULL DEFAULT 1,
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
        _ensure_column(conn, "bot_users", "notify", "INTEGER NOT NULL DEFAULT 1")


def add_listing(owner_chat_id: int, photo_file_id: str, location: str, phone: str, price: float) -> int:
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO listings (owner_chat_id, photo_file_id, location, phone, price) VALUES (?, ?, ?, ?, ?)",
            (owner_chat_id, photo_file_id, location, phone, price),
        )
        return cur.lastrowid


def set_channel_message(listing_id: int, message_id: int) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE listings SET channel_message_id = ? WHERE id = ?", (message_id, listing_id))


def get_listing(listing_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()


def taken_slots(listing_id: int, day: str) -> set[str]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT slot FROM bookings WHERE listing_id = ? AND day = ? AND status = 'confirmed'",
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


def confirm_booking(booking_id: int, charge_id: str) -> bool:
    """Mark a booking paid. False if someone else already confirmed the same slot."""
    try:
        with closing(_connect()) as conn, conn:
            conn.execute(
                "UPDATE bookings SET status = 'confirmed', charge_id = ? WHERE id = ?", (charge_id, booking_id)
            )
        return True
    except sqlite3.IntegrityError:
        with closing(_connect()) as conn, conn:
            conn.execute("UPDATE bookings SET status = 'needs_refund', charge_id = ? WHERE id = ?", (charge_id, booking_id))
        return False


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
