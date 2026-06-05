"""Club roster import and name resolution."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import init_db
from utils import (
    enroll_students_in_club,
    get_co_curricular_name_to_id,
    import_club_roster_assignments,
    parse_club_roster_dataframe,
    parse_co_curricular_ids,
    resolve_students_by_names,
)


def _fresh_db():
    tmp = tempfile.TemporaryDirectory()
    os.chdir(tmp.name)
    conn = init_db()
    conn.execute(
        """INSERT INTO fee_structure (fee_name, fee_amount, fee_category)
           VALUES ('Drama', 1000, 'co_curricular')"""
    )
    conn.execute(
        """INSERT INTO students (student_code, name, grade, balance, status)
           VALUES ('0001', 'Alice Wonder', 'Grade 1', 0, 'Active'),
                  ('0002', 'Bob Builder', 'Grade 2', 0, 'Active')"""
    )
    conn.commit()
    return conn, tmp


class ClubRosterTests(unittest.TestCase):
    def test_parse_long_format(self):
        import pandas as pd

        df = pd.DataFrame(
            {
                "club": ["Drama", "Drama"],
                "student_name": ["Alice Wonder", "Bob Builder"],
            }
        )
        rows = parse_club_roster_dataframe(df)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["club_name"], "Drama")

    def test_resolve_and_enroll(self):
        conn, tmp = _fresh_db()
        try:
            club_id = list(get_co_curricular_name_to_id(conn).values())[0]
            res = resolve_students_by_names(conn, ["Alice Wonder", "Nobody Here"])
            self.assertEqual(len(res["matched"]), 1)
            self.assertEqual(res["matched"][0]["student_id"], 1)
            self.assertEqual(res["unmatched"], ["Nobody Here"])

            rep = enroll_students_in_club(conn, club_id, [1], mode="add")
            self.assertEqual(rep["updated"], 1)
            rec = conn.execute(
                "SELECT co_curricular_activities FROM students WHERE id=1"
            ).fetchone()
            ids = parse_co_curricular_ids(rec[0], conn=conn, student_id=1)
            self.assertIn(club_id, ids)
        finally:
            tmp.cleanup()

    def test_import_dry_run(self):
        conn, tmp = _fresh_db()
        try:
            assignments = [
                {"club_name": "Drama", "student_name": "Alice Wonder", "sheet_row": 2},
                {"club_name": "Drama", "student_name": "Bob Builder", "sheet_row": 3},
            ]
            rep = import_club_roster_assignments(conn, assignments, dry_run=True)
            self.assertEqual(len(rep["preview"]), 2)
            self.assertEqual(rep["students_updated"], 0)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
