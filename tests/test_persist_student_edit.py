"""Verify all Manage Students edit fields persist to school.db."""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import persist_student_edit, verify_student_edit_saved, get_student_record


def test_full_student_edit_persists():
    db = ROOT / "school.db"
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT id FROM students WHERE status IS NULL OR status='Active' LIMIT 1"
    ).fetchone()
    assert row, "Need at least one student in school.db"
    sid = int(row[0])
    before = get_student_record(conn, sid)

    payload = {
        "name": (before.get("name") or "Test") + "",
        "grade": before.get("grade") or "Grade 1",
        "parent_name": "Test Parent Persist",
        "parent_phone": "254712345678",
        "date_of_birth": "2015-06-15",
        "has_transport": True,
        "selected_transport_id": conn.execute(
            "SELECT id FROM fee_structure WHERE fee_category='transport' LIMIT 1"
        ).fetchone()[0],
        "has_meal": True,
        "include_admission_fees": 1,
        "co_curricular_ids": [
            conn.execute(
                "SELECT id FROM fee_structure WHERE fee_category='co_curricular' AND fee_name='Football'"
            ).fetchone()[0]
        ],
        "balance": 12345.0,
        "transport_choice": str(
            conn.execute(
                "SELECT id FROM fee_structure WHERE fee_category='transport' LIMIT 1"
            ).fetchone()[0]
        ),
    }

    persist_student_edit(conn, sid, payload)
    ok, errs = verify_student_edit_saved(conn, sid, payload)
    assert ok, f"Verification failed: {errs}"

    rec = get_student_record(conn, sid)
    assert rec["parent_name"] == "Test Parent Persist"
    assert rec["date_of_birth"] == "2015-06-15"
    assert int(rec["has_meal"]) == 1
    assert abs(float(rec["balance"]) - 12345.0) < 0.01
    cc = json.loads(rec["co_curricular_activities"])
    assert payload["co_curricular_ids"][0] in cc

    # Restore minimal fields so we do not leave test data permanently
    restore = {
        "name": before.get("name"),
        "grade": before.get("grade"),
        "parent_name": before.get("parent_name"),
        "parent_phone": before.get("parent_phone"),
        "date_of_birth": before.get("date_of_birth"),
        "has_transport": bool(before.get("has_transport")),
        "selected_transport_id": before.get("transport_route_id"),
        "has_meal": bool(before.get("has_meal")),
        "include_admission_fees": int(before.get("include_admission_fees") or 0),
        "co_curricular_ids": json.loads(before["co_curricular_activities"])
        if before.get("co_curricular_activities")
        else [],
        "balance": float(before.get("balance") or 0),
        "transport_choice": "__none__"
        if not before.get("has_transport")
        else str(before.get("transport_route_id")),
    }
    persist_student_edit(conn, sid, restore)
    conn.close()
    print("test_full_student_edit_persists: OK")


if __name__ == "__main__":
    test_full_student_edit_persists()
