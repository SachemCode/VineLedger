"""Grade ordering and dashboard admission/exit list helpers."""
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import init_db
from school_calendar import list_new_admissions_this_term, list_student_exits_this_term
from utils import (
    grade_progression_through,
    grade_sort_key,
    sort_grade_labels,
)


def _fresh_db():
    tmp = tempfile.TemporaryDirectory()
    os.chdir(tmp.name)
    return init_db(), tmp


class GradeProgressionTests(unittest.TestCase):
    def test_sort_grade_labels_follows_school_order(self):
        shuffled = ["Grade 9", "PP1", "Grade 1", "Playgroup", "PP2"]
        self.assertEqual(
            sort_grade_labels(shuffled),
            ["Playgroup", "PP1", "PP2", "Grade 1", "Grade 9"],
        )

    def test_grade_sort_key_unknown_after_known(self):
        self.assertLess(grade_sort_key("Grade 2"), grade_sort_key("Legacy"))

    def test_progression_through_grade_3(self):
        self.assertEqual(
            grade_progression_through("Grade 3"),
            "Playgroup → PP1 → PP2 → Grade 1 → Grade 2 → Grade 3",
        )


class AdmissionExitListTests(unittest.TestCase):
    def test_new_admissions_list_in_term_window(self):
        conn, tmp = _fresh_db()
        try:
            conn.execute(
                """INSERT INTO students
                   (name, student_code, grade, joined_date, status, balance)
                   VALUES (?, ?, ?, ?, 'Active', 0)""",
                ("Ada", "V001", "Grade 1", "2026-01-15"),
            )
            conn.commit()
            rows = list_new_admissions_this_term(conn, today=date(2026, 5, 15))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "Ada")
            self.assertEqual(rows[0]["joined_date"], "2026-01-15")
        finally:
            tmp.cleanup()

    def test_exits_list_excludes_graduated(self):
        conn, tmp = _fresh_db()
        try:
            conn.execute(
                """INSERT INTO students
                   (name, student_code, grade, joined_date, exited_at, status, balance)
                   VALUES (?, ?, ?, '2020-01-01', ?, 'Transferred', 0)""",
                ("Bob", "V002", "Grade 5", "2026-02-01"),
            )
            conn.execute(
                """INSERT INTO students
                   (name, student_code, grade, joined_date, exited_at, status, balance)
                   VALUES (?, ?, ?, '2020-01-01', ?, 'Graduated', 0)""",
                ("Cara", "V003", "Grade 9", "2026-02-01"),
            )
            conn.commit()
            rows = list_student_exits_this_term(conn, today=date(2026, 5, 15))
            names = [r["name"] for r in rows]
            self.assertIn("Bob", names)
            self.assertNotIn("Cara", names)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
