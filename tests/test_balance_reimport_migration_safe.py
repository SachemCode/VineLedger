"""balance_reimport migration must not clear existing learner balances."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db_module
from database import _maybe_balance_reimport_reset, init_db
from utils import BALANCE_STATUS_SET, get_student_record, student_balance_status


class TestBalanceReimportMigrationSafe(unittest.TestCase):
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

    def test_reimport_reset_does_not_wipe_balances(self):
        self.conn.execute("UPDATE school_calendar_settings SET balance_reimport_reset_done=0")
        self.conn.commit()
        _maybe_balance_reimport_reset(self.conn)
        self.conn.commit()
        rec = get_student_record(self.conn, self.sid)
        self.assertEqual(student_balance_status(rec), BALANCE_STATUS_SET)
        self.assertEqual(float(rec.get("balance") or 0), 5000.0)
        flag = self.conn.execute(
            "SELECT balance_reimport_reset_done FROM school_calendar_settings WHERE id=1"
        ).fetchone()[0]
        self.assertEqual(int(flag or 0), 1)


if __name__ == "__main__":
    unittest.main()
