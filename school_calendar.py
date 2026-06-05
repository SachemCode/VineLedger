"""
Academic calendar, per-term billing ledger, and automated term rollover.

Balance model (when a current school term exists):
    student.balance = SUM(student_term_billing.amount_billed) - student.total_paid

Each term row stores:
    amount_billed - fees for that term (from fee_structure + student options)
    opening_balance - unpaid amount carried in when the term opened (carry-on)

Automation (triggered on Dashboard load via run_calendar_automation_if_due):
    - On closing_date: close current term (is_current=0, status=closed)
    - After Term 3 close: graduate Active Grade 9 leavers, then bulk-promote remaining active students; create next academic year
    - On opening_date: set term current and bill all active students for the new term

Admission fee (KSH 1,000) and interview fee (grade-based) are each included on the first term
billing row that applies them (admission_included / interview_fee_included flags).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from utils import (
    REAL_GRADES,
    BALANCE_STATUS_CLEARED,
    BALANCE_STATUS_SET,
    archive_graduated_student_record,
    calculate_student_fees,
    get_student_record,
    parse_co_curricular_ids,
    student_row_bool,
)


def _today() -> date:
    return date.today()


def _parse_date(val):
    if val is None:
        return None
    if isinstance(val, date):
        return val
    s = str(val).strip()[:10]
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _date_to_storage(d):
    if d is None:
        return None
    if isinstance(d, datetime):
        d = d.date()
    return d.isoformat()


def get_calendar_settings(conn):
    row = conn.execute(
        "SELECT warn_days_before_close FROM school_calendar_settings WHERE id=1"
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT OR IGNORE INTO school_calendar_settings (id, warn_days_before_close) VALUES (1, 14)"
        )
        conn.commit()
        return {"warn_days_before_close": 14}
    return {"warn_days_before_close": int(row[0] or 14)}


def save_calendar_settings(conn, warn_days_before_close):
    conn.execute(
        """INSERT INTO school_calendar_settings (id, warn_days_before_close)
           VALUES (1, ?)
           ON CONFLICT(id) DO UPDATE SET warn_days_before_close=excluded.warn_days_before_close""",
        (int(warn_days_before_close),),
    )
    conn.commit()


def list_academic_years(conn):
    return conn.execute(
        "SELECT id, label FROM academic_years ORDER BY label DESC"
    ).fetchall()


def get_terms_for_year(conn, year_id):
    return conn.execute(
        """SELECT id, year_id, term_number, label, opening_date, closing_date,
                  is_current, status, automation_cancelled, closing_processed_at, opening_processed_at
           FROM school_terms WHERE year_id=? ORDER BY term_number""",
        (int(year_id),),
    ).fetchall()


def get_term_by_id(conn, term_id):
    row = conn.execute(
        """SELECT id, year_id, term_number, label, opening_date, closing_date,
                  is_current, status, automation_cancelled, closing_processed_at, opening_processed_at
           FROM school_terms WHERE id=?""",
        (int(term_id),),
    ).fetchone()
    if not row:
        return None
    keys = (
        "id", "year_id", "term_number", "label", "opening_date", "closing_date",
        "is_current", "status", "automation_cancelled", "closing_processed_at", "opening_processed_at",
    )
    return dict(zip(keys, row))


def get_current_term(conn):
    row = conn.execute(
        """SELECT id, year_id, term_number, label, opening_date, closing_date,
                  is_current, status, automation_cancelled, closing_processed_at, opening_processed_at
           FROM school_terms WHERE is_current=1 ORDER BY id LIMIT 1"""
    ).fetchone()
    if not row:
        return None
    keys = (
        "id", "year_id", "term_number", "label", "opening_date", "closing_date",
        "is_current", "status", "automation_cancelled", "closing_processed_at", "opening_processed_at",
    )
    return dict(zip(keys, row))


def ensure_next_academic_year(conn, year_id, *, do_commit=False):
    """Create the following academic year and three terms if missing (after Term 3 close)."""
    year = conn.execute("SELECT label FROM academic_years WHERE id=?", (int(year_id),)).fetchone()
    if not year:
        return None
    try:
        next_label = str(int(year[0]) + 1)
    except ValueError:
        next_label = f"{year[0]} (next)"
    existing = conn.execute("SELECT id FROM academic_years WHERE label=?", (next_label,)).fetchone()
    if existing:
        ny_id = int(existing[0])
    else:
        cur = conn.execute("INSERT INTO academic_years (label) VALUES (?)", (next_label,))
        ny_id = int(cur.lastrowid)
        for tn in (1, 2, 3):
            conn.execute(
                """INSERT INTO school_terms (year_id, term_number, label, is_current, status)
                   VALUES (?, ?, ?, 0, 'upcoming')""",
                (ny_id, tn, f"{next_label} Term {tn}"),
            )
    for tn in (1, 2, 3):
        if not conn.execute(
            "SELECT 1 FROM school_terms WHERE year_id=? AND term_number=?",
            (ny_id, tn),
        ).fetchone():
            conn.execute(
                """INSERT INTO school_terms (year_id, term_number, label, is_current, status)
                   VALUES (?, ?, ?, 0, 'upcoming')""",
                (ny_id, tn, f"{next_label} Term {tn}"),
            )
    if do_commit:
        conn.commit()
    return ny_id


def get_next_term(conn, term_id):
    term = get_term_by_id(conn, term_id)
    if not term:
        return None
    tn = int(term["term_number"])
    yid = int(term["year_id"])
    if tn < 3:
        row = conn.execute(
            """SELECT id FROM school_terms WHERE year_id=? AND term_number=?""",
            (yid, tn + 1),
        ).fetchone()
        return get_term_by_id(conn, row[0]) if row else None
    year = conn.execute("SELECT label FROM academic_years WHERE id=?", (yid,)).fetchone()
    if not year:
        return None
    try:
        next_label = str(int(year[0]) + 1)
    except ValueError:
        next_label = f"{year[0]} (next)"
    ny = conn.execute("SELECT id FROM academic_years WHERE label=?", (next_label,)).fetchone()
    if not ny:
        return None
    row = conn.execute(
        "SELECT id FROM school_terms WHERE year_id=? AND term_number=1", (ny[0],)
    ).fetchone()
    return get_term_by_id(conn, row[0]) if row else None


def validate_term_dates(term_rows):
    """term_rows: list of dicts with term_number, opening_date, closing_date (date objects)."""
    errors = []
    sorted_rows = sorted(term_rows, key=lambda r: int(r["term_number"]))
    prev_close = None
    for r in sorted_rows:
        tn = int(r["term_number"])
        op = r.get("opening_date")
        cl = r.get("closing_date")
        if op and cl and op > cl:
            errors.append(f"Term {tn}: opening date must be on or before closing date.")
        if prev_close and op and op < prev_close:
            errors.append(f"Term {tn}: opening date must be on or after previous term closes.")
        if cl:
            prev_close = cl
    return errors


def save_school_calendar_year(conn, year_id, term_dates_by_number, warn_days=None):
    """
    term_dates_by_number: {1: (open, close), 2: (...), 3: (...)} with date or None.
    """
    for tn, (op, cl) in term_dates_by_number.items():
        conn.execute(
            """UPDATE school_terms SET opening_date=?, closing_date=?, updated_at=CURRENT_TIMESTAMP
               WHERE year_id=? AND term_number=?""",
            (_date_to_storage(op), _date_to_storage(cl), int(year_id), int(tn)),
        )
    if warn_days is not None:
        save_calendar_settings(conn, warn_days)
    else:
        conn.commit()


def student_has_admission_billed(conn, student_id):
    row = conn.execute(
        """SELECT 1 FROM student_term_billing
           WHERE student_id=? AND admission_included=1 LIMIT 1""",
        (int(student_id),),
    ).fetchone()
    return row is not None


def student_has_interview_billed(conn, student_id):
    row = conn.execute(
        """SELECT 1 FROM student_term_billing
           WHERE student_id=? AND COALESCE(interview_fee_included, 0)=1 LIMIT 1""",
        (int(student_id),),
    ).fetchone()
    return row is not None


def student_has_prior_term_billing(conn, student_id, *, exclude_term_id=None):
    """True if the learner already has a billing row in another term (not first term in system)."""
    sid = int(student_id)
    if exclude_term_id is not None:
        row = conn.execute(
            "SELECT 1 FROM student_term_billing WHERE student_id=? AND term_id != ? LIMIT 1",
            (sid, int(exclude_term_id)),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM student_term_billing WHERE student_id=? LIMIT 1",
            (sid,),
        ).fetchone()
    return row is not None


def align_student_term_ledger_to_balance(conn, student_id, *, balance=None, do_commit=False):
    """
    Treat students.balance as authoritative: clear carry-on (opening_balance) and set the
    current term amount_billed so SUM(amount_billed) - total_paid equals that balance.
    """
    sid = int(student_id)
    row = conn.execute(
        "SELECT COALESCE(balance, 0), COALESCE(total_paid, 0), COALESCE(is_sponsored, 0), grade "
        "FROM students WHERE id=?",
        (sid,),
    ).fetchone()
    if not row:
        return False
    if balance is None:
        balance = float(row[0] or 0)
    else:
        balance = float(balance)
    total_paid = float(row[1] or 0)
    is_sponsored = int(row[2] or 0)
    grade = row[3]
    if is_sponsored:
        if do_commit:
            conn.commit()
        return True

    target_billed = balance + total_paid
    conn.execute(
        "UPDATE student_term_billing SET opening_balance=0 WHERE student_id=?",
        (sid,),
    )
    term = get_current_term(conn)
    if not term:
        if do_commit:
            conn.commit()
        return True

    tid = int(term["id"])
    other_billed = float(
        conn.execute(
            "SELECT COALESCE(SUM(amount_billed), 0) FROM student_term_billing "
            "WHERE student_id=? AND term_id != ?",
            (sid, tid),
        ).fetchone()[0]
        or 0
    )
    current_amt = max(target_billed - other_billed, 0.0)
    existing = conn.execute(
        "SELECT id FROM student_term_billing WHERE student_id=? AND term_id=?",
        (sid, tid),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE student_term_billing SET amount_billed=?, opening_balance=0,
               updated_at=CURRENT_TIMESTAMP WHERE student_id=? AND term_id=?""",
            (current_amt, sid, tid),
        )
    elif current_amt > 0.009 or target_billed > 0.009:
        conn.execute(
            """INSERT INTO student_term_billing
               (student_id, term_id, amount_billed, opening_balance, grade_at_billing)
               VALUES (?, ?, ?, 0, ?)""",
            (sid, tid, current_amt, grade),
        )
    if do_commit:
        conn.commit()
    return True


def clear_carry_on_balances_for_launch(conn, *, do_commit=True):
    """
    Clear all carry-on (opening_balance) and align term billing to each student's current
    outstanding balance. Intended for first-term go-live when spreadsheet balances are authoritative.
    """
    conn.execute("UPDATE student_term_billing SET opening_balance=0")
    rows = conn.execute(
        "SELECT id FROM students WHERE COALESCE(status, 'Active') = 'Active'"
    ).fetchall()
    aligned = 0
    for (sid,) in rows:
        if align_student_term_ledger_to_balance(conn, int(sid), do_commit=False):
            aligned += 1
    if do_commit:
        conn.commit()
    return {"students_aligned": aligned, "carry_on_cleared": True}


def reset_all_student_balances_not_set(conn, *, do_commit=True):
    """
    Clear all outstanding balances so staff can re-import from spreadsheets.
    Sets balance_set=0 (displayed as 'Not set'); clears carry-on and aligns ledgers to zero.
    """
    conn.execute("UPDATE student_term_billing SET opening_balance=0")
    conn.execute(
        "UPDATE students SET balance=NULL, balance_set=0, balance_status='not_set'"
    )
    rows = conn.execute("SELECT id FROM students").fetchall()
    for (sid,) in rows:
        align_student_term_ledger_to_balance(conn, int(sid), balance=0.0, do_commit=False)
    if do_commit:
        conn.commit()
    return {"students_reset": len(rows)}


def recompute_student_balance_from_ledger(conn, student_id, do_commit=False):
    """Set students.balance from the sum of all term billing rows minus total_paid."""
    total_billed = conn.execute(
        "SELECT COALESCE(SUM(amount_billed), 0) FROM student_term_billing WHERE student_id=?",
        (int(student_id),),
    ).fetchone()[0]
    stu = conn.execute(
        "SELECT COALESCE(total_paid, 0), COALESCE(is_sponsored, 0), COALESCE(balance, 0) FROM students WHERE id=?",
        (int(student_id),),
    ).fetchone()
    total_paid, is_sponsored, current_bal = stu[0], stu[1], stu[2]
    if int(is_sponsored or 0):
        new_bal = float(current_bal or 0)
        conn.execute(
            "UPDATE students SET balance=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_bal, int(student_id)),
        )
    else:
        new_bal = max(float(total_billed) - float(total_paid), 0.0)
        if new_bal <= 0.0001:
            new_bal = 0.0
            bstat = BALANCE_STATUS_CLEARED
        else:
            bstat = BALANCE_STATUS_SET
        conn.execute(
            """
            UPDATE students SET balance=?, balance_set=1, balance_status=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=?
            """,
            (new_bal, bstat, int(student_id)),
        )
    if do_commit:
        conn.commit()
    return new_bal


def _fee_payload_for_student(conn, student_row):
    import pandas as pd

    if hasattr(student_row, "iloc"):
        student = student_row.iloc[0] if len(student_row) > 1 else student_row.iloc[0]
    else:
        student = student_row
    sid = int(student["id"])
    grade = student["grade"]
    has_transport = bool(student.get("has_transport") if hasattr(student, "get") else student["has_transport"])
    tr_id = student.get("transport_route_id") if hasattr(student, "get") else student["transport_route_id"]
    if has_transport and tr_id is not None and not (isinstance(tr_id, float) and pd.isna(tr_id)):
        transport_route_id = int(tr_id)
    else:
        transport_route_id = None
    has_meal = bool(student.get("has_meal") if hasattr(student, "get") else student["has_meal"])
    cc_ids = parse_co_curricular_ids(
        student.get("co_curricular_activities") if hasattr(student, "get") else student["co_curricular_activities"],
        conn=conn,
        student_id=sid,
    )
    include_admission = student_row_bool(student, "include_admission_fees", False)
    include_interview = student_row_bool(student, "include_interview_fee", False)
    if student_has_admission_billed(conn, sid):
        include_admission = False
    if student_has_interview_billed(conn, sid):
        include_interview = False
    fee = calculate_student_fees(
        conn,
        grade,
        transport_route_id,
        cc_ids if cc_ids else None,
        has_meal,
        include_admission=include_admission,
        include_interview=include_interview,
    )
    return sid, grade, fee, include_admission, include_interview


def bill_student_for_term(conn, student_id, term_id, *, student_row=None, do_commit=False):
    """
    Create or update one student_term_billing row for a term.

    On first insert, opening_balance is set from the student's balance before billing
    (carry-on from prior terms). Admission and interview one-time fees are each skipped
    if already billed in any prior row.
    """
    import pandas as pd

    if student_row is None:
        student_row = pd.read_sql("SELECT * FROM students WHERE id=?", conn, params=(int(student_id),))
        if student_row.empty:
            return None
    sid, grade, fee, include_admission, include_interview = _fee_payload_for_student(conn, student_row)
    amount = float(fee.get("grand_total") or 0)
    existing = conn.execute(
        "SELECT id, opening_balance FROM student_term_billing WHERE student_id=? AND term_id=?",
        (sid, int(term_id)),
    ).fetchone()
    opening = float(existing[1]) if existing else 0.0
    if not existing:
        # First billing row for this learner in the system is not "carry-on" from a prior term.
        if student_has_prior_term_billing(conn, sid, exclude_term_id=int(term_id)):
            prev_bal = conn.execute(
                "SELECT COALESCE(balance, 0) FROM students WHERE id=?", (sid,)
            ).fetchone()[0]
            opening = float(prev_bal or 0)
        else:
            opening = 0.0
    breakdown = json.dumps(fee.get("fee_breakdown") or [])
    if existing:
        conn.execute(
            """UPDATE student_term_billing SET amount_billed=?, fee_breakdown_json=?,
               grade_at_billing=?, admission_included=?, interview_fee_included=?,
               updated_at=CURRENT_TIMESTAMP
               WHERE student_id=? AND term_id=?""",
            (
                amount,
                breakdown,
                grade,
                1 if include_admission else 0,
                1 if include_interview else 0,
                sid,
                int(term_id),
            ),
        )
    else:
        conn.execute(
            """INSERT INTO student_term_billing
               (student_id, term_id, amount_billed, fee_breakdown_json, opening_balance,
                grade_at_billing, admission_included, interview_fee_included)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sid,
                int(term_id),
                amount,
                breakdown,
                opening,
                grade,
                1 if include_admission else 0,
                1 if include_interview else 0,
            ),
        )
    bal = recompute_student_balance_from_ledger(conn, sid, do_commit=do_commit)
    return {"student_id": sid, "amount_billed": amount, "balance": bal, "fee_result": fee}


def upsert_current_term_billing(conn, student_id, *, do_commit=False):
    """Mid-term sync: update billing row for the current term only."""
    term = get_current_term(conn)
    if not term:
        return None
    return bill_student_for_term(conn, student_id, term["id"], do_commit=do_commit)


def get_student_term_ledger(conn, student_id):
    rows = conn.execute(
        """SELECT stb.amount_billed, stb.opening_balance, stb.grade_at_billing,
                  stb.admission_included, stb.interview_fee_included, st.label, st.term_number, ay.label AS year_label,
                  st.opening_date, st.closing_date, st.is_current
           FROM student_term_billing stb
           JOIN school_terms st ON st.id = stb.term_id
           JOIN academic_years ay ON ay.id = st.year_id
           WHERE stb.student_id=?
           ORDER BY ay.label, st.term_number""",
        (int(student_id),),
    ).fetchall()
    cols = [
        "amount_billed", "opening_balance", "grade_at_billing", "admission_included",
        "interview_fee_included",
        "term_label", "term_number", "year_label", "opening_date", "closing_date", "is_current",
    ]
    return [dict(zip(cols, r)) for r in rows]


def _prior_term_label(conn, year_id, term_number):
    tn = int(term_number)
    yid = int(year_id)
    if tn <= 1:
        row = conn.execute("SELECT label FROM academic_years WHERE id=?", (yid,)).fetchone()
        return f"Before {row[0]} Term 1" if row else "Before Term 1"
    row = conn.execute(
        "SELECT label FROM school_terms WHERE year_id=? AND term_number=?",
        (yid, tn - 1),
    ).fetchone()
    return row[0] if row else f"Term {tn - 1}"


def get_carry_on_opening_map(conn, student_ids):
    """Opening balance brought into the current term, keyed by student_id."""
    term = get_current_term(conn)
    if not term or not student_ids:
        return {}
    ids = [int(s) for s in student_ids]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT student_id, COALESCE(opening_balance, 0)
            FROM student_term_billing
            WHERE term_id=? AND student_id IN ({placeholders})""",
        [int(term["id"])] + ids,
    ).fetchall()
    out = {int(sid): 0.0 for sid in ids}
    for sid, ob in rows:
        amt = float(ob or 0)
        if amt > 0.01:
            out[int(sid)] = amt
    return out


def get_current_term_carry_on(conn, student_id):
    return get_carry_on_opening_map(conn, [int(student_id)]).get(int(student_id), 0.0)


def get_student_carry_on_breakdown(conn, student_id):
    """
    Each row: amount that was unpaid when a prior term closed and was brought into a later term.
    """
    rows = conn.execute(
        """SELECT stb.opening_balance, st.label, st.term_number, st.year_id, st.is_current
           FROM student_term_billing stb
           JOIN school_terms st ON st.id = stb.term_id
           JOIN academic_years ay ON ay.id = st.year_id
           WHERE stb.student_id=? AND COALESCE(stb.opening_balance, 0) > 0.01
           ORDER BY ay.label, st.term_number""",
        (int(student_id),),
    ).fetchall()
    items = []
    for ob, label, tn, yid, is_cur in rows:
        items.append({
            "from_term": _prior_term_label(conn, yid, tn),
            "into_term": label,
            "amount": float(ob),
            "is_current": bool(is_cur),
        })
    return items


def get_current_term_expected_total(conn, student_id):
    term = get_current_term(conn)
    if not term:
        return None
    row = conn.execute(
        "SELECT amount_billed FROM student_term_billing WHERE student_id=? AND term_id=?",
        (int(student_id), int(term["id"])),
    ).fetchone()
    return float(row[0]) if row else None


def graduate_grade9_leavers_bulk(conn, *, today=None, do_commit=True):
    """Mark Active Grade 9 students as Graduated before end-of-year promotion."""
    today = today or _today()
    exited = _date_to_storage(today)
    rows = conn.execute(
        """SELECT id FROM students
           WHERE COALESCE(status, 'Active') = 'Active'
           AND TRIM(grade) = 'Grade 9'"""
    ).fetchall()
    graduated = 0
    for (sid,) in rows:
        conn.execute(
            """UPDATE students SET status = 'Graduated', exited_at = ?,
               updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (exited, int(sid)),
        )
        archive_graduated_student_record(conn, int(sid), archived_at=datetime.now())
        graduated += 1
    if do_commit:
        conn.commit()
    return {"graduated": graduated}


def promote_students_bulk(conn, *, do_commit=True):
    """Advance each active student's grade along REAL_GRADES."""
    promoted = 0
    unchanged = 0
    rows = conn.execute(
        """SELECT id, grade FROM students
           WHERE COALESCE(status, 'Active') = 'Active'"""
    ).fetchall()
    for sid, grade in rows:
        g = (grade or "").strip()
        if g not in REAL_GRADES:
            unchanged += 1
            continue
        idx = REAL_GRADES.index(g)
        if idx >= len(REAL_GRADES) - 1:
            unchanged += 1
            continue
        new_grade = REAL_GRADES[idx + 1]
        conn.execute(
            "UPDATE students SET grade=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_grade, int(sid)),
        )
        promoted += 1
    if do_commit:
        conn.commit()
    return {"promoted": promoted, "unchanged": unchanged}


def process_term_closing(conn, term_id, *, do_commit=True):
    """Mark term closed; after Term 3, graduate Grade 9 leavers, promote grades, ensure next year."""
    term = get_term_by_id(conn, term_id)
    if not term:
        return {"ok": False, "message": "Term not found."}
    if int(term.get("automation_cancelled") or 0):
        return {"ok": False, "message": "Automation cancelled for this term."}
    if term.get("closing_processed_at"):
        return {"ok": True, "message": "Term already closed.", "skipped": True}

    conn.execute(
        """UPDATE school_terms SET status='closed', closing_processed_at=CURRENT_TIMESTAMP,
           is_current=0, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (int(term_id),),
    )
    grad = None
    promo = None
    if int(term["term_number"]) == 3:
        ensure_next_academic_year(conn, int(term["year_id"]), do_commit=False)
        grad = graduate_grade9_leavers_bulk(conn, do_commit=False)
        promo = promote_students_bulk(conn, do_commit=False)

    next_term = get_next_term(conn, term_id)
    msg = f"Closed {term['label']}."
    if grad and int(grad.get("graduated") or 0) > 0:
        msg += f" Graduated {grad['graduated']} Grade 9 leaver(s)."
    if promo and int(promo.get("promoted") or 0) > 0:
        msg += f" Promoted {promo['promoted']} student(s) to the next grade."
    if next_term:
        nop = _parse_date(next_term.get("opening_date"))
        msg += f" Next term ({next_term['label']}) opens" + (f" on {nop.isoformat()}." if nop else " (set opening date in School Calendar).")
    if do_commit:
        conn.commit()
    return {"ok": True, "message": msg, "graduation": grad, "promotion": promo, "next_term": next_term}


def process_term_opening(conn, term_id, *, bill_active_only=True, do_commit=True):
    """Activate term, bill every active student, and refresh balances from the ledger."""
    import pandas as pd

    term = get_term_by_id(conn, term_id)
    if not term:
        return {"ok": False, "message": "Term not found."}
    if term.get("opening_processed_at"):
        return {"ok": True, "message": "Term already opened.", "skipped": True, "billed": 0}

    conn.execute("UPDATE school_terms SET is_current=0")
    conn.execute(
        """UPDATE school_terms SET is_current=1, status='active',
           opening_processed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (int(term_id),),
    )

    if bill_active_only:
        students_df = pd.read_sql(
            """SELECT * FROM students WHERE COALESCE(status, 'Active') = 'Active'""",
            conn,
        )
    else:
        students_df = pd.read_sql("SELECT * FROM students", conn)

    billed = 0
    for _, row in students_df.iterrows():
        bill_student_for_term(conn, int(row["id"]), term_id, student_row=pd.DataFrame([row]), do_commit=False)
        billed += 1

    if do_commit:
        conn.commit()
    return {
        "ok": True,
        "message": f"Opened {term['label']}. Billed {billed} student(s).",
        "billed": billed,
    }


def run_calendar_automation_if_due(conn, today=None):
    """
    Run close/open actions when dates are due. Returns list of action messages.
    Idempotent per term via closing_processed_at / opening_processed_at.
    """
    today = today or _today()
    messages = []

    current = get_current_term(conn)
    if current:
        close_d = _parse_date(current.get("closing_date"))
        if (
            close_d
            and today >= close_d
            and not current.get("closing_processed_at")
            and not int(current.get("automation_cancelled") or 0)
        ):
            res = process_term_closing(conn, current["id"], do_commit=False)
            messages.append(res.get("message", "Term closed."))
            current = get_term_by_id(conn, current["id"])

    rows = conn.execute(
        """SELECT id, opening_date, opening_processed_at, automation_cancelled, label
           FROM school_terms
           WHERE opening_date IS NOT NULL AND opening_date != ''"""
    ).fetchall()
    for tid, op_s, opened_at, cancelled, label in rows:
        if opened_at or int(cancelled or 0):
            continue
        op = _parse_date(op_s)
        if op and today >= op:
            res = process_term_opening(conn, tid, do_commit=False)
            messages.append(res.get("message", f"Opened {label}."))

    if messages:
        conn.commit()
    return messages


def get_dashboard_calendar_alerts(conn, today=None):
    """Return list of dicts: level (warning|info|success), html message."""
    today = today or _today()
    settings = get_calendar_settings(conn)
    warn_days = int(settings.get("warn_days_before_close") or 14)
    alerts = []

    current = get_current_term(conn)
    if not current:
        alerts.append({
            "level": "info",
            "message": "No current school term is set. Open **School Calendar** under Configuration to set term dates.",
        })
        return alerts

    if int(current.get("automation_cancelled") or 0):
        alerts.append({
            "level": "info",
            "message": (
                f"Automatic rollover is **cancelled** for **{current['label']}**. "
                "Open **School Calendar** to change dates or clear cancellation (admin password)."
            ),
        })
        return alerts

    close_d = _parse_date(current.get("closing_date"))
    if close_d and not current.get("closing_processed_at"):
        days_left = (close_d - today).days
        if 0 <= days_left <= warn_days:
            extra = ""
            if int(current["term_number"]) == 3:
                extra = (
                    " Active **Grade 9** learners will be **graduated** and other grades **promoted** "
                    "when the term closes."
                )
            alerts.append({
                "level": "warning",
                "message": (
                    f"**{current['label']}** closes on **{close_d.isoformat()}** ({days_left} day(s)). "
                    f"VineLedger will close the term and bill the next term on its opening date.{extra} "
                    "To change dates or cancel automation, open **School Calendar** (admin password required)."
                ),
            })
        elif today >= close_d:
            alerts.append({
                "level": "warning",
                "message": (
                    f"**{current['label']}** closing date has passed. "
                    "Open the **Dashboard** or **School Calendar** and use **Run term actions now** "
                    "if automation has not run yet."
                ),
            })

    next_t = get_next_term(conn, current["id"])
    if next_t:
        op = _parse_date(next_t.get("opening_date"))
        if op and today >= op and not next_t.get("opening_processed_at"):
            alerts.append({
                "level": "warning",
                "message": (
                    f"**{next_t['label']}** opening date is **{op.isoformat()}**. "
                    "Billing and balance recalculation will run when term actions execute."
                ),
            })

    return alerts


def cancel_term_automation(conn, term_id, cancelled=True):
    conn.execute(
        "UPDATE school_terms SET automation_cancelled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (1 if cancelled else 0, int(term_id)),
    )
    conn.commit()


def run_term_actions_now(conn, today=None):
    """Force close due terms and open due terms."""
    today = today or _today()
    messages = []
    current = get_current_term(conn)
    if current and not current.get("closing_processed_at"):
        if not int(current.get("automation_cancelled") or 0):
            close_d = _parse_date(current.get("closing_date"))
            if not close_d or today >= close_d:
                res = process_term_closing(conn, current["id"], do_commit=False)
                messages.append(res.get("message", ""))

    rows = conn.execute(
        """SELECT id FROM school_terms
           WHERE opening_processed_at IS NULL AND COALESCE(automation_cancelled, 0)=0"""
    ).fetchall()
    for (tid,) in rows:
        term = get_term_by_id(conn, tid)
        op = _parse_date(term.get("opening_date")) if term else None
        if op and today >= op:
            res = process_term_opening(conn, tid, do_commit=False)
            messages.append(res.get("message", ""))

    if messages:
        conn.commit()
    return messages


def _academic_year_label_for_counts(conn, today=None):
    """School year label used for 'this year' admission/exit counts (e.g. '2025')."""
    today = today or _today()
    term = get_current_term(conn)
    if term:
        row = conn.execute(
            "SELECT label FROM academic_years WHERE id=?", (int(term["year_id"]),)
        ).fetchone()
        if row and row[0]:
            return str(row[0]).strip()
    row = conn.execute(
        "SELECT label FROM academic_years ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row and row[0]:
        return str(row[0]).strip()
    return str(today.year)


def _term_date_window(conn, today=None):
    """Return (start, end) dates for the current term, or None if not configured."""
    today = today or _today()
    term = get_current_term(conn)
    if not term:
        return None
    op = _parse_date(term.get("opening_date"))
    cl = _parse_date(term.get("closing_date")) or today
    if not op:
        return None
    end = cl if cl <= today else today
    return op, end


def _new_admissions_sql_suffix(conn, today=None):
    """Shared filter for term/year admission counts and lists. Returns (sql_suffix, params)."""
    today = today or _today()
    window = _term_date_window(conn, today)
    base = (
        "joined_date IS NOT NULL AND TRIM(joined_date) != ''"
    )
    if window:
        start, end = window
        return (
            base + " AND date(joined_date) >= date(?) AND date(joined_date) <= date(?)",
            (_date_to_storage(start), _date_to_storage(end)),
        )
    y = _academic_year_label_for_counts(conn, today)
    try:
        y = str(int(y))
    except ValueError:
        pass
    return base + " AND strftime('%Y', joined_date)=?", (y,)


def _fetch_students_by_sql_suffix(conn, suffix, params, *, order_by="date(joined_date), name"):
    """Load full student rows matching a SQL WHERE suffix (no leading WHERE)."""
    import pandas as pd

    return pd.read_sql(
        f"SELECT * FROM students WHERE {suffix} ORDER BY {order_by}",
        conn,
        params=params or [],
    )


def fetch_students_new_admissions_this_term(conn, today=None):
    """Active or exited learners who joined in the current term window."""
    suffix, params = _new_admissions_sql_suffix(conn, today)
    return _fetch_students_by_sql_suffix(conn, suffix, params)


def fetch_students_new_admissions_this_year(conn, today=None):
    """Learners with joined_date in the current academic year label."""
    today = today or _today()
    y = _academic_year_label_for_counts(conn, today)
    try:
        y = str(int(y))
    except ValueError:
        y = str(today.year)
    suffix = "joined_date IS NOT NULL AND TRIM(joined_date) != '' AND strftime('%Y', joined_date)=?"
    return _fetch_students_by_sql_suffix(conn, suffix, (y,))


def fetch_students_exits_this_term(conn, today=None):
    """Transfers / scheduled deletions with exited_at in the current term window."""
    suffix, params = _student_exits_sql_suffix(conn, today)
    return _fetch_students_by_sql_suffix(
        conn, suffix, params, order_by="date(exited_at), name"
    )


def list_new_admissions_this_term(conn, today=None):
    """Rows for students who joined in the current term window (or year fallback)."""
    suffix, params = _new_admissions_sql_suffix(conn, today)
    rows = conn.execute(
        f"""SELECT name, student_code, grade, joined_date, status
            FROM students WHERE {suffix}
            ORDER BY date(joined_date), name""",
        params,
    ).fetchall()
    return [
        {
            "name": r[0],
            "student_code": r[1],
            "grade": r[2],
            "joined_date": (str(r[3])[:10] if r[3] else "-"),
            "status": r[4] or "Active",
        }
        for r in rows
    ]


def count_new_admissions_this_term(conn, today=None):
    """Students with joined_date within the current term's opening-closing window."""
    return len(list_new_admissions_this_term(conn, today))


def count_new_admissions_this_year(conn, today=None):
    """Students with joined_date in the current academic year label (calendar year of label)."""
    today = today or _today()
    y = _academic_year_label_for_counts(conn, today)
    try:
        y = str(int(y))
    except ValueError:
        y = str(today.year)
    row = conn.execute(
        """SELECT COUNT(*) FROM students
           WHERE joined_date IS NOT NULL AND TRIM(joined_date) != ''
           AND strftime('%Y', joined_date)=?""",
        (y,),
    ).fetchone()
    return int(row[0] if row else 0)


_EXIT_STATUSES_SQL = "('Transferred', 'Scheduled for Deletion')"


def count_graduated_students(conn):
    """Learners who completed Grade 9 and left (archived; not current enrolment)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM students WHERE status = 'Graduated'"
    ).fetchone()
    return int(row[0] if row else 0)


def _student_exits_sql_suffix(conn, today=None):
    """Shared filter for term/year exit counts and lists."""
    today = today or _today()
    window = _term_date_window(conn, today)
    base = (
        f"status IN {_EXIT_STATUSES_SQL} "
        "AND exited_at IS NOT NULL AND TRIM(exited_at) != ''"
    )
    if window:
        start, end = window
        return (
            base + " AND date(exited_at) >= date(?) AND date(exited_at) <= date(?)",
            (_date_to_storage(start), _date_to_storage(end)),
        )
    y = _academic_year_label_for_counts(conn, today)
    try:
        y = str(int(y))
    except ValueError:
        pass
    return base + " AND strftime('%Y', exited_at)=?", (y,)


def list_student_exits_this_term(conn, today=None):
    """Rows for transfers / scheduled deletions in the current term window."""
    suffix, params = _student_exits_sql_suffix(conn, today)
    rows = conn.execute(
        f"""SELECT name, student_code, grade, exited_at, status
            FROM students WHERE {suffix}
            ORDER BY date(exited_at), name""",
        params,
    ).fetchall()
    return [
        {
            "name": r[0],
            "student_code": r[1],
            "grade": r[2],
            "exited_at": (str(r[3])[:10] if r[3] else "-"),
            "status": r[4],
        }
        for r in rows
    ]


def count_student_exits_this_term(conn, today=None):
    """Transferred or scheduled-for-deletion with exited_at in the current term window."""
    return len(list_student_exits_this_term(conn, today))


def count_student_exits_this_year(conn, today=None):
    """Transferred or scheduled-for-deletion with exited_at in the academic year."""
    today = today or _today()
    y = _academic_year_label_for_counts(conn, today)
    try:
        y = str(int(y))
    except ValueError:
        y = str(today.year)
    row = conn.execute(
        f"""SELECT COUNT(*) FROM students
           WHERE status IN {_EXIT_STATUSES_SQL}
           AND exited_at IS NOT NULL AND TRIM(exited_at) != ''
           AND strftime('%Y', exited_at)=?""",
        (y,),
    ).fetchone()
    return int(row[0] if row else 0)
