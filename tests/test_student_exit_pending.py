"""Pending transfer/deletion drafts and schedule helpers."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db_module
from database import init_db
from utils import (
    DRAFT_TYPE_STUDENT_DELETION,
    DRAFT_TYPE_STUDENT_TRANSFER,
    delete_pending_student_deletion_review,
    delete_pending_student_transfer_review,
    fetch_all_pending_reviews,
    get_student_record,
    schedule_student_deletion,
    schedule_student_transfer,
    upsert_pending_student_deletion_review,
    upsert_pending_student_transfer_review,
)


class TestStudentExitPending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev_db_path = db_module.SQLITE_DB_PATH
        db_module.SQLITE_DB_PATH = os.path.join(self.tmp.name, "test.db")
        self.conn = init_db()
        self.conn.execute(
            """INSERT INTO students (student_code, name, grade, balance, status)
               VALUES ('E001', 'Exit Test', 'Grade 2', 0, 'Active')"""
        )
        self.conn.commit()
        self.sid = int(self.conn.execute("SELECT id FROM students").fetchone()[0])

    def tearDown(self):
        self.conn.close()
        db_module.SQLITE_DB_PATH = self._prev_db_path
        self.tmp.cleanup()

    def test_transfer_pending_round_trip(self):
        upsert_pending_student_transfer_review(
            self.conn, self.sid, {"transfer_reason": "Moved away"}
        )
        data = fetch_all_pending_reviews(self.conn)
        self.assertIn(self.sid, data["student_transfers"])
        schedule_student_transfer(self.conn, self.sid, "Moved away")
        rec = get_student_record(self.conn, self.sid)
        self.assertEqual(rec["status"], "Transferred")
        delete_pending_student_transfer_review(self.conn, self.sid)
        row = self.conn.execute(
            "SELECT 1 FROM pending_reviews WHERE draft_type=? AND draft_key=?",
            (DRAFT_TYPE_STUDENT_TRANSFER, str(self.sid)),
        ).fetchone()
        self.assertIsNone(row)

    def test_deletion_pending_and_schedule(self):
        upsert_pending_student_deletion_review(
            self.conn, self.sid, {"deletion_reason": "Duplicate record"}
        )
        data = fetch_all_pending_reviews(self.conn)
        self.assertIn(self.sid, data["student_deletions"])
        schedule_student_deletion(self.conn, self.sid, "Duplicate record")
        rec = get_student_record(self.conn, self.sid)
        self.assertEqual(rec["status"], "Scheduled for Deletion")
        self.assertEqual(rec["deletion_reason"], "Duplicate record")
        delete_pending_student_deletion_review(self.conn, self.sid)


if __name__ == "__main__":
    unittest.main()
