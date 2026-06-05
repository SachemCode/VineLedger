#!/usr/bin/env python3
"""
Online backup of VineLedger SQLite (WAL-safe).

Run on the same host that owns the live database (the machine where Streamlit runs).
Uses sqlite3.Connection.backup() for a consistent snapshot while the app may be running.

Examples:
  python scripts/backup_school_db.py
  python scripts/backup_school_db.py --src /var/lib/vineledger/school.db --dest-dir ./backups
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import resolve_sqlite_database_path, snapshot_sqlite_database_to_path  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup VineLedger school.db (online SQLite backup).")
    parser.add_argument(
        "--src",
        default=None,
        help="Path to live school.db (default: VINELEDGER_SQLITE_PATH or ./school.db)",
    )
    parser.add_argument(
        "--dest-dir",
        default="backups",
        help="Directory for timestamped backup files (created if missing)",
    )
    parser.add_argument(
        "--name-prefix",
        default="school",
        help="Backup filename prefix (default: school -> school_YYYYMMDD_HHMMSS.db)",
    )
    args = parser.parse_args()

    src = resolve_sqlite_database_path(args.src)
    if not src.is_file():
        print(f"Error: source database not found: {src}", file=sys.stderr)
        return 1

    dest_dir = Path(args.dest_dir).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = dest_dir / f"{args.name_prefix}_{stamp}.db"

    snapshot_sqlite_database_to_path(dest, src_path=args.src)

    size_kb = dest.stat().st_size / 1024.0
    print(f"OK: {src} -> {dest} ({size_kb:.1f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
