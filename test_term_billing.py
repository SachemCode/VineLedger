"""Term billing ledger: rollover balance, admission once, promotion, automation cancel."""
import os
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import init_db
from school_calendar import (
    bill_student_for_term,
    cancel_term_automation,
    clear_carry_on_balances_for_launch,
    ensure_next_academic_year,
    get_carry_on_opening_map,
    get_current_term,
    get_next_term,
    get_terms_for_year,
    process_term_closing,
    process_term_opening,
    promote_students_bulk,
    recompute_student_balance_from_ledger,
    run_calendar_automation_if_due,
)
from utils import (
    calculate_student_fees,
    graduated_students_archive_path,
    persist_student_edit,
    sync_student_fees_from_db,
)


def _fresh_db():
    tmp = tempfile.TemporaryDirectory()
    os.chdir(tmp.name)
    conn = init_db()
    return conn, tmp


def _insert_student(conn, grade="Grade 1", code="T001", name="Test Learner"):
    conn.execute(
        """INSERT INTO students (student_code, name, parent_name, parent_phone, grade,
           balance, total_paid, has_transport, has_meal, include_admission_fees, include_interview_fee, status)
           VALUES (?, ?, 'Parent', '254700000001', ?, 0, 0, 0, 0, 1, 1, 'Active')""",
        (code, name, grade),
    )
    conn.commit()
    return int(conn.execute("SELECT id FROM students WHERE student_code=?", (code,)).fetchone()[0])


def _term_id(conn, year_id, term_number):
    row = conn.execute(
        "SELECT id FROM school_terms WHERE year_id=? AND term_number=?",
        (int(year_id), int(term_number)),
    ).fetchone()
    return int(row[0])


def test_term2_balance_after_term1_paid():
    conn, tmp = _fresh_db()
    try:
        sid = _insert_student(conn)
        year_id = conn.execute("SELECT id FROM academic_years LIMIT 1").fetchone()[0]
        t1 = _term_id(conn, year_id, 1)
        t2 = _term_id(conn, year_id, 2)

        fee1 = calculate_student_fees(
            conn, "Grade 1", None, None, False, include_admission=True, include_interview=True
        )
        bill_student_for_term(conn, sid, t1, do_commit=True)
        amt1 = float(fee1["grand_total"])

        conn.execute("UPDATE students SET total_paid=?, balance=0 WHERE id=?", (amt1, sid))
        conn.commit()

        process_term_closing(conn, t1, do_commit=True)
        process_term_opening(conn, t2, do_commit=True)

        fee2 = calculate_student_fees(
            conn, "Grade 1", None, None, False, include_admission=False, include_interview=False
        )
        expected = float(fee2["grand_total"])
        row = conn.execute("SELECT balance FROM students WHERE id=?", (sid,)).fetchone()
        assert abs(float(row[0]) - expected) < 0.02, f"balance {row[0]} expected ~{expected}"
    finally:
        tmp.cleanup()
        os.chdir(ROOT)


def test_admission_billed_once_across_terms():
    conn, tmp = _fresh_db()
    try:
        sid = _insert_student(conn)
        year_id = conn.execute("SELECT id FROM academic_years LIMIT 1").fetchone()[0]
        t1 = _term_id(conn, year_id, 1)
        t2 = _term_id(conn, year_id, 2)

        bill_student_for_term(conn, sid, t1, do_commit=True)
        r1 = conn.execute(
            "SELECT admission_included, interview_fee_included FROM student_term_billing WHERE student_id=? AND term_id=?",
            (sid, t1),
        ).fetchone()
        assert r1[0] == 1 and r1[1] == 1

        bill_student_for_term(conn, sid, t2, do_commit=True)
        r2 = conn.execute(
            "SELECT admission_included, interview_fee_included FROM student_term_billing WHERE student_id=? AND term_id=?",
            (sid, t2),
        ).fetchone()
        assert r2[0] == 0 and r2[1] == 0
    finally:
        tmp.cleanup()
        os.chdir(ROOT)


def test_grade9_graduates_before_grade8_promoted():
    conn, tmp = _fresh_db()
    try:
        sid_g9 = _insert_student(conn, grade="Grade 9", code="G9A", name="Leaver Nine")
        sid_g8 = _insert_student(conn, grade="Grade 8", code="G8A", name="Promoted Eight")
        year_id = conn.execute("SELECT id FROM academic_years LIMIT 1").fetchone()[0]
        t3 = _term_id(conn, year_id, 3)
        conn.execute("UPDATE school_terms SET is_current=0")
        conn.execute("UPDATE school_terms SET is_current=1 WHERE id=?", (t3,))
        conn.commit()

        process_term_closing(conn, t3, do_commit=True)

        g9_status, g9_grade = conn.execute(
            "SELECT status, grade FROM students WHERE id=?", (sid_g9,)
        ).fetchone()
        assert g9_status == "Graduated"
        assert g9_grade == "Grade 9"
        assert conn.execute(
            "SELECT exited_at FROM students WHERE id=?", (sid_g9,)
        ).fetchone()[0] is not None

        g8_status, g8_grade = conn.execute(
            "SELECT status, grade FROM students WHERE id=?", (sid_g8,)
        ).fetchone()
        assert g8_status == "Active"
        assert g8_grade == "Grade 9"

        active_g9 = conn.execute(
            """SELECT COUNT(*) FROM students
               WHERE grade='Grade 9' AND COALESCE(status,'Active')='Active'"""
        ).fetchone()[0]
        assert active_g9 == 1

        archive_path = graduated_students_archive_path()
        assert archive_path.is_file()
        assert "G9A" in archive_path.read_text(encoding="utf-8")

        try:
            persist_student_edit(
                conn,
                sid_g9,
                {
                    "name": "Leaver Nine",
                    "grade": "Grade 9",
                    "parent_name": "Parent",
                    "parent_phone": "254700000001",
                    "has_transport": False,
                    "selected_transport_id": None,
                    "has_meal": False,
                    "include_admission_fees": 0,
                    "include_interview_fee": 0,
                    "co_curricular_ids": [],
                    "balance": 0,
                    "transport_choice": "__none__",
                },
            )
        except ValueError as e:
            assert "Cannot edit" in str(e)
        else:
            raise AssertionError("Expected ValueError editing Graduated student")
    finally:
        tmp.cleanup()
        os.chdir(ROOT)


def test_promotion_after_term3_and_next_year_created():
    conn, tmp = _fresh_db()
    try:
        sid = _insert_student(conn, grade="Grade 1")
        year_id = conn.execute("SELECT id FROM academic_years LIMIT 1").fetchone()[0]
        t3 = _term_id(conn, year_id, 3)
        conn.execute("UPDATE school_terms SET is_current=0")
        conn.execute("UPDATE school_terms SET is_current=1 WHERE id=?", (t3,))
        conn.commit()

        process_term_closing(conn, t3, do_commit=True)
        grade = conn.execute("SELECT grade FROM students WHERE id=?", (sid,)).fetchone()[0]
        assert grade == "Grade 2"

        year_label = conn.execute("SELECT label FROM academic_years WHERE id=?", (year_id,)).fetchone()[0]
        next_label = str(int(year_label) + 1)
        ny = conn.execute("SELECT id FROM academic_years WHERE label=?", (next_label,)).fetchone()
        assert ny is not None
        assert get_next_term(conn, t3) is not None
    finally:
        tmp.cleanup()
        os.chdir(ROOT)


def test_cancel_automation_skips_close():
    conn, tmp = _fresh_db()
    try:
        _insert_student(conn)
        cur = get_current_term(conn)
        assert cur
        conn.execute(
            "UPDATE school_terms SET closing_date=?, closing_processed_at=NULL WHERE id=?",
            (date.today().isoformat(), int(cur["id"])),
        )
        conn.commit()
        cancel_term_automation(conn, int(cur["id"]), cancelled=True)
        msgs = run_calendar_automation_if_due(conn, today=date.today())
        assert not any("Closed" in m for m in msgs)
        row = conn.execute(
            "SELECT closing_processed_at FROM school_terms WHERE id=?", (int(cur["id"]),)
        ).fetchone()
        assert row[0] is None
    finally:
        tmp.cleanup()
        os.chdir(ROOT)


def test_ledger_balance_equals_sum_billed_minus_paid():
    conn, tmp = _fresh_db()
    try:
        sid = _insert_student(conn)
        sync_student_fees_from_db(conn, sid, do_commit=True)
        conn.execute("UPDATE students SET total_paid=500 WHERE id=?", (sid,))
        conn.commit()
        bal = recompute_student_balance_from_ledger(conn, sid, do_commit=True)
        total_billed = conn.execute(
            "SELECT COALESCE(SUM(amount_billed), 0) FROM student_term_billing WHERE student_id=?",
            (sid,),
        ).fetchone()[0]
        assert abs(bal - max(float(total_billed) - 500, 0)) < 0.02
    finally:
        tmp.cleanup()
        os.chdir(ROOT)


def test_first_term_billing_does_not_set_carry_on_opening():
    conn, tmp = _fresh_db()
    try:
        sid = _insert_student(conn)
        conn.execute("UPDATE students SET balance=5000 WHERE id=?", (sid,))
        conn.commit()
        year_id = conn.execute("SELECT id FROM academic_years LIMIT 1").fetchone()[0]
        t1 = _term_id(conn, year_id, 1)
        bill_student_for_term(conn, sid, t1, do_commit=True)
        ob = conn.execute(
            "SELECT opening_balance FROM student_term_billing WHERE student_id=? AND term_id=?",
            (sid, t1),
        ).fetchone()[0]
        assert float(ob or 0) < 0.01
        co = get_carry_on_opening_map(conn, [sid]).get(sid, 0)
        assert float(co or 0) < 0.01
    finally:
        tmp.cleanup()
        os.chdir(ROOT)


def test_clear_carry_on_for_launch():
    conn, tmp = _fresh_db()
    try:
        sid = _insert_student(conn)
        conn.execute("UPDATE students SET balance=3200 WHERE id=?", (sid,))
        conn.commit()
        year_id = conn.execute("SELECT id FROM academic_years LIMIT 1").fetchone()[0]
        t1 = _term_id(conn, year_id, 1)
        bill_student_for_term(conn, sid, t1, do_commit=True)
        conn.execute("UPDATE students SET balance=3200 WHERE id=?", (sid,))
        conn.execute(
            "UPDATE student_term_billing SET opening_balance=3200 WHERE student_id=? AND term_id=?",
            (sid, t1),
        )
        conn.commit()
        clear_carry_on_balances_for_launch(conn, do_commit=True)
        ob = conn.execute(
            "SELECT opening_balance FROM student_term_billing WHERE student_id=? AND term_id=?",
            (sid, t1),
        ).fetchone()[0]
        bal = conn.execute("SELECT balance FROM students WHERE id=?", (sid,)).fetchone()[0]
        assert float(ob or 0) < 0.01
        assert abs(float(bal) - 3200.0) < 0.02
        assert float(get_carry_on_opening_map(conn, [sid]).get(sid, 0) or 0) < 0.01
    finally:
        tmp.cleanup()
        os.chdir(ROOT)


def test_interview_fee_amount_by_grade():
    conn, tmp = _fresh_db()
    try:
        f6 = calculate_student_fees(
            conn, "Grade 6", None, None, False, include_admission=False, include_interview=True
        )
        f7 = calculate_student_fees(
            conn, "Grade 7", None, None, False, include_admission=False, include_interview=True
        )
        assert abs(float(f6.get("interview_total") or 0) - 500) < 0.01
        assert abs(float(f7.get("interview_total") or 0) - 700) < 0.01
    finally:
        tmp.cleanup()
        os.chdir(ROOT)
