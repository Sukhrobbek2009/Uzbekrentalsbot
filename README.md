# Uzbek Rentals bots

Two Telegram bots (host and renter) sharing one SQLite database.

## Database
- File: `data/bot.db` (override with `BOT_DB_PATH`). Created and migrated automatically on startup; no manual setup.
- Tables: listings, bookings, bot_users, account_links, reviews, messages.
- Runs in WAL mode so both bot processes can use it at once.
- Backup: `python backup_db.py` (keeps the latest 14 in `backups/`). Run it daily with cron, e.g. `0 3 * * * cd /path/to/Uzbekrentalsbot && .venv/bin/python backup_db.py`.
- Restore: stop both bots, copy a backup over `data/bot.db`, start them.
- Reset: stop both bots and delete `data/bot.db*`.
