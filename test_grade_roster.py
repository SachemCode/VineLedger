"""Grade roster contact import and parsing."""
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
    apply_student_contact_patch,
    get_student_record,
    grade_roster_detected_columns,
    import_grade_roster_updates,
    infer_grade_from_text,
    parse_grade_roster_dataframe,
    parse_date_of_birth_cell,
    resolve_students_in_grade_by_names,
    split_parent_guardian_names,
    split_parent_phone_numbers,
)


def _fresh_db():
    tmp = tempfile.TemporaryDirectory()
    os.chdir(tmp.name)
    conn = init_db()
    conn.execute(
        """INSERT INTO students (student_code, name, grade, balance, status)
           VALUES ('0001', 'Alice Wonder', 'Grade 1', 0, 'Active'),
                  ('0002', 'Bob Builder', 'Grade 2', 0, 'Active')"""
    )
    conn.commit()
    return conn, tmp


class GradeRosterParseTests(unittest.TestCase):
    def test_split_parents_and_phones(self):
        self.assertEqual(split_parent_guardian_names("Mary & John"), ("Mary", "John"))
        p1, p2 = split_parent_phone_numbers("254712345678, 254700000001")
        self.assertTrue(p1.startswith("254"))
        self.assertTrue(p2.startswith("254"))

    def test_parse_roster_dataframe(self):
        df = pd.DataFrame(
            {
                "grade": ["Grade 1", "Grade 1"],
                "student_name": ["Alice Wonder", "Bob Builder"],
                "parent_guardian_names": ["Mary Doe", "Peter Smith"],
                "parent_phone": ["254712345678", ""],
                "date_of_birth": ["2018-01-15", ""],
            }
        )
        rows = parse_grade_roster_dataframe(df)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["grade"], "Grade 1")
        self.assertEqual(rows[0]["date_of_birth"], "2018-01-15")

    def test_detected_columns_contact_alias(self):
        df = pd.DataFrame(
            {
                "Grade": ["Grade 1"],
                "Learner": ["Alice Wonder"],
                "Mother": ["Mary"],
                "Contact": ["0712345678"],
            }
        )
        det = grade_roster_detected_columns(df)
        self.assertEqual(det["phone"], "Contact")
        self.assertIsNotNone(det["student_name"])
        rows = parse_grade_roster_dataframe(df)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["parent_phone"].startswith("254"))

    def test_numbered_parent_phone_headers_like_exported_sheet(self):
        """Regression: headers Parent/guardian names (1)/(2) and Phone (1) from school exports."""
        df = pd.DataFrame(
            {
                "Student Name": ["Janice Wambui"],
                "Parent/guardian names (1)": ["Mary Wambui"],
                "Parent/guardian names (2)": ["John Wambui"],
                "Phone (1)": ["254797948720"],
                "Date of birth": ["2015-12-19"],
            }
        )
        det = grade_roster_detected_columns(df)
        self.assertEqual(det["student_name"], "Student Name")
        self.assertEqual(det["parent"], "Parent/guardian names (1)")
        self.assertEqual(det["parent2"], "Parent/guardian names (2)")
        self.assertEqual(det["phone"], "Phone (1)")
        self.assertEqual(det["dob"], "Date of birth")
        self.assertIsNone(det["grade"])

        rows = parse_grade_roster_dataframe(df, default_grade="Grade 5")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["grade"], "Grade 5")
        self.assertEqual(rows[0]["student_name"], "Janice Wambui")
        self.assertEqual(rows[0]["parent_name"], "Mary Wambui")
        self.assertEqual(rows[0]["parent2_name"], "John Wambui")
        self.assertEqual(rows[0]["parent_phone"], "254797948720")
        self.assertEqual(rows[0]["date_of_birth"], "2015-12-19")

    def test_default_grade_from_file_overrides_sheet_grade_column(self):
        df = pd.DataFrame(
            {
                "name": ["Alice Wonder"],
                "grade": ["Grade 2"],
                "parent_phone": ["0712345678"],
            }
        )
        rows = parse_grade_roster_dataframe(df, default_grade="Grade 5")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["grade"], "Grade 5")

    def test_infer_grade_compact_filename_style(self):
        self.assertEqual(infer_grade_from_text("grade5parents"), "Grade 5")

    def test_two_parent_name_columns(self):
        df = pd.DataFrame(
            {
                "Learner": ["Alice Wonder"],
                "Mother": ["Mary W"],
                "Father": ["John W"],
                "Contact": ["0711111111"],
            }
        )
        rows = parse_grade_roster_dataframe(df, default_grade="Grade 1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["parent_name"], "Mary W")
        self.assertEqual(rows[0]["parent2_name"], "John W")
        self.assertTrue(rows[0]["parent_phone"].startswith("254"))

    def test_dob_parse(self):
        self.assertEqual(parse_date_of_birth_cell("15/05/2018"), "2018-05-15")


class GradeRosterImportTests(unittest.TestCase):
    def test_import_updates_matched_student(self):
        conn, tmp = _fresh_db()
        try:
            rows = [
                {
                    "grade": "Grade 1",
                    "student_name": "Alice Wonder",
                    "parent_name": "Mary Doe",
                    "parent2_name": "",
                    "parent_phone": "254712345678",
                    "parent2_phone": "",
                    "date_of_birth": "2018-06-01",
                    "sheet_row": 2,
                }
            ]
            rep = import_grade_roster_updates(conn, rows, dry_run=False)
            self.assertEqual(rep["updated"], 1)
            rec = get_student_record(conn, 1)
            self.assertEqual(rec["parent_name"], "Mary Doe")
            self.assertEqual(rec["parent_phone"], "254712345678")
            self.assertEqual(parse_date_of_birth_cell(rec["date_of_birth"]), "2018-06-01")
        finally:
            tmp.cleanup()

    def test_resolve_in_grade(self):
        conn, tmp = _fresh_db()
        try:
            res = resolve_students_in_grade_by_names(conn, "Grade 1", ["Alice Wonder", "Nobody"])
            self.assertEqual(len(res["matched"]), 1)
            self.assertEqual(res["unmatched"], ["Nobody"])
            self.assertEqual(res["ambiguous"], [])
        finally:
            tmp.cleanup()

    def test_import_dry_run_unmatched_does_not_crash(self):
        """Regression: unresolved names must not be merged into ambiguous as strings (breaks ** unpack)."""
        conn, tmp = _fresh_db()
        try:
            rows = [
                {
                    "grade": "Grade 1",
                    "student_name": "Alice Wonder",
                    "parent_name": "X",
                    "parent2_name": "",
                    "parent_phone": "",
                    "parent2_phone": "",
                    "date_of_birth": None,
                    "sheet_row": 2,
                },
                {
                    "grade": "Grade 1",
                    "student_name": "Nobody Here",
                    "parent_name": "Y",
                    "parent2_name": "",
                    "parent_phone": "",
                    "parent2_phone": "",
                    "date_of_birth": None,
                    "sheet_row": 3,
                },
            ]
            rep = import_grade_roster_updates(conn, rows, dry_run=True, resync_fees=False)
            self.assertIn("unresolved", rep)
            self.assertTrue(
                any(
                    u.get("student_name") == "Nobody Here"
                    for u in rep["unresolved"].get("unmatched", [])
                )
            )
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
