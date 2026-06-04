"""balance_set flag and reset for spreadsheet re-import."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db_module
from database import init_db
from school_calendar import reset_all_student_balances_not_set
from utils import (
    BALANCE_STATUS_CLEARED,
    BALANCE_STATUS_NOT_SET,
    BALANCE_STATUS_SET,
    apply_student_balance_entry,
    apply_student_balance_override,
    format_student_balance_display,
    get_student_record,
    parse_balance_editor_value,
    student_balance_is_outstanding,
    student_balance_is_set,
    student_balance_status,
)


class TestBalanceNotSet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev_db_path = db_module.SQLITE_DB_PATH
        db_module.SQLITE_DB_PATH = os.path.join(self.tmp.name, "test.db")
        self.conn = init_db()
        self.conn.execute(
            """INSERT INTO students (student_code, name, grade, balance, balance_set, balance_status, status)
               VALUES ('T001', 'Test Learner', 'Grade 1', 5000, 1, 'set', 'Active')"""
        )
        self.conn.commit()
        self.sid = int(self.conn.execute("SELECT id FROM students").fetchone()[0])

    def tearDown(self):
        self.conn.close()
        db_module.SQLITE_DB_PATH = self._prev_db_path
        self.tmp.cleanup()

    def test_reset_marks_not_set(self):
        reset_all_student_balances_not_set(self.conn)
        rec = get_student_record(self.conn, self.sid)
        self.assertEqual(student_balance_status(rec), BALANCE_STATUS_NOT_SET)
        self.assertEqual(format_student_balance_display(student_row=rec), "Not set")

    def test_cleared_and_amount(self):
        reset_all_student_balances_not_set(self.conn)
        apply_student_balance_entry(self.conn, self.sid, BALANCE_STATUS_CLEARED, 0.0)
        rec = get_student_record(self.conn, self.sid)
        self.assertEqual(student_balance_status(rec), BALANCE_STATUS_CLEARED)
        self.assertEqual(format_student_balance_display(student_row=rec), "Cleared")
        self.assertFalse(student_balance_is_outstanding(rec))

        apply_student_balance_override(self.conn, self.sid, 1200)
        rec = get_student_record(self.conn, self.sid)
        self.assertEqual(student_balance_status(rec), BALANCE_STATUS_SET)
        self.assertEqual(float(rec["balance"]), 1200.0)
        self.assertTrue(student_balance_is_outstanding(rec))

    def test_parse_editor_values(self):
        self.assertEqual(parse_balance_editor_value("Not set"), (BALANCE_STATUS_NOT_SET, None))
        self.assertEqual(parse_balance_editor_value("Cleared"), (BALANCE_STATUS_CLEARED, 0.0))
        self.assertEqual(parse_balance_editor_value("5000"), (BALANCE_STATUS_SET, 5000.0))
        self.assertEqual(parse_balance_editor_value("0"), (BALANCE_STATUS_CLEARED, 0.0))


if __name__ == "__main__":
    unittest.main()
