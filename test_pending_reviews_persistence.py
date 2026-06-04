"""Pending reviews (Save for later) survive database round-trips."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import init_db
from utils import (
    DRAFT_TYPE_BALANCE_BULK,
    DRAFT_TYPE_CLUB_BULK,
    DRAFT_TYPE_GRADE_CONTACT_BULK,
    DRAFT_TYPE_MEAL_BULK,
    DRAFT_TYPE_TRANSPORT_BULK,
    delete_pending_bulk_draft,
    delete_pending_student_review,
    enroll_students_in_club,
    fetch_all_pending_reviews,
    get_co_curricular_name_to_id,
    import_balance_roster_updates,
    import_grade_roster_updates,
    insert_pending_expense_review,
    insert_pending_manual_payment_review,
    parse_co_curricular_ids,
    upsert_pending_bulk_draft,
    upsert_pending_student_review,
)


def _reload_conn():
    """New connection to the same on-disk DB (simulates browser refresh)."""
    return init_db()


class PendingReviewsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.conn = init_db()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_student_pending_review_persists(self):
        payload = {
            "name": "Ada Lovelace",
            "grade": "Grade 1",
            "parent_name": "Parent",
            "co_curricular_ids": [1, 2],
        }
        upsert_pending_student_review(self.conn, 42, payload)
        loaded = fetch_all_pending_reviews(self.conn)
        self.assertEqual(loaded["students"][42]["name"], "Ada Lovelace")
        self.assertEqual(loaded["students"][42]["co_curricular_ids"], [1, 2])

        delete_pending_student_review(self.conn, 42)
        self.assertNotIn(42, fetch_all_pending_reviews(self.conn)["students"])

    def test_payment_and_expense_drafts_persist(self):
        pay = {
            "id": "pay-1",
            "student_id": 1,
            "amount": 500.0,
            "payment_date": "2026-05-15",
            "payment_method": "Cash",
            "purpose": "Fees",
            "description": "",
        }
        exp = {
            "id": "exp-1",
            "category": "Supplies",
            "custom_label": "",
            "amount": 1200.0,
            "expense_date": "2026-05-15",
            "payment_method": "M-Pesa",
            "vendor": "Shop",
            "receipt_number": "",
            "description": "Books",
        }
        insert_pending_manual_payment_review(self.conn, pay)
        insert_pending_expense_review(self.conn, exp)
        loaded = fetch_all_pending_reviews(self.conn)
        self.assertEqual(len(loaded["payments"]), 1)
        self.assertEqual(loaded["payments"][0]["id"], "pay-1")
        self.assertEqual(len(loaded["expenses"]), 1)
        self.assertEqual(loaded["expenses"][0]["amount"], 1200.0)

    def _assert_bulk_draft_round_trip(
        self,
        draft_type,
        list_key,
        draft_id,
        initial_draft,
        updated_draft,
    ):
        upsert_pending_bulk_draft(self.conn, draft_type, initial_draft)
        self.conn.close()

        conn2 = _reload_conn()
        try:
            loaded = fetch_all_pending_reviews(conn2)
            self.assertEqual(len(loaded[list_key]), 1)
            got = loaded[list_key][0]
            self.assertEqual(got["id"], draft_id)
            self.assertEqual(got["kind"], initial_draft["kind"])
            self.assertEqual(got["label"], initial_draft["label"])
            self.assertEqual(got["payload"], initial_draft["payload"])

            upsert_pending_bulk_draft(conn2, draft_type, updated_draft)
            loaded2 = fetch_all_pending_reviews(conn2)
            self.assertEqual(len(loaded2[list_key]), 1)
            self.assertEqual(loaded2[list_key][0]["label"], updated_draft["label"])
            self.assertEqual(
                loaded2[list_key][0]["payload"],
                updated_draft["payload"],
            )

            delete_pending_bulk_draft(conn2, draft_type, draft_id)
            self.assertEqual(fetch_all_pending_reviews(conn2)[list_key], [])
        finally:
            conn2.close()

    def test_club_bulk_draft_persists(self):
        self.conn.execute(
            """INSERT INTO fee_structure (fee_name, fee_amount, fee_category)
               VALUES ('Drama', 1000, 'co_curricular')"""
        )
        self.conn.execute(
            """INSERT INTO students (student_code, name, grade, balance, status)
               VALUES ('0001', 'Alice Wonder', 'Grade 1', 0, 'Active')"""
        )
        self.conn.commit()
        club_id = list(get_co_curricular_name_to_id(self.conn).values())[0]

        draft_id = "club-draft-1"
        initial = {
            "id": draft_id,
            "kind": "club_assign",
            "label": "Add Alice to Drama",
            "payload": {
                "club_id": club_id,
                "club_name": "Drama",
                "mode": "add",
                "student_ids": [1],
            },
        }
        updated = {
            "id": draft_id,
            "kind": "club_import",
            "label": "Import roster",
            "payload": {
                "rows": [
                    {
                        "club_name": "Drama",
                        "student_name": "Alice Wonder",
                        "sheet_row": 2,
                    }
                ],
                "mode": "add",
            },
        }
        self._assert_bulk_draft_round_trip(
            DRAFT_TYPE_CLUB_BULK,
            "club_drafts",
            draft_id,
            initial,
            updated,
        )

    def test_grade_contact_bulk_draft_persists(self):
        self.conn.execute(
            """INSERT INTO students (student_code, name, grade, balance, status,
               parent_name, parent_phone)
               VALUES ('0001', 'Alice Wonder', 'Grade 1', 0, 'Active', 'Old Parent', '')"""
        )
        self.conn.commit()

        draft_id = "grade-draft-1"
        initial = {
            "id": draft_id,
            "kind": "grade_bulk",
            "label": "Grade 1 contacts",
            "payload": {
                "grade": "Grade 1",
                "rows": [
                    {
                        "id": 1,
                        "parent_name": "New Parent",
                        "parent_phone": "254712345678",
                        "parent2_name": "",
                        "parent2_phone": "",
                        "date_of_birth": "2018-01-15",
                    }
                ],
            },
        }
        updated = {
            "id": draft_id,
            "kind": "grade_import",
            "label": "Grade 1 import",
            "payload": {
                "rows": [
                    {
                        "grade": "Grade 1",
                        "student_name": "Alice Wonder",
                        "parent_guardian_names": "Mary & John",
                        "parent_phone": "254712345678",
                        "date_of_birth": "2018-01-15",
                        "sheet_row": 2,
                    }
                ],
            },
        }
        self._assert_bulk_draft_round_trip(
            DRAFT_TYPE_GRADE_CONTACT_BULK,
            "grade_contact_drafts",
            draft_id,
            initial,
            updated,
        )

    def test_balance_bulk_draft_persists(self):
        self.conn.execute(
            """INSERT INTO students (student_code, name, grade, balance, status)
               VALUES ('0001', 'Alice Wonder', 'Grade 1', 5000, 'Active')"""
        )
        self.conn.commit()

        draft_id = "bal-draft-1"
        rows = [
            {
                "grade": "Grade 1",
                "student_name": "Alice Wonder",
                "balance": 9000.0,
                "sheet_row": 2,
            }
        ]
        initial = {
            "id": draft_id,
            "kind": "balance_import",
            "label": "Term balances",
            "payload": {"rows": rows},
        }
        updated = {
            "id": draft_id,
            "kind": "balance_bulk",
            "label": "Grade 1 bulk edit",
            "payload": {
                "grade": "Grade 1",
                "rows": [{"id": 1, "balance": 7500.0}],
            },
        }
        self._assert_bulk_draft_round_trip(
            DRAFT_TYPE_BALANCE_BULK,
            "balance_drafts",
            draft_id,
            initial,
            updated,
        )

    def test_meal_bulk_draft_persists(self):
        draft_id = "meal-draft-1"
        initial = {
            "id": draft_id,
            "kind": "meal_bulk",
            "label": "Grade 1 — meals",
            "payload": {"grade": "Grade 1", "rows": [{"id": 1, "meals": "Yes"}]},
        }
        updated = {
            "id": draft_id,
            "kind": "meal_bulk",
            "label": "Grade 1 — meals v2",
            "payload": {"grade": "Grade 1", "rows": [{"id": 1, "meals": "No"}, {"id": 2, "meals": "Yes"}]},
        }
        self._assert_bulk_draft_round_trip(
            DRAFT_TYPE_MEAL_BULK,
            "meal_drafts",
            draft_id,
            initial,
            updated,
        )

    def test_transport_bulk_draft_persists(self):
        draft_id = "tr-draft-1"
        initial = {
            "id": draft_id,
            "kind": "transport_bulk",
            "label": "Grade 2 — transport",
            "payload": {"grade": "Grade 2", "rows": [{"id": 1, "transport_choice": "__none__"}]},
        }
        updated = {
            "id": draft_id,
            "kind": "transport_bulk",
            "label": "Grade 2 — transport v2",
            "payload": {
                "grade": "Grade 2",
                "rows": [{"id": 1, "transport_choice": "__none__"}, {"id": 2, "transport_choice": "99"}],
            },
        }
        self._assert_bulk_draft_round_trip(
            DRAFT_TYPE_TRANSPORT_BULK,
            "transport_drafts",
            draft_id,
            initial,
            updated,
        )


class PendingBulkDraftApplyTests(unittest.TestCase):
    """Apply path mirrors app.apply_pending_* (utils only; no Streamlit import)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.conn = init_db()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_apply_balance_import_draft_then_remove(self):
        self.conn.execute(
            """INSERT INTO students (student_code, name, grade, balance, status)
               VALUES ('0001', 'Alice Wonder', 'Grade 1', 5000, 'Active')"""
        )
        self.conn.commit()

        rows = [
            {
                "grade": "Grade 1",
                "student_name": "Alice Wonder",
                "balance": 12000.0,
                "sheet_row": 2,
            }
        ]
        draft = {
            "id": "bal-apply-1",
            "kind": "balance_import",
            "label": "Apply test",
            "payload": {"rows": rows},
        }
        upsert_pending_bulk_draft(self.conn, DRAFT_TYPE_BALANCE_BULK, draft)
        self.assertEqual(len(fetch_all_pending_reviews(self.conn)["balance_drafts"]), 1)

        rep = import_balance_roster_updates(self.conn, draft["payload"]["rows"], dry_run=False)
        self.assertEqual(rep["updated"], 1)
        rec = self.conn.execute("SELECT balance FROM students WHERE id=1").fetchone()
        self.assertEqual(float(rec[0]), 12000.0)

        delete_pending_bulk_draft(self.conn, DRAFT_TYPE_BALANCE_BULK, draft["id"])
        self.assertEqual(fetch_all_pending_reviews(self.conn)["balance_drafts"], [])

    def test_apply_club_assign_draft_then_remove(self):
        self.conn.execute(
            """INSERT INTO fee_structure (fee_name, fee_amount, fee_category)
               VALUES ('Drama', 1000, 'co_curricular')"""
        )
        self.conn.execute(
            """INSERT INTO students (student_code, name, grade, balance, status)
               VALUES ('0001', 'Alice Wonder', 'Grade 1', 0, 'Active')"""
        )
        self.conn.commit()
        club_id = list(get_co_curricular_name_to_id(self.conn).values())[0]

        draft = {
            "id": "club-apply-1",
            "kind": "club_assign",
            "label": "Enroll Alice",
            "payload": {
                "club_id": club_id,
                "club_name": "Drama",
                "mode": "add",
                "student_ids": [1],
            },
        }
        upsert_pending_bulk_draft(self.conn, DRAFT_TYPE_CLUB_BULK, draft)

        p = draft["payload"]
        enroll_students_in_club(
            self.conn,
            int(p["club_id"]),
            [int(x) for x in p["student_ids"]],
            mode=p.get("mode") or "add",
            resync_fees=True,
            do_commit=True,
        )
        rec = self.conn.execute(
            "SELECT co_curricular_activities FROM students WHERE id=1"
        ).fetchone()
        ids = parse_co_curricular_ids(rec[0], conn=self.conn, student_id=1)
        self.assertIn(club_id, ids)

        delete_pending_bulk_draft(self.conn, DRAFT_TYPE_CLUB_BULK, draft["id"])
        self.assertEqual(fetch_all_pending_reviews(self.conn)["club_drafts"], [])


if __name__ == "__main__":
    unittest.main()
