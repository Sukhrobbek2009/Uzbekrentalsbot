"""Copy the bot database to backups/bot-<timestamp>.db (safe while the bots are running).

Usage: python backup_db.py [keep]   (keep = how many backups to retain, default 14)
"""
import datetime as dt
import os
import sqlite3
import sys
from pathlib import Path

src = Path(os.environ.get("BOT_DB_PATH", "data/bot.db"))
keep = int(sys.argv[1]) if len(sys.argv) > 1 else 14
if not src.exists():
    sys.exit(f"No database at {src}")
out_dir = Path("backups")
out_dir.mkdir(exist_ok=True)
dest = out_dir / f"bot-{dt.datetime.now():%Y%m%d-%H%M%S}.db"
with sqlite3.connect(src) as a, sqlite3.connect(dest) as b:
    a.backup(b)
for old in sorted(out_dir.glob("bot-*.db"))[:-keep]:
    old.unlink()
print(f"Backed up to {dest}")
