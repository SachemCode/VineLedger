"""Balance roster import and bulk helpers."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from database import init_db
from utils import (
    balance_roster_detected_columns,
    import_balance_roster_updates,
    parse_balance_cell,
    parse_balance_roster_dataframe,
    prepare_balance_roster_dataframe,
)


def _fresh_db():
    tmp = tempfile.TemporaryDirectory()
    os.chdir(tmp.name)
    conn = init_db()
    conn.execute(
        """INSERT INTO students (student_code, name, grade, balance, status)
           VALUES ('0001', 'Alice Wonder', 'Grade 1', 5000, 'Active')"""
    )
    conn.commit()
    return conn, tmp


class BalanceRosterTests(unittest.TestCase):
    def test_parse_balance_cell(self):
        self.assertEqual(parse_balance_cell("KSH 15,000"), 15000.0)
        self.assertEqual(parse_balance_cell(0), 0.0)

    def test_parse_dataframe(self):
        df = pd.DataFrame(
            {
                "grade": ["Grade 1"],
                "student_name": ["Alice Wonder"],
                "balance": ["12000"],
            }
        )
        rows = parse_balance_roster_dataframe(df)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["balance"], 12000.0)

    def test_parse_fee_sheet_title_row_then_headers(self):
        """Row 1 title, row 2 NAMES/BALANCE headers (pandas read with wrong header row)."""
        df = pd.DataFrame(
            [
                [None, None, "BALANCE", "DIARY", None, None, None, None, None, None, None, None, None, None, None, "NAMES"],
                [None, None, 1500, "yes", None, None, None, None, None, None, None, None, None, None, 1, "RODGERS MAINA"],
                [None, None, 1050, None, None, None, None, None, None, None, None, None, None, None, 2, "RONNIE AGUMA"],
                [None, None, 91850, None, None, None, None, None, None, None, None, None, None, None, None, "total"],
            ],
            columns=[f"c{i}" for i in range(16)],
        )
        prepared, start_row = prepare_balance_roster_dataframe(df)
        det = balance_roster_detected_columns(prepared)
        self.assertEqual(det["student_name"], "NAMES")
        self.assertEqual(det["balance"], "BALANCE")
        self.assertEqual(start_row, 3)
        rows = parse_balance_roster_dataframe(df, default_grade="Grade 1")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["student_name"], "RODGERS MAINA")
        self.assertEqual(rows[0]["balance"], 1500.0)
        self.assertEqual(rows[1]["balance"], 1050.0)

    def test_parse_names_and_balance_aliases(self):
        df = pd.DataFrame(
            {
                "NAMES": ["Alice Wonder", "total"],
                "BALANCE": [12000, 99999],
            }
        )
        rows = parse_balance_roster_dataframe(df, default_grade="Grade 1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["balance"], 12000.0)

    def test_import_updates_balance(self):
        conn, tmp = _fresh_db()
        try:
            rows = [
                {
                    "grade": "Grade 1",
                    "student_name": "Alice Wonder",
                    "balance": 9000.0,
                    "sheet_row": 2,
                }
            ]
            rep = import_balance_roster_updates(conn, rows, dry_run=False)
            self.assertEqual(rep["updated"], 1)
            rec = conn.execute("SELECT balance FROM students WHERE id=1").fetchone()
            self.assertEqual(float(rec[0]), 9000.0)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
