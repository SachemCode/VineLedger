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
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def _resolve_src(arg_src: str | None) -> Path:
    if arg_src:
        return Path(arg_src).expanduser().resolve()
    env = os.environ.get("VINELEDGER_SQLITE_PATH", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path("school.db").resolve()


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

    src = _resolve_src(args.src)
    if not src.is_file():
        print(f"Error: source database not found: {src}", file=sys.stderr)
        return 1

    dest_dir = Path(args.dest_dir).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = dest_dir / f"{args.name_prefix}_{stamp}.db"

    src_conn = sqlite3.connect(str(src), timeout=60.0)
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            # Consistent snapshot; safe while Streamlit holds other connections (WAL).
            src_conn.backup(dest_conn)
            dest_conn.commit()
        finally:
            dest_conn.close()
    finally:
        src_conn.close()

    size_kb = dest.stat().st_size / 1024.0
    print(f"OK: {src} -> {dest} ({size_kb:.1f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
