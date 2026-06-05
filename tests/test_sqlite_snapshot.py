"""SQLite snapshot helpers (WAL-safe backup)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db_module
from database import init_db, resolve_sqlite_database_path, snapshot_sqlite_database_bytes


class TestSqliteSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev = db_module.SQLITE_DB_PATH
        db_module.SQLITE_DB_PATH = os.path.join(self.tmp.name, "live.db")
        self.conn = init_db()
        self.conn.execute(
            """INSERT INTO students (student_code, name, grade, balance, balance_set, balance_status, status)
               VALUES ('T1', 'A', 'Grade 1', 0, 1, 'cleared', 'Active')"""
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        db_module.SQLITE_DB_PATH = self._prev
        self.tmp.cleanup()

    def test_snapshot_bytes_roundtrip(self):
        p = resolve_sqlite_database_path(None)
        self.assertTrue(p.is_file())
        raw = snapshot_sqlite_database_bytes()
        self.assertGreater(len(raw), 100)
        fd, out = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with open(out, "wb") as f:
                f.write(raw)
            import sqlite3

            c = sqlite3.connect(out)
            try:
                n = c.execute("SELECT COUNT(*) FROM students").fetchone()[0]
                self.assertEqual(int(n), 1)
            finally:
                c.close()
        finally:
            os.unlink(out)


if __name__ == "__main__":
    unittest.main()
