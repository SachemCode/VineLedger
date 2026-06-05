"""
SQLite schema initialization and lightweight migrations for VineLedger.

On init_db(), creates students, payments, fee_structure, staff, expenses, school activities,
pending_reviews (saved-for-later drafts), academic calendar tables, gate_audit, and app_action_audit.
"""

import os
import secrets
import sqlite3
import string
from datetime import date, datetime

# Days after "Delete student" / transfer before rows are permanently removed from the database.
STUDENT_DELETION_GRACE_DAYS = 25

# SQLite file path (used by init_db and standalone gate audit logging).
# Override with VINELEDGER_SQLITE_PATH so every client hits the same file when you run one server over Tailscale.
SQLITE_DB_PATH = os.environ.get("VINELEDGER_SQLITE_PATH", "school.db")


def configure_sqlite_connection(conn):
    """
    Tune SQLite for several browsers hitting one Streamlit server (writes can overlap).
    WAL + busy_timeout reduces 'database is locked' failures; check_same_thread=False matches Streamlit threading.
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    try:
        conn.execute("PRAGMA busy_timeout=8000")
    except sqlite3.Error:
        pass
    try:
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    try:
        conn.execute("PRAGMA temp_store=MEMORY")
    except sqlite3.Error:
        pass
    try:
        conn.execute("PRAGMA cache_size=-64000")
    except sqlite3.Error:
        pass
    try:
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error:
        pass


def connect_sqlite(path=None):
    """Open SQLite with settings suitable for Streamlit + multiple concurrent sessions."""
    db_path = path or SQLITE_DB_PATH
    conn = sqlite3.connect(db_path, check_same_thread=False)
    configure_sqlite_connection(conn)
    return conn


def _maybe_first_term_carry_on_reset(conn):
    """
    One-time on first app start after upgrade: clear carry-on and align ledgers to
    students.balance (spreadsheet imports). Skipped after carry_on_launch_reset_done=1.
    """
    c = conn.cursor()
    try:
        c.execute(
            "ALTER TABLE school_calendar_settings ADD COLUMN carry_on_launch_reset_done INTEGER DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass
    row = c.execute(
        "SELECT COALESCE(carry_on_launch_reset_done, 0) FROM school_calendar_settings WHERE id=1"
    ).fetchone()
    if row and int(row[0] or 0):
        return
    from school_calendar import clear_carry_on_balances_for_launch

    clear_carry_on_balances_for_launch(conn, do_commit=False)
    c.execute("UPDATE school_calendar_settings SET carry_on_launch_reset_done=1 WHERE id=1")


def _maybe_balance_reimport_reset(conn):
    """
    Migration: ensure balance_reimport_reset_done exists and mark it complete.

    Earlier builds called reset_all_student_balances_not_set() here once, which cleared
    every learner balance on first app start — that was unsafe for live databases.
    We only flip the flag now; use scripts/recover_balances_from_audit.py if you need
    to restore balances from app_action_audit after a bad run.
    """
    c = conn.cursor()
    try:
        c.execute(
            "ALTER TABLE school_calendar_settings ADD COLUMN balance_reimport_reset_done INTEGER DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass
    row = c.execute(
        "SELECT COALESCE(balance_reimport_reset_done, 0) FROM school_calendar_settings WHERE id=1"
    ).fetchone()
    if row and int(row[0] or 0):
        return
    c.execute("UPDATE school_calendar_settings SET balance_reimport_reset_done=1 WHERE id=1")


def _init_academic_calendar(conn):
    """
    Seed academic year/terms if missing and backfill student_term_billing once.

    Runs on every init_db() but skips billing backfill when any ledger rows exist.
    Ensures exactly one school_terms row has is_current=1 when possible.
    """
    c = conn.cursor()
    c.execute(
        """INSERT OR IGNORE INTO school_calendar_settings (id, warn_days_before_close)
           VALUES (1, 14)"""
    )

    year_row = c.execute("SELECT id, label FROM academic_years ORDER BY id DESC LIMIT 1").fetchone()
    if not year_row:
        label = str(date.today().year)
        c.execute("INSERT INTO academic_years (label) VALUES (?)", (label,))
        year_id = c.lastrowid
        for tn in (1, 2, 3):
            c.execute(
                """INSERT INTO school_terms (year_id, term_number, label, is_current, status)
                   VALUES (?, ?, ?, ?, ?)""",
                (year_id, tn, f"{label} Term {tn}", 1 if tn == 1 else 0, "active" if tn == 1 else "upcoming"),
            )
    else:
        year_id = year_row[0]
        label = year_row[1]
        for tn in (1, 2, 3):
            if not c.execute(
                "SELECT 1 FROM school_terms WHERE year_id=? AND term_number=?",
                (year_id, tn),
            ).fetchone():
                c.execute(
                    """INSERT INTO school_terms (year_id, term_number, label, is_current, status)
                       VALUES (?, ?, ?, ?, ?)""",
                    (year_id, tn, f"{label} Term {tn}", 1 if tn == 1 else 0, "active" if tn == 1 else "upcoming"),
                )

    current = c.execute(
        "SELECT id FROM school_terms WHERE is_current=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if not current:
        first = c.execute(
            "SELECT id FROM school_terms WHERE year_id=? ORDER BY term_number LIMIT 1",
            (year_id,),
        ).fetchone()
        if first:
            c.execute("UPDATE school_terms SET is_current=0")
            c.execute(
                "UPDATE school_terms SET is_current=1, status='active' WHERE id=?",
                (first[0],),
            )
            current = first

    if not current:
        return

    term_id = current[0]
    billed_students = c.execute(
        "SELECT COUNT(DISTINCT student_id) FROM student_term_billing"
    ).fetchone()[0]
    if billed_students:
        return

    from utils import calculate_student_fees, parse_co_curricular_ids
    import json

    rows = c.execute(
        """SELECT id, grade, has_transport, transport_route_id, has_meal,
                  co_curricular_activities, include_admission_fees, include_interview_fee, balance, total_paid
           FROM students"""
    ).fetchall()
    for row in rows:
        sid, grade, has_tr, tr_id, has_meal, cc_raw, inc_adm, inc_int, bal, total_paid = row
        cc_ids = parse_co_curricular_ids(cc_raw, conn=conn, student_id=sid)
        inc_a = bool(inc_adm) if inc_adm is not None else False
        inc_i = bool(inc_int) if inc_int is not None else False
        fee = calculate_student_fees(
            conn,
            grade,
            tr_id if has_tr else None,
            cc_ids if cc_ids else None,
            bool(has_meal),
            include_admission=inc_a,
            include_interview=inc_i,
        )
        amount = float(fee.get("grand_total") or 0)
        if amount <= 0 and total_paid is not None and bal is not None:
            amount = max(float(bal or 0) + float(total_paid or 0), 0)
        breakdown = json.dumps(fee.get("fee_breakdown") or [])
        c.execute(
            """INSERT INTO student_term_billing
               (student_id, term_id, amount_billed, fee_breakdown_json, opening_balance,
                grade_at_billing, admission_included, interview_fee_included)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sid,
                term_id,
                amount,
                breakdown,
                0.0,
                grade,
                1 if inc_a else 0,
                1 if inc_i else 0,
            ),
        )

    for row in c.execute("SELECT id FROM students").fetchall():
        sid = row[0]
        total_billed = c.execute(
            "SELECT COALESCE(SUM(amount_billed), 0) FROM student_term_billing WHERE student_id=?",
            (sid,),
        ).fetchone()[0]
        total_paid = c.execute(
            "SELECT COALESCE(total_paid, 0) FROM students WHERE id=?", (sid,)
        ).fetchone()[0]
        new_bal = max(float(total_billed) - float(total_paid), 0)
        c.execute("UPDATE students SET balance=? WHERE id=?", (new_bal, sid))

# Spreadsheet header rows mistaken for learners during bulk import (matched case-insensitively).
_HEADER_PHANTOM_STUDENT_NAMES = ("NAMES",)


def _delete_student_and_related(c, student_id):
    """Remove dependent rows before students (SQLite FKs are not all ON DELETE CASCADE)."""
    c.execute("DELETE FROM student_term_billing WHERE student_id=?", (student_id,))
    c.execute("DELETE FROM payments WHERE student_id=?", (student_id,))
    c.execute("DELETE FROM student_fee_items WHERE student_id=?", (student_id,))
    c.execute("DELETE FROM students WHERE id=?", (student_id,))


def remove_header_phantom_students(c):
    """Permanently delete mistaken 'NAMES' (column header) import rows only."""
    placeholders = ",".join("?" * len(_HEADER_PHANTOM_STUDENT_NAMES))
    c.execute(
        f"SELECT id FROM students WHERE UPPER(TRIM(name)) IN ({placeholders})",
        _HEADER_PHANTOM_STUDENT_NAMES,
    )
    ids = [row[0] for row in c.fetchall()]
    for sid in ids:
        _delete_student_and_related(c, sid)
    return len(ids)


def purge_students_past_deletion_date(c):
    """Hard-delete students whose scheduled removal date has passed."""
    c.execute(
        """
        SELECT id FROM students
        WHERE status IN ('Scheduled for Deletion', 'Transferred')
          AND deletion_scheduled IS NOT NULL
          AND datetime(deletion_scheduled) <= datetime('now')
        """
    )
    ids = [row[0] for row in c.fetchall()]
    for sid in ids:
        _delete_student_and_related(c, sid)
    return len(ids)


def init_db():
    conn = connect_sqlite()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS students(
        id INTEGER PRIMARY KEY,
        student_code TEXT UNIQUE,
        name TEXT,
        parent_name TEXT,
        parent_phone TEXT,
        grade TEXT,
        balance REAL DEFAULT 0.0,
        total_paid REAL DEFAULT 0.0,
        has_transport BOOLEAN DEFAULT 0,
        transport_route TEXT,
        extra_classes TEXT,
        has_meal BOOLEAN DEFAULT 0,
        co_curricular_activities TEXT,
        transport_route_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY,
        student_id INTEGER,
        amount REAL,
        payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        transaction_id TEXT,
        description TEXT,
        matched BOOLEAN DEFAULT 0,
        FOREIGN KEY (student_id) REFERENCES students (id)
    )''')

    try:
        c.execute("ALTER TABLE payments ADD COLUMN purpose TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE payments ADD COLUMN payment_method TEXT")
    except sqlite3.OperationalError:
        pass
    # internal_payment_id: add as plain TEXT first (some SQLite builds reject UNIQUE on ADD COLUMN;
    # a bare try/except previously hid that and left the column missing).
    pay_cols = {row[1] for row in c.execute("PRAGMA table_info(payments)").fetchall()}
    if "internal_payment_id" not in pay_cols:
        try:
            c.execute("ALTER TABLE payments ADD COLUMN internal_payment_id TEXT")
        except sqlite3.OperationalError:
            pass
    try:
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_internal_payment_id_unique "
            "ON payments(internal_payment_id) "
            "WHERE internal_payment_id IS NOT NULL AND TRIM(internal_payment_id) != ''"
        )
    except sqlite3.OperationalError:
        pass

    c.execute('''CREATE TABLE IF NOT EXISTS staff(
        id INTEGER PRIMARY KEY,
        staff_id TEXT UNIQUE,
        name TEXT,
        email TEXT,
        phone TEXT,
        position TEXT,
        department TEXT,
        salary REAL,
        hire_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'Active',
        deletion_scheduled TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    for _staff_sql in (
        "ALTER TABLE staff ADD COLUMN bank_name TEXT",
        "ALTER TABLE staff ADD COLUMN bank_account_number TEXT",
        "ALTER TABLE staff ADD COLUMN bank_branch TEXT",
        "ALTER TABLE staff ADD COLUMN bank_other_details TEXT",
        "ALTER TABLE staff ADD COLUMN teaching_subjects TEXT",
        "ALTER TABLE staff ADD COLUMN teaching_grades TEXT",
    ):
        try:
            c.execute(_staff_sql)
        except sqlite3.OperationalError:
            pass

    c.execute(
        """CREATE TABLE IF NOT EXISTS school_activities(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        activity_date TEXT,
        location TEXT,
        status TEXT DEFAULT 'planned',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_school_activities_status ON school_activities(status)")

    c.execute(
        """CREATE TABLE IF NOT EXISTS school_activity_participants(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        activity_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(activity_id, student_id)
    )"""
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_school_activity_participants_activity ON school_activity_participants(activity_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_school_activity_participants_student ON school_activity_participants(student_id)"
    )

    c.execute('''CREATE TABLE IF NOT EXISTS expenses(
        id INTEGER PRIMARY KEY,
        category TEXT,
        custom_label TEXT,
        amount REAL,
        expense_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        description TEXT,
        payment_method TEXT,
        vendor TEXT,
        receipt_number TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS expense_categories(
        id INTEGER PRIMARY KEY,
        name TEXT,
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # --- FEE STRUCTURE TABLE ---
    c.execute('''CREATE TABLE IF NOT EXISTS fee_structure(
        id INTEGER PRIMARY KEY,
        fee_category TEXT NOT NULL,
        fee_name TEXT NOT NULL,
        fee_amount REAL NOT NULL,
        grade_applicable TEXT,
        transport_route TEXT,
        is_optional BOOLEAN DEFAULT 1,
        is_one_time BOOLEAN DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # --- STUDENT FEE ITEMS (junction table for selected optional items) ---
    c.execute('''CREATE TABLE IF NOT EXISTS student_fee_items(
        id INTEGER PRIMARY KEY,
        student_id INTEGER NOT NULL,
        fee_item_id INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
        FOREIGN KEY (fee_item_id) REFERENCES fee_structure(id) ON DELETE CASCADE
    )''')

    # Insert default expense categories (idempotent: only adds names not already present)
    default_categories = [
        ("Admin", "Office administration, licences, and general admin costs"),
        ("Events", "School events, trips, and activities"),
        ("Food", "Meals, catering, and kitchen supplies"),
        ("Maintenance", "Building, grounds, and equipment upkeep"),
        ("Other", "Miscellaneous expenses"),
        ("School Fees", "Tuition and other school-related fees"),
        ("Sports", "Sports programs, kits, and fixtures"),
        ("Supplies", "Books, stationery, and learning materials"),
        ("Support", "Learning support, counselling, and allied services"),
        ("Transport", "School transport and vehicle costs"),
        ("Utilities", "Water, electricity, internet, and communications"),
    ]
    default_categories = sorted(default_categories, key=lambda x: x[0].lower())
    for cat_name, cat_desc in default_categories:
        c.execute(
            """INSERT INTO expense_categories (name, description)
               SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM expense_categories WHERE name = ?)""",
            (cat_name, cat_desc, cat_name),
        )

    # --- INSERT DEFAULT FEE STRUCTURE ---
    # Check if fee structure already populated
    result = c.execute("SELECT COUNT(*) FROM fee_structure").fetchone()
    if result[0] == 0:
        # Tuition fees per grade
        tuition_fees = [
            ("tuition", "Playgroup Tuition", 7000.0, "Playgroup", None, 0, 0),
            ("tuition", "PP1 Tuition", 7500.0, "PP1", None, 0, 0),
            ("tuition", "PP2 Tuition", 7500.0, "PP2", None, 0, 0),
            ("tuition", "Grade 1 Tuition", 8000.0, "Grade 1", None, 0, 0),
            ("tuition", "Grade 2 Tuition", 8000.0, "Grade 2", None, 0, 0),
            ("tuition", "Grade 3 Tuition", 8000.0, "Grade 3", None, 0, 0),
            ("tuition", "Grade 4 Tuition", 8500.0, "Grade 4", None, 0, 0),
            ("tuition", "Grade 5 Tuition", 8500.0, "Grade 5", None, 0, 0),
            ("tuition", "Grade 6 Tuition", 8500.0, "Grade 6", None, 0, 0),
        ]
        # Tuition for Grade 7-9 - not specified in the image, assume same as Grade 6
        for g in ["Grade 7", "Grade 8", "Grade 9"]:
            tuition_fees.append(("tuition", f"{g} Tuition", 8500.0, g, None, 0, 0))
        
        for fee in tuition_fees:
            c.execute('''INSERT INTO fee_structure (fee_category, fee_name, fee_amount, grade_applicable, transport_route, is_optional, is_one_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?)''', fee)

        # One-time admission fees
        c.execute('''INSERT INTO fee_structure (fee_category, fee_name, fee_amount, grade_applicable, transport_route, is_optional, is_one_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''', ("admission", "Admission Fee", 1000.0, None, None, 0, 1))
        c.execute('''INSERT INTO fee_structure (fee_category, fee_name, fee_amount, grade_applicable, transport_route, is_optional, is_one_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''', ("admission", "Admission Interview Fee", 700.0, None, None, 0, 1))

        # Mandatory per-term fees
        mandatory_fees = [
            ("mandatory", "Exam Fee", 1200.0),
            ("mandatory", "School Activity Fee", 1500.0),
            ("mandatory", "Computer & Technical Subjects", 2700.0),
            ("mandatory", "Assessment Book", 700.0),
        ]
        for fee_cat, fee_name, fee_amount in mandatory_fees:
            c.execute('''INSERT INTO fee_structure (fee_category, fee_name, fee_amount, grade_applicable, transport_route, is_optional, is_one_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?)''', (fee_cat, fee_name, fee_amount, None, None, 0, 0))

        # Transport routes
        transport_routes = [
            ("transport", "Kihunguro", 7300.0, "Kihunguro"),
            ("transport", "Ruiru Town, Membley, Gitambaya, Varsity, Wonders", 7800.0, "Ruiru Town / Membley / Gitambaya / Varsity / Wonders"),
            ("transport", "Rainbow, Kairu, BTL, Kamakis, Upper Membly, Prisons, Githunguri", 8300.0, "Rainbow / Kairu / BTL / Kamakis / Upper Mbly / Prisons / Githunguri"),
            ("transport", "Kimbo, Toll, Kahawa, Corner, K.U, Tatu", 8800.0, "Kimbo / Toll / Kahawa / Corner / K.U / Tatu"),
            ("transport", "Githurai, Karuguru, Mugutha, Murera, K-Road, West, Mitikenda", 9300.0, "Githurai / Karuguru / Mugutha / Murera / K-Road / West / Mitikenda"),
            ("transport", "One Way", 6800.0, "One Way"),
        ]
        for fee_cat, fee_name, fee_amount, route in transport_routes:
            c.execute('''INSERT INTO fee_structure (fee_category, fee_name, fee_amount, grade_applicable, transport_route, is_optional, is_one_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?)''', (fee_cat, fee_name, fee_amount, None, route, 1, 0))

        # Co-curricular activities
        co_curricular = [
            "Chess", "Musical Instruments", "Taekwondo", "Skating",
            "Football", "French", "Sign Language", "First Aid", "Acrobat",
            "Scouting", "Swimming Club",
        ]
        for activity in co_curricular:
            c.execute('''INSERT INTO fee_structure (fee_category, fee_name, fee_amount, grade_applicable, transport_route, is_optional, is_one_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?)''', ("co_curricular", activity, 3000.0, None, None, 1, 0))

        # Meal program
        c.execute('''INSERT INTO fee_structure (fee_category, fee_name, fee_amount, grade_applicable, transport_route, is_optional, is_one_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''', ("meal", "Lunch and Break", 5000.0, None, None, 1, 0))

    # Add management fields to students table
    try:
        c.execute("ALTER TABLE students ADD COLUMN status TEXT DEFAULT 'Active'")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE students ADD COLUMN deletion_scheduled TIMESTAMP")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE students ADD COLUMN transfer_reason TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE students ADD COLUMN has_meal BOOLEAN DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE students ADD COLUMN co_curricular_activities TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE students ADD COLUMN transport_route_id INTEGER")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE students ADD COLUMN include_admission_fees INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE students ADD COLUMN include_interview_fee INTEGER DEFAULT 0")
        _added_interview_fee_col = True
    except sqlite3.OperationalError:
        _added_interview_fee_col = False
    if _added_interview_fee_col:
        try:
            c.execute(
                "UPDATE students SET include_interview_fee = 1 WHERE COALESCE(include_admission_fees, 0) = 1"
            )
        except sqlite3.OperationalError:
            pass

    try:
        c.execute("ALTER TABLE students ADD COLUMN date_of_birth TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE students ADD COLUMN parent2_name TEXT")
    except sqlite3.OperationalError:
        pass

    # New admission join date (set when "New Admission?" is checked on Add Student)
    try:
        c.execute("ALTER TABLE students ADD COLUMN joined_date TEXT")
    except sqlite3.OperationalError:
        pass

    # When learner is marked transferred or scheduled for deletion
    try:
        c.execute("ALTER TABLE students ADD COLUMN exited_at TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE students ADD COLUMN deletion_reason TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE students ADD COLUMN parent2_phone TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE students ADD COLUMN is_sponsored INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE students ADD COLUMN balance_set INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute(
            "ALTER TABLE students ADD COLUMN balance_status TEXT DEFAULT 'set'"
        )
    except sqlite3.OperationalError:
        pass
    c.execute(
        """
        UPDATE students SET balance_status='not_set'
        WHERE COALESCE(balance_set, 0) = 0
          AND (balance_status IS NULL OR balance_status = 'set')
        """
    )
    c.execute(
        """
        UPDATE students SET balance_status='cleared'
        WHERE COALESCE(balance_set, 0) = 1
          AND COALESCE(balance, 0) < 0.01
          AND (balance_status IS NULL OR balance_status = 'set')
        """
    )
    c.execute(
        """
        UPDATE students SET balance_status='set'
        WHERE COALESCE(balance_set, 0) = 1
          AND COALESCE(balance, 0) >= 0.01
          AND (balance_status IS NULL OR balance_status = 'set')
        """
    )

    # Meal program label (legacy DBs may still say Lunch & Breakfast)
    c.execute(
        """UPDATE fee_structure SET fee_name='Lunch and Break'
           WHERE fee_category='meal' AND fee_name IN ('Lunch & Breakfast', 'Lunch and Breakfast')"""
    )

    # Create performance indexes
    c.execute("CREATE INDEX IF NOT EXISTS idx_students_parent_phone ON students(parent_phone)")
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_students_parent2_phone ON students(parent2_phone)")
    except sqlite3.OperationalError:
        pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_students_student_code ON students(student_code)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_students_name ON students(name)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_students_updated_at ON students(updated_at)")
    
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_students_status ON students(status)")
    except sqlite3.OperationalError:
        pass
    
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_students_deletion_scheduled ON students(deletion_scheduled)")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_students_joined_date ON students(joined_date)")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_students_exited_at ON students(exited_at)")
    except sqlite3.OperationalError:
        pass

    c.execute("CREATE INDEX IF NOT EXISTS idx_payments_student_id ON payments(student_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_payments_payment_date ON payments(payment_date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_payments_matched ON payments(matched)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_payments_description ON payments(description)")
    
    c.execute("CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses(category)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_expenses_expense_date ON expenses(expense_date)")
    
    c.execute("CREATE INDEX IF NOT EXISTS idx_staff_staff_id ON staff(staff_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_staff_name ON staff(name)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_staff_status ON staff(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_staff_deletion_scheduled ON staff(deletion_scheduled)")

    c.execute("CREATE INDEX IF NOT EXISTS idx_fee_structure_category ON fee_structure(fee_category)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_fee_structure_grade ON fee_structure(grade_applicable)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_student_fee_items_student ON student_fee_items(student_id)")

    c.execute(
        """CREATE TABLE IF NOT EXISTS pending_reviews(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        draft_type TEXT NOT NULL,
        draft_key TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(draft_type, draft_key)
    )"""
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_reviews_type ON pending_reviews(draft_type)"
    )

    # --- Academic calendar & term billing ledger --------------------------------
    # academic_years / school_terms: Term 1–3 dates and automation flags
    # student_term_billing: per-student per-term amount_billed + opening_balance (carry-on)
    # school_calendar_settings: dashboard warning days before term close

    c.execute(
        """CREATE TABLE IF NOT EXISTS academic_years(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )"""
    )

    c.execute(
        """CREATE TABLE IF NOT EXISTS school_terms(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        year_id INTEGER NOT NULL,
        term_number INTEGER NOT NULL,
        label TEXT NOT NULL,
        opening_date TEXT,
        closing_date TEXT,
        is_current INTEGER DEFAULT 0,
        status TEXT DEFAULT 'upcoming',
        automation_cancelled INTEGER DEFAULT 0,
        closing_processed_at TEXT,
        opening_processed_at TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (year_id) REFERENCES academic_years(id),
        UNIQUE(year_id, term_number)
    )"""
    )

    c.execute(
        """CREATE TABLE IF NOT EXISTS student_term_billing(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        term_id INTEGER NOT NULL,
        amount_billed REAL NOT NULL DEFAULT 0,
        fee_breakdown_json TEXT,
        opening_balance REAL DEFAULT 0,
        grade_at_billing TEXT,
        admission_included INTEGER DEFAULT 0,
        interview_fee_included INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (student_id) REFERENCES students(id),
        FOREIGN KEY (term_id) REFERENCES school_terms(id),
        UNIQUE(student_id, term_id)
    )"""
    )

    c.execute(
        """CREATE TABLE IF NOT EXISTS school_calendar_settings(
        id INTEGER PRIMARY KEY CHECK (id = 1),
        warn_days_before_close INTEGER NOT NULL DEFAULT 14
    )"""
    )

    c.execute("CREATE INDEX IF NOT EXISTS idx_school_terms_year ON school_terms(year_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_school_terms_current ON school_terms(is_current)")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_student_term_billing_student ON student_term_billing(student_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_student_term_billing_term ON student_term_billing(term_id)"
    )

    try:
        c.execute(
            "ALTER TABLE student_term_billing ADD COLUMN interview_fee_included INTEGER DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass

    c.execute(
        """CREATE TABLE IF NOT EXISTS gate_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        user_slug TEXT NOT NULL,
        event TEXT NOT NULL,
        detail TEXT
    )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_gate_audit_ts ON gate_audit(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_gate_audit_user ON gate_audit(user_slug)")

    c.execute(
        """CREATE TABLE IF NOT EXISTS app_action_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        user_slug TEXT NOT NULL,
        action_area TEXT NOT NULL,
        save_mode TEXT NOT NULL,
        summary TEXT NOT NULL,
        detail TEXT,
        entity_type TEXT,
        entity_id INTEGER,
        entity_code TEXT,
        internal_payment_id TEXT
    )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_app_action_audit_ts ON app_action_audit(ts)")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_action_audit_user ON app_action_audit(user_slug)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_action_audit_payment ON app_action_audit(internal_payment_id)"
    )

    _init_academic_calendar(conn)
    _maybe_first_term_carry_on_reset(conn)
    _maybe_balance_reimport_reset(conn)

    # Rename legacy co-curricular fee labels (existing DBs)
    for old_name, new_name in (
        ("Keyboard / Music Instruments", "Musical Instruments"),
        ("French / Chinese", "French"),
    ):
        c.execute(
            "UPDATE fee_structure SET fee_name=? WHERE fee_category='co_curricular' AND fee_name=?",
            (new_name, old_name),
        )

    # Co-curricular rows added after older seeds (keeps existing DBs in sync with current fee sheet)
    for activity, amt in (("Scouting", 3000.0), ("Swimming Club", 3000.0)):
        if not c.execute(
            "SELECT 1 FROM fee_structure WHERE fee_category='co_curricular' AND fee_name=?",
            (activity,),
        ).fetchone():
            c.execute(
                """INSERT INTO fee_structure (fee_category, fee_name, fee_amount, grade_applicable, transport_route, is_optional, is_one_time)
                   VALUES ('co_curricular', ?, ?, NULL, NULL, 1, 0)""",
                (activity, amt),
            )

    remove_header_phantom_students(c)
    purge_students_past_deletion_date(c)

    conn.commit()
    return conn


def log_gate_event(conn, user_slug, event, detail=None):
    """
    Append-only audit row for app gate (login, logout, idle_timeout, login_failed).
    Commits immediately so events survive standalone short-lived connections.
    """
    ts = datetime.now().isoformat(timespec="seconds")
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO gate_audit (ts, user_slug, event, detail) VALUES (?, ?, ?, ?)",
        (ts, user_slug or "", event, detail),
    )
    conn.commit()


def log_gate_event_ephemeral(user_slug, event, detail=None):
    """Log a gate audit event using a short-lived connection (e.g. Streamlit fragment ticks)."""
    conn = connect_sqlite()
    try:
        log_gate_event(conn, user_slug, event, detail)
    finally:
        conn.close()


_ALNUM_ID_CHARS = string.ascii_uppercase + string.digits


def new_payment_alnum_ref(length: int = 9) -> str:
    """Cryptographically random uppercase letter + digit string (default 9 chars)."""
    return "".join(secrets.choice(_ALNUM_ID_CHARS) for _ in range(length))


def new_internal_payment_id() -> str:
    """
    Stable unique id for each payment row (audit / receipts / Payment History "Transaction ID").
    Format: 9 uppercase letters and digits (e.g. ``K4P2M8QX1``), unique in ``payments``.
    """
    for _ in range(48):
        cand = new_payment_alnum_ref(9)
        try:
            with connect_sqlite() as con:
                row = con.execute(
                    "SELECT 1 FROM payments WHERE internal_payment_id = ? LIMIT 1",
                    (cand,),
                ).fetchone()
            if row is None:
                return cand
        except sqlite3.Error:
            return cand
    return new_payment_alnum_ref(9) + new_payment_alnum_ref(4)


def log_app_action(
    conn,
    user_slug,
    action_area,
    summary,
    *,
    save_mode="immediate",
    detail=None,
    entity_type=None,
    entity_id=None,
    entity_code=None,
    internal_payment_id=None,
):
    """
    High-level business audit (who did what, when, save-now vs pending).
    summary: one-line human description; detail: optional longer text or JSON string.
    """
    ts = datetime.now().isoformat(timespec="seconds")
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO app_action_audit
           (ts, user_slug, action_area, save_mode, summary, detail, entity_type, entity_id, entity_code, internal_payment_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ts,
            user_slug or "",
            action_area,
            save_mode,
            summary,
            detail,
            entity_type,
            entity_id,
            entity_code,
            internal_payment_id,
        ),
    )
    conn.commit()