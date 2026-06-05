#!/usr/bin/env python3
"""
Rebuild student balances from app_action_audit (Manage Student saves).

Use when balances were lost but each save was audited with JSON `detail` containing
`balance` and ideally `balance_status` (balance_status is logged for newer app versions).

Examples:
  python scripts/recover_balances_from_audit.py school.db
  python scripts/recover_balances_from_audit.py --apply school.db
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

# Repo root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import (  # noqa: E402
    BALANCE_STATUS_CLEARED,
    BALANCE_STATUS_NOT_SET,
    BALANCE_STATUS_SET,
    apply_student_balance_entry,
)


def _parse_detail(detail: str | None) -> dict | None:
    if not detail or not str(detail).strip():
        return None
    try:
        return json.loads(detail)
    except json.JSONDecodeError:
        return None


def latest_balance_by_student_id(conn: sqlite3.Connection) -> dict[int, tuple[str, float | None]]:
    """Map student id -> (balance_status, balance_amount or None). Latest audit row wins."""
    cur = conn.cursor()
    if not cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='app_action_audit'"
    ).fetchone():
        return {}
    rows = cur.execute(
        """
        SELECT entity_id, detail
        FROM app_action_audit
        WHERE entity_type = 'student' AND entity_id IS NOT NULL AND detail IS NOT NULL
        ORDER BY id ASC
        """
    ).fetchall()
    out: dict[int, tuple[str, float | None]] = {}
    for eid, detail in rows:
        sid = int(eid)
        chunk = _parse_detail(detail)
        if not chunk:
            continue
        bstat = chunk.get("balance_status")
        bal = chunk.get("balance")
        if bstat in (BALANCE_STATUS_NOT_SET, "not_set"):
            out[sid] = (BALANCE_STATUS_NOT_SET, None)
            continue
        if bstat in (BALANCE_STATUS_CLEARED, "cleared"):
            out[sid] = (BALANCE_STATUS_CLEARED, 0.0)
            continue
        if bstat in (BALANCE_STATUS_SET, "set"):
            try:
                amt = float(bal) if bal is not None else 0.0
            except (TypeError, ValueError):
                continue
            out[sid] = (BALANCE_STATUS_SET, amt)
            continue
        # Older audits: balance only
        if bal is None:
            continue
        try:
            amt = float(bal)
        except (TypeError, ValueError):
            continue
        if abs(amt) < 0.01:
            out[sid] = (BALANCE_STATUS_CLEARED, 0.0)
        else:
            out[sid] = (BALANCE_STATUS_SET, amt)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Recover student balances from app_action_audit.")
    ap.add_argument("db_path", nargs="?", default=os.environ.get("VINELEDGER_SQLITE_PATH", "school.db"))
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Write balances via apply_student_balance_entry (otherwise dry-run preview).",
    )
    args = ap.parse_args()
    path = os.path.abspath(args.db_path)
    if not os.path.isfile(path):
        print(f"Database not found: {path}", file=sys.stderr)
        return 1

    from database import configure_sqlite_connection

    conn = sqlite3.connect(path, check_same_thread=False)
    configure_sqlite_connection(conn)
    try:
        by_sid = latest_balance_by_student_id(conn)
        if not by_sid:
            print("No student audit rows with parseable balance detail found.")
            return 0
        print(f"Found latest balance snapshot for {len(by_sid)} student id(s).")
        for sid in sorted(by_sid.keys()):
            st, amt = by_sid[sid]
            print(f"  id={sid}  {st!r}  amount={amt!r}")

        if not args.apply:
            print("\nDry run only. Re-run with --apply to write these balances.")
            return 0

        for sid in sorted(by_sid.keys()):
            st, amt = by_sid[sid]
            apply_student_balance_entry(conn, sid, st, amt, do_commit=False)
        conn.commit()
        print("\nApplied. Verify in Manage Students / roster.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
