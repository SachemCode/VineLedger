"""Non-Active students cannot be edited via persist_student_edit."""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import get_student_record, persist_student_edit


def test_persist_student_edit_rejects_non_active():
    for status in ("Graduated", "Transferred", "Scheduled for Deletion"):
        _assert_rejects_status(status)


def _assert_rejects_status(status):
    db = ROOT / "school.db"
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT id FROM students WHERE status IS NULL OR status='Active' LIMIT 1"
    ).fetchone()
    assert row, "Need at least one Active student in school.db"
    sid = int(row[0])
    before = get_student_record(conn, sid)
    conn.execute("UPDATE students SET status = ? WHERE id = ?", (status, sid))
    conn.commit()
    payload = {
        "name": before.get("name") or "Test",
        "grade": before.get("grade") or "Grade 1",
        "parent_name": before.get("parent_name") or "Parent",
        "parent_phone": before.get("parent_phone") or "254700000000",
        "date_of_birth": before.get("date_of_birth"),
        "has_transport": bool(before.get("has_transport")),
        "selected_transport_id": before.get("transport_route_id"),
        "has_meal": bool(before.get("has_meal")),
        "include_admission_fees": int(before.get("include_admission_fees") or 0),
        "co_curricular_ids": [],
        "balance": float(before.get("balance") or 0),
        "transport_choice": "__none__",
    }
    try:
        try:
            persist_student_edit(conn, sid, payload)
        except ValueError as e:
            assert "Cannot edit" in str(e), str(e)
        else:
            raise AssertionError(f"Expected ValueError for status {status!r}")
    finally:
        conn.execute(
            "UPDATE students SET status = ? WHERE id = ?",
            (before.get("status") or "Active", sid),
        )
        conn.commit()
        conn.close()


if __name__ == "__main__":
    test_persist_student_edit_rejects_non_active()
    print("test_persist_student_edit_rejects_non_active: OK")
