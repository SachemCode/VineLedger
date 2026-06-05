# VineLedger School Management System
#
# Streamlit front-end for school fee collection, expenses, and records.
# Major areas in this file:
#   - Theme/CSS and sidebar navigation
#   - Student and staff CRUD, bulk import, pending-review queues
#   - Payment Management (bank upload, manual payments), school activities (purposes + rosters)
#   - View Students insight categories (including carry-on ledger)
#   - Receipt generation, fee structure, school calendar UI
#   - Dashboard metrics and deep links (e.g. ?carry_student=<id> for carry-on)
#
# Business logic for fees, PDF parsing, and receipts lives in utils.py.
# Academic terms, term billing, and rollover automation live in school_calendar.py.
# Schema creation and migrations live in database.py.

import os
from pathlib import Path


def _bootstrap_gate_env_from_dotenv():
    """Load project `.env` into os.environ if present (only sets keys that are not already set)."""
    path = Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        os.environ.setdefault(key, val)


_bootstrap_gate_env_from_dotenv()

import streamlit as st
import pandas as pd
import sqlite3
from database import (
    init_db,
    log_app_action,
    log_gate_event,
    STUDENT_DELETION_GRACE_DAYS,
    new_internal_payment_id,
    new_payment_alnum_ref,
    connect_sqlite,
    SQLITE_DB_PATH,
    resolve_sqlite_database_path,
    snapshot_sqlite_database_bytes,
)
from gate_auth import clear_gate_session, load_gate_password_map, render_global_gate, touch_gate_activity
from utils import (
    extract_transactions,
    match_payment,
    load_payment_transaction_id_hints,
    apply_optional_other_payer_for_payment,
    generate_bulk_plain_receipts_pdf,
    create_filled_template,
    create_filled_templates_bulk,
    calculate_student_fees,
    interview_fee_amount_for_grade,
    sync_student_fees_from_db,
    resync_all_student_balances,
    student_is_sponsored,
    student_row_bool,
    read_student_spreadsheet,
    bulk_import_students_from_dataframe,
    STUDENT_IMPORT_TEMPLATE_CSV,
    STUDENT_IMPORT_OPTIONAL_SPECS,
    suggest_student_import_name_column,
    preview_sheet_grade_for_bulk_import,
    dedupe_students_in_grade_by_name,
    REAL_GRADES,
    INCOMPLETE_GRADE_LABEL,
    GRADE_CHOICES_EDIT,
    GRADE_PROGRESSION_ARROWS,
    grade_progression_through,
    sort_dataframe_by_grade,
    sort_grade_labels,
    normalize_kenya_msisdn,
    parse_co_curricular_ids,
    co_curricular_ids_to_json,
    save_student_co_curricular,
    CLUB_ROSTER_IMPORT_TEMPLATE_CSV,
    parse_club_roster_dataframe,
    import_club_roster_assignments,
    enroll_students_in_club,
    GRADE_ROSTER_IMPORT_TEMPLATE_CSV,
    grade_roster_detected_columns,
    parse_grade_roster_dataframe,
    import_grade_roster_updates,
    persist_grade_bulk_contact_edits,
    parse_date_of_birth_cell,
    infer_grade_from_filename,
    get_next_student_code,
    display_student_code,
    persist_student_edit,
    verify_student_edit_saved,
    merge_student_edit_payload,
    get_student_record,
    fetch_all_pending_reviews,
    upsert_pending_student_review,
    delete_pending_student_review,
    upsert_pending_student_transfer_review,
    delete_pending_student_transfer_review,
    upsert_pending_student_deletion_review,
    delete_pending_student_deletion_review,
    schedule_student_transfer,
    schedule_student_deletion,
    student_balance_is_set,
    student_balance_is_outstanding,
    sum_outstanding_balance_rows,
    student_balance_status,
    format_student_balance_display,
    balance_editor_display,
    parse_balance_editor_value,
    apply_student_balance_entry,
    BALANCE_STATUS_NOT_SET,
    BALANCE_STATUS_CLEARED,
    BALANCE_STATUS_SET,
    BALANCE_DISPLAY_NOT_SET,
    BALANCE_DISPLAY_CLEARED,
    persist_meal_program_bulk_edits,
    persist_transport_users_bulk_edits,
    insert_pending_manual_payment_review,
    delete_pending_manual_payment_review,
    insert_pending_expense_review,
    delete_pending_expense_review,
    upsert_pending_bulk_draft,
    delete_pending_bulk_draft,
    DRAFT_TYPE_CLUB_BULK,
    DRAFT_TYPE_GRADE_CONTACT_BULK,
    DRAFT_TYPE_BALANCE_BULK,
    DRAFT_TYPE_MEAL_BULK,
    DRAFT_TYPE_TRANSPORT_BULK,
    BALANCE_ROSTER_IMPORT_TEMPLATE_CSV,
    parse_balance_roster_dataframe,
    balance_roster_detected_columns,
    prepare_balance_roster_dataframe,
    import_balance_roster_updates,
    persist_balance_bulk_edits,
    parse_balance_cell,
    sweep_stale_receipt_cache_files,
)
from school_calendar import (
    cancel_term_automation,
    count_new_admissions_this_term,
    count_new_admissions_this_year,
    count_graduated_students,
    count_student_exits_this_term,
    count_student_exits_this_year,
    fetch_students_exits_this_term,
    fetch_students_new_admissions_this_term,
    fetch_students_new_admissions_this_year,
    get_calendar_settings,
    get_carry_on_opening_map,
    get_current_term,
    get_current_term_carry_on,
    get_dashboard_calendar_alerts,
    get_student_carry_on_breakdown,
    get_student_term_ledger,
    get_terms_for_year,
    list_academic_years,
    run_calendar_automation_if_due,
    run_term_actions_now,
    save_school_calendar_year,
    validate_term_dates,
)
import base64
import html as html_module
import json
import uuid
import re
import textwrap
import uuid
import webbrowser
from datetime import date, datetime, timedelta
from functools import lru_cache

# Shown when scheduling student record deletion (Manage Students)
STUDENT_DELETION_REASON_OPTIONS = (
    "Duplicate record",
    "Student does not exist",
    "No longer a student at this school",
    "Left for another school",
    "Other (specify below)",
)


def scheduled_deletion_datetime_str():
    """ISO datetime string for SQLite (avoids binding pandas Timestamp)."""
    return (datetime.now() + timedelta(days=STUDENT_DELETION_GRACE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")


def resolve_deletion_reason_text(preset_choice, custom_text):
    """Build stored deletion_reason from preset or custom entry."""
    choice = (preset_choice or "").strip()
    if choice.startswith("Other"):
        custom = (custom_text or "").strip()
        return custom or None
    return choice or None


def student_status_label(student_row):
    """Normalized status string (defaults to Active)."""
    if hasattr(student_row, "get"):
        return str(student_row.get("status") or "Active").strip() or "Active"
    return str(student_row["status"] if student_row.get("status") is not None else "Active").strip() or "Active"


def student_record_is_editable(student_row):
    """Only Active students can be edited or marked for transfer/deletion again."""
    return student_status_label(student_row) == "Active"


def student_row_is_active(student_row):
    """True if the learner is currently enrolled (Active status)."""
    return student_status_label(student_row) == "Active"


def active_students_mask(df):
    """Boolean Series: True for Active rows in a students dataframe."""
    if df is None or df.empty:
        return pd.Series(dtype=bool)
    if "status" not in df.columns:
        return pd.Series(True, index=df.index)
    return df["status"].fillna("Active").astype(str).str.strip().eq("Active")


def _student_code_display_cell(x):
    """Normalize learner code for UI tables (legacy numeric-only → VINE####)."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    return display_student_code(str(x).strip())


def view_students_display_mask(df, *, include_exited=False):
    """View Students: never show Graduated (archived). Optionally include transfer/deletion exits."""
    if df is None or df.empty:
        return pd.Series(dtype=bool)
    if "status" not in df.columns:
        return pd.Series(True, index=df.index)
    st = df["status"].fillna("Active").astype(str).str.strip()
    mask = ~st.eq("Graduated")
    if not include_exited:
        mask &= st.eq("Active")
    return mask


VIEW_STUDENTS_INSIGHT_CATEGORIES = (
    "new_admissions_term",
    "new_admissions_year",
    "student_exits_term",
    "meal_program",
    "transport",
    "outstanding",
    "carry_on",
)

VIEW_STUDENTS_CATEGORY_META = {
    "new_admissions_term": {
        "title": "New admissions (this term)",
        "eyebrow": "Insight",
        "color": "#8b5cf6",
        "caption": "Students who joined during the current term (name, grade, join date).",
    },
    "new_admissions_year": {
        "title": "New admissions (this year)",
        "eyebrow": "Insight",
        "color": "#06b6d4",
        "caption": "Students who joined during the current school year.",
    },
    "student_exits_term": {
        "title": "Student exits (this term)",
        "eyebrow": "Insight",
        "color": "#f59e0b",
        "caption": "Transfers and scheduled deletions this term (not graduates).",
    },
    "meal_program": {
        "title": "Meal program students",
        "eyebrow": "Insight",
        "color": "var(--success)",
        "caption": "Active learners enrolled in the meals program.",
    },
    "transport": {
        "title": "Transport users",
        "eyebrow": "Insight",
        "color": "var(--info)",
        "caption": "Active learners using school transport.",
    },
    "outstanding": {
        "title": "Outstanding balances",
        "eyebrow": "Insight",
        "color": "var(--danger)",
        "caption": "Active learners with a fee balance greater than zero.",
    },
    "carry_on": {
        "title": "Carry on",
        "eyebrow": "Insight",
        "color": "var(--secondary)",
        "caption": "Amounts unpaid at term close and brought into later terms.",
    },
}


def _metric_card_nav_link(category_key, card_inner_html):
    """Wrap a dashboard metric card so it opens View Students with that insight category."""
    safe = html_module.escape(str(category_key))
    return f'<a class="metric-card-link" href="?view_students={safe}">{card_inner_html}</a>'


def _filter_students_search_df(df, search_term):
    """Filter a students dataframe by name, code, grade, or parent contact fields."""
    q = (search_term or "").strip().lower()
    if not q or df is None or df.empty:
        return df
    mask = pd.Series(False, index=df.index)
    for col in (
        "name",
        "student_code",
        "grade",
        "parent_name",
        "parent_phone",
        "parent2_name",
        "parent2_phone",
    ):
        if col in df.columns:
            mask |= df[col].fillna("").astype(str).str.lower().str.contains(q, regex=False)
    return df.loc[mask].copy()


def _club_assign_suggestion_rows(df, search_term, *, grade_filter, intent_ids, cap=12):
    """
    Rows for club-assign autocomplete: non-empty search only; grade filter; hide ids already in intent;
    prefix matches on name first, then cap.
    """
    q = (search_term or "").strip()
    if not q or df is None or df.empty:
        return df.iloc[0:0].copy()
    pool = _filter_students_search_df(df, q)
    if pool.empty:
        return pool
    if grade_filter and str(grade_filter) != "All grades":
        pool = pool.loc[pool["grade"].astype(str) == str(grade_filter)].copy()
    if pool.empty:
        return pool
    intent_set = {int(x) for x in (intent_ids or [])}
    if intent_set:
        pool = pool.loc[~pool["id"].astype(int).isin(intent_set)].copy()
    if pool.empty:
        return pool
    ql = q.lower()
    nm = pool["name"].fillna("").astype(str).str.lower()
    pool = pool.assign(_pfx=nm.str.startswith(ql).astype(int))
    pool = pool.sort_values(["_pfx", "name"], ascending=[False, True]).drop(columns=["_pfx"])
    return pool.head(int(cap)).copy()


def _text_matches_needle(haystack, needle):
    """True if needle is empty or needle appears as substring in haystack (case-insensitive)."""
    n = (needle or "").strip().lower()
    if not n:
        return True
    return n in str(haystack or "").lower()


def _bulk_student_row_search_mask(df, q):
    """Boolean mask: name, student_code (raw + display), optional parent columns."""
    if df is None or df.empty:
        return pd.Series(dtype=bool)
    qs = (q or "").strip().lower()
    if not qs:
        return pd.Series(True, index=df.index)
    parts = [df["name"].fillna("").astype(str).str.lower()]
    if "student_code" in df.columns:
        parts.append(df["student_code"].fillna("").astype(str).str.lower())
        parts.append(df["student_code"].map(_student_code_display_cell).fillna("").astype(str).str.lower())
    for col in ("parent_name", "parent_phone", "parent2_name", "parent2_phone"):
        if col in df.columns:
            parts.append(df[col].fillna("").astype(str).str.lower())
    if "balance" in df.columns:
        parts.append(df["balance"].fillna("").astype(str).str.lower())
    if "meals" in df.columns:
        parts.append(df["meals"].fillna("").astype(str).str.lower())
    if "transport_choice" in df.columns:
        parts.append(df["transport_choice"].fillna("").astype(str).str.lower())
    blob = pd.concat(parts, axis=1).apply(lambda r: " ".join(r.astype(str)), axis=1)
    return blob.str.contains(qs, regex=False)


def _merge_partial_edits_into_full(full_df, partial_edited_df):
    """
    Overlay rows from partial_edited_df onto full_df by matching ``id``.
    Both must share the same column names for columns present in partial.
    """
    if full_df is None or full_df.empty:
        return full_df
    if partial_edited_df is None or partial_edited_df.empty:
        return full_df.copy()
    out = full_df.copy()
    for _, row in partial_edited_df.iterrows():
        sid = int(row["id"])
        m = out["id"] == sid
        if not m.any():
            continue
        for col in partial_edited_df.columns:
            if col != "id" and col in out.columns:
                out.loc[m, col] = row[col]
    return out


def _pool_df_with_grade(df, fixed_grade):
    """Ensure ``grade`` column exists for assign-suggestion grade filters."""
    if df is None or df.empty:
        return df
    if "grade" in df.columns:
        return df
    return df.assign(grade=str(fixed_grade))


def _count_view_students_category(conn, category_key):
    """Count learners in a View Students insight category."""
    if category_key == "new_admissions_term":
        return count_new_admissions_this_term(conn)
    if category_key == "new_admissions_year":
        return count_new_admissions_this_year(conn)
    if category_key == "student_exits_term":
        return count_student_exits_this_term(conn)
    if category_key == "meal_program":
        row = conn.execute(
            """SELECT COUNT(*) FROM students
               WHERE COALESCE(status, 'Active') = 'Active'
               AND COALESCE(has_meal, 0) != 0"""
        ).fetchone()
        return int(row[0] if row else 0)
    if category_key == "transport":
        row = conn.execute(
            """SELECT COUNT(*) FROM students
               WHERE COALESCE(status, 'Active') = 'Active'
               AND COALESCE(has_transport, 0) != 0"""
        ).fetchone()
        return int(row[0] if row else 0)
    if category_key == "outstanding":
        row = conn.execute(
            """SELECT COUNT(*) FROM students
               WHERE COALESCE(status, 'Active') = 'Active'
               AND COALESCE(balance_status, 'set') = 'set'
               AND COALESCE(balance, 0) > 0"""
        ).fetchone()
        return int(row[0] if row else 0)
    if category_key == "carry_on":
        _ids = conn.execute("SELECT id FROM students").fetchall()
        if not _ids:
            return 0
        _co_map = get_carry_on_opening_map(conn, [int(r[0]) for r in _ids])
        return sum(1 for v in _co_map.values() if float(v or 0) > 0.01)
    return 0


def _load_view_students_category_df(conn, category_key):
    """Load student rows for a View Students insight category."""
    if category_key == "new_admissions_term":
        df = fetch_students_new_admissions_this_term(conn)
        return sort_dataframe_by_grade(df, "grade", then_by="joined_date")
    if category_key == "new_admissions_year":
        df = fetch_students_new_admissions_this_year(conn)
        return sort_dataframe_by_grade(df, "grade", then_by="joined_date")
    if category_key == "student_exits_term":
        df = fetch_students_exits_this_term(conn)
        return sort_dataframe_by_grade(df, "grade", then_by="exited_at")
    base = pd.read_sql("SELECT * FROM students ORDER BY name", conn)
    active = base.loc[active_students_mask(base)].copy()
    if category_key == "meal_program":
        return active.loc[
            active.apply(lambda r: student_row_bool(r, "has_meal", False), axis=1)
        ]
    if category_key == "transport":
        return active.loc[
            active.apply(lambda r: student_row_bool(r, "has_transport", False), axis=1)
        ]
    if category_key == "outstanding":
        out = active.loc[
            active.apply(lambda r: student_balance_is_outstanding(r), axis=1)
        ].copy()
        return out.sort_values("balance", ascending=False)
    return pd.DataFrame()


def _render_view_students_records_list(
    conn,
    df,
    *,
    list_title,
    search_placeholder,
    pending_edits,
    ledger_key_suffix,
    include_exited=False,
    show_include_exited_checkbox=False,
):
    """Shared table + search UI for grade and insight category lists in View Students."""
    st.markdown('<div class="form-container">', unsafe_allow_html=True)
    search_term = st.text_input(
        f"Search {list_title}",
        placeholder=search_placeholder,
        key=f"view_students_search_{ledger_key_suffix}",
    )
    if show_include_exited_checkbox:
        include_exited = st.checkbox(
            "Include transferred / scheduled for deletion (graduates are archived, not listed here)",
            value=include_exited,
            key=f"view_students_include_exited_{ledger_key_suffix}",
        )
    st.markdown("</div>", unsafe_allow_html=True)

    if not df.empty:
        df = df.loc[view_students_display_mask(df, include_exited=include_exited)].copy()
    if search_term:
        df = _filter_students_search_df(df, search_term)

    if df.empty:
        st.info(f"No students match your filters in **{list_title}**.")
        return

    st.markdown(
        f'<div class="info-message">Showing <strong>{len(df)}</strong> student(s)</div>',
        unsafe_allow_html=True,
    )

    df = apply_pending_student_edits_to_df(df, pending_edits)
    df = add_pending_draft_status_column(df, pending_edits)
    display_df = enrich_students_for_view(df, conn)
    if "student_code" in display_df.columns:
        display_df = display_df.copy()
        display_df["student_code"] = display_df["student_code"].map(_student_code_display_cell)

    def _yn(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "No"
        if isinstance(v, bool):
            return "Yes" if v else "No"
        try:
            return "Yes" if int(v) else "No"
        except (TypeError, ValueError):
            return "Yes" if v else "No"

    if "has_transport" in display_df.columns:
        display_df["has_transport"] = display_df["has_transport"].apply(_yn)
    if "has_meal" in display_df.columns:
        display_df["has_meal"] = display_df["has_meal"].apply(_yn)
    if "include_admission_fees" in display_df.columns:
        display_df["include_admission_fees"] = display_df["include_admission_fees"].apply(_yn)
    if "include_interview_fee" in display_df.columns:
        display_df["include_interview_fee"] = display_df["include_interview_fee"].apply(_yn)
    if "total_paid" in display_df.columns:
        display_df["total_paid"] = display_df["total_paid"].apply(lambda x: f"KSH {x:,.0f}")

    for _hc in VIEW_STUDENTS_HIDDEN_COLUMNS:
        if _hc in display_df.columns:
            display_df = display_df.drop(columns=[_hc])

    display_df = display_df.rename(
        columns={
            c: VIEW_STUDENTS_SQL_COLUMN_LABELS.get(c, c.replace("_", " ").title())
            for c in display_df.columns
        }
    )

    _pref = [
        "Save status",
        "Student Code",
        "Name",
        "Grade",
        "Date of birth",
        "Age",
        "Joined",
        "Parent/Guardian 1 Name",
        "Parent/Guardian 1 Phone",
        "Parent/Guardian 2 Name",
        "Parent/Guardian 2 Phone",
        "School transport",
        "Transport route",
        "Meals program",
        "Co-curricular",
        "Balance",
        "Total Paid",
        "Most recent payment",
        "To be deleted",
        "Days till deleted",
        "Deletion reason",
        "Transfer reason",
        "Transferred",
        "Status",
        "Admission fees",
    ]
    _ordered = [c for c in _pref if c in display_df.columns]
    _rest = [c for c in display_df.columns if c not in _ordered]
    display_df = display_df[_ordered + _rest]

    st.markdown('<div class="dataframe-container">', unsafe_allow_html=True)
    render_students_table_with_bf(df, display_df, conn)
    st.markdown("</div>", unsafe_allow_html=True)

    _ledger_opts = df["id"].astype(int).tolist()
    _ledger_labels = {
        int(r["id"]): f"{r['name']} ({_student_code_display_cell(r.get('student_code'))})"
        for _, r in df.iterrows()
    }
    _ledger_sid = st.selectbox(
        "View term billing history for a student",
        options=_ledger_opts,
        format_func=lambda sid: _ledger_labels[int(sid)],
        key=f"view_students_ledger_{ledger_key_suffix}",
    )
    if _ledger_sid:
        render_student_term_ledger(conn, int(_ledger_sid))


_APP_DIR = Path(__file__).resolve().parent
CLUB_STATIC_DIR = _APP_DIR / "static" / "clubs"
LOGO_WEBP = _APP_DIR / "Vinegrape logo white background.webp"
LOGO_HEADER_LIGHT = _APP_DIR / "Vinegrape_Academy_Logo-removebg-preview.png"

# View Students: `SELECT *` column order must match labels (see PRAGMA table_info(students)).
VIEW_STUDENTS_SQL_COLUMN_LABELS = {
    "id": "ID",
    "student_code": "Student Code",
    "name": "Name",
    "parent_name": "Parent/Guardian 1 Name",
    "parent_phone": "Parent/Guardian 1 Phone",
    "parent2_name": "Parent/Guardian 2 Name",
    "parent2_phone": "Parent/Guardian 2 Phone",
    "grade": "Grade",
    "balance": "Balance",
    "total_paid": "Total Paid",
    "has_transport": "School transport",
    "transport_route": "Route (legacy)",
    "extra_classes": "Extra classes",
    "has_meal": "Meals program",
    "co_curricular_activities": "Co-curricular",
    "transport_route_id": "Transport route ID",
    "created_at": "Created",
    "updated_at": "Updated",
    "status": "Status",
    "deletion_scheduled": "Deletion scheduled",
    "transfer_reason": "Transfer reason",
    "transferred": "Transferred",
    "most_recent_payment": "Most recent payment",
    "transport_route": "Transport route",
    "include_admission_fees": "Admission fee",
    "include_interview_fee": "Interview fee",
    "pending_save_status": "Save status",
    "date_of_birth": "Date of birth",
    "age": "Age",
    "joined_date": "Joined",
    "to_be_deleted": "To be deleted",
    "days_till_deleted": "Days till deleted",
    "deletion_reason": "Deletion reason",
}

# Hidden in View Students (internal / replaced by friendlier columns)
VIEW_STUDENTS_HIDDEN_COLUMNS = (
    "id",
    "transport_route_id",
    "extra_classes",
    "deletion_scheduled",
)
st.set_page_config(
    page_title="VineLedger",
    page_icon=str(LOGO_WEBP),
    layout="wide",
    initial_sidebar_state="expanded",
)

if "_vineledger_receipt_cache_swept" not in st.session_state:
    st.session_state._vineledger_receipt_cache_swept = True
    try:
        sweep_stale_receipt_cache_files()
    except Exception:
        pass

# Database Connection Pool Class
# Manages database connections for better performance and resource management
class ConnectionPool:
    def __init__(self, db_path, max_connections=5):
        """Initialize connection pool with database path and maximum connections"""
        self.db_path = db_path
        self.max_connections = max_connections
        self.connections = []
        self.current = 0
    
    def get_connection(self):
        """Get a database connection from the pool or create a new one"""
        if self.connections:
            conn = self.connections.pop()
        else:
            conn = connect_sqlite(self.db_path)
        return conn
    
    def return_connection(self, conn):
        """Return a connection to the pool or close if pool is full"""
        if len(self.connections) < self.max_connections:
            self.connections.append(conn)
        else:
            conn.close()

# Global connection pool instance for the application
conn_pool = ConnectionPool(SQLITE_DB_PATH)

@lru_cache(maxsize=128)
def get_cached_students():
    """Cache student data for frequently accessed information to improve performance"""
    conn = conn_pool.get_connection()
    try:
        students_df = pd.read_sql("SELECT * FROM students", conn)
        return students_df
    finally:
        conn_pool.return_connection(conn)


def invalidate_student_cache():
    get_cached_students.cache_clear()


def _render_receipt_pdf_actions(
    pdf_path,
    *,
    download_label,
    download_file_name,
    print_button_key,
    download_button_key,
    viewer_key,
):
    """
    In-app PDF preview (``st.pdf`` when available), download from memory, and open in system viewer for print.
    ``pdf_path`` is the temp file written by utils receipt generators.
    """
    if not pdf_path or not os.path.isfile(pdf_path):
        return
    with open(pdf_path, "rb") as _pdf_f:
        pdf_bytes = _pdf_f.read()
    st_pdf = getattr(st, "pdf", None)
    if callable(st_pdf):
        st_pdf(pdf_bytes, height=720, key=viewer_key)
        st.caption("Use your browser's **Print** dialog (Ctrl/Cmd+P) to print from the preview above.")
    else:
        st.caption(
            "For an in-app preview and print, install **streamlit[pdf]** (see README). "
            "You can still **Download** or **Open for printing** below."
        )
    _c1, _c2 = st.columns(2)
    with _c1:
        st.download_button(
            label=download_label,
            data=pdf_bytes,
            file_name=download_file_name,
            mime="application/pdf",
            use_container_width=True,
            key=download_button_key,
        )
    with _c2:
        if st.button("Open for printing", key=print_button_key, use_container_width=True):
            try:
                webbrowser.open(f"file://{os.path.abspath(pdf_path)}")
            except Exception as _e:
                st.warning(f"Could not open file: {_e}")


# --- Carry-on (brought-forward) balances ------------------------------------
# When a term opens, unpaid balance from the previous term is stored as
# opening_balance on the new term's student_term_billing row. The (co) superscript
# in student tables links here via ?carry_student=<id> query parameter.

CO_SUPERSCRIPT = "(co)"
CO_TOOLTIP = "Carry-on balance from a prior term (unpaid at term close)"


def navigate_to_carry_on(student_id):
    """Open View Students → Carry on for one learner (programmatic navigation)."""
    st.session_state.current_page = "View Students"
    st.session_state.view_students_category = "carry_on"
    st.session_state.carry_on_student_id = int(student_id)
    st.session_state.selected_grade = None
    st.session_state.selected_cc_club = None
    st.rerun()


def format_balance_cell_html(
    balance, student_id, carry_on_amount, *, balance_status=None, balance_set=None
):
    """HTML for balance column: optional (co) link above KSH amount when carry-on applies."""
    label = format_student_balance_display(
        balance, balance_set=balance_set, balance_status=balance_status
    )
    if label in (BALANCE_DISPLAY_NOT_SET, BALANCE_DISPLAY_CLEARED):
        return html_module.escape(label)
    bal_s = label
    if not carry_on_amount or float(carry_on_amount) < 0.01:
        return html_module.escape(bal_s)
    co_s = f"KSH {float(carry_on_amount):,.0f}"
    sid = int(student_id)
    return (
        f'<div class="balance-with-bf">'
        f'<a href="?carry_student={sid}" class="co-sup-link" title="{html_module.escape(CO_TOOLTIP)} — {co_s}">'
        f"<sup>{CO_SUPERSCRIPT}</sup></a><br>"
        f"<span>{html_module.escape(bal_s)}</span></div>"
    )


def render_students_table_with_bf(df_raw, display_df, conn):
    """Render student table with native dataframe toolbar (search, download, fullscreen)."""
    ids = df_raw["id"].astype(int).tolist()
    carry_map = get_carry_on_opening_map(conn, ids)
    has_bf = any(v > 0.01 for v in carry_map.values())

    out = display_df.copy()
    if "Balance" in out.columns:
        def _fmt_bal_cell(pos):
            if pos >= len(df_raw):
                val = out.iloc[pos]["Balance"]
            else:
                row = df_raw.iloc[pos]
                val = format_student_balance_display(student_row=row)
            if str(val).strip().lower() in ("not set", "cleared"):
                return val
            if val is not None and not (isinstance(val, float) and pd.isna(val)) and str(val).strip() != "":
                if str(val).startswith("KSH"):
                    return str(val)
                try:
                    return f"KSH {float(val):,.0f}"
                except (TypeError, ValueError):
                    return str(val)
            return "—"

        out["Balance"] = [ _fmt_bal_cell(i) for i in range(len(out)) ]

    column_config = {}
    if has_bf:
        st.caption(
            f"**{CO_SUPERSCRIPT}** = carry-on from a prior term. Use the **Carry-on** column to open that learner’s record."
        )
        co_urls = []
        co_amounts = []
        for pos in range(len(df_raw)):
            sid = int(df_raw.iloc[pos]["id"])
            amt = float(carry_map.get(sid, 0) or 0)
            if amt > 0.01:
                co_urls.append(f"?carry_student={sid}")
                co_amounts.append(f"KSH {amt:,.0f}")
            else:
                co_urls.append(None)
                co_amounts.append("—")
        # Insert after Balance for readability
        if "Balance" in out.columns:
            bal_idx = out.columns.get_loc("Balance") + 1
            out.insert(bal_idx, "Carry-on (KSH)", co_amounts)
            out.insert(bal_idx + 1, "Carry-on", co_urls)
        else:
            out["Carry-on (KSH)"] = co_amounts
            out["Carry-on"] = co_urls
        column_config["Carry-on"] = st.column_config.LinkColumn(
            "Carry-on",
            help=f"{CO_TOOLTIP} Click to open that learner’s carry-on record.",
            display_text="(co)",
        )

    _df_kw = {"use_container_width": True, "hide_index": True}
    if column_config:
        _df_kw["column_config"] = column_config
    st.dataframe(out, **_df_kw)


def _render_carry_on_student_detail(conn, student_id, student=None):
    """Show carry-on rows: prior term unpaid at close → amount brought into each later term."""
    if student is None:
        student = get_student_record(conn, int(student_id))
    if not student:
        st.warning("Student not found.")
        return
    st.markdown(
        f"**{student['name']}** · `{_student_code_display_cell(student.get('student_code'))}` · {student.get('grade', '—')}",
    )
    items = get_student_carry_on_breakdown(conn, int(student_id))
    if not items:
        st.info("No brought-forward balances on record for this learner.")
        return
    rows = []
    for it in items:
        rows.append({
            "Unpaid at close of": it["from_term"],
            "Brought into": it["into_term"],
            "Amount (KSH)": f"{it['amount']:,.0f}",
            "Current term": "Yes" if it["is_current"] else "",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    cur_co = get_current_term_carry_on(conn, int(student_id))
    if cur_co > 0.01:
        st.markdown(
            f'<div class="info-message">Total brought into the <strong>current</strong> term: '
            f"<strong>KSH {cur_co:,.0f}</strong></div>",
            unsafe_allow_html=True,
        )


def _filter_carry_on_student_list(df, query):
    """Filter carry-on student picker by name, student code, or grade."""
    q = (query or "").strip().lower()
    if not q or df.empty:
        return df
    _code_disp = df["student_code"].map(_student_code_display_cell).fillna("").astype(str).str.lower()
    mask = (
        df["name"].fillna("").astype(str).str.lower().str.contains(q, regex=False)
        | df["student_code"].fillna("").astype(str).str.lower().str.contains(q, regex=False)
        | _code_disp.str.contains(q, regex=False)
        | df["grade"].fillna("").astype(str).str.lower().str.contains(q, regex=False)
    )
    return df[mask]


def _render_carry_on_tab(conn):
    st.markdown(
        '<p class="vine-help-text" style="margin-bottom: 1rem;">'
        "Track amounts that were still owed when a term closed and were carried into the next term’s billing. "
        f"**{CO_SUPERSCRIPT}** in student lists marks a carry-on balance from an earlier term."
        "</p>",
        unsafe_allow_html=True,
    )
    sid = st.session_state.get("carry_on_student_id")
    if sid:
        c_back, c_title = st.columns([1, 4])
        with c_back:
            if st.button("← All students", key="carry_on_clear_focus", use_container_width=True):
                st.session_state.pop("carry_on_student_id", None)
                st.rerun()
        with c_title:
            st.markdown("#### Carry-on record")
        _render_carry_on_student_detail(conn, int(sid))
        return

    students_df = pd.read_sql(
        "SELECT id, name, student_code, grade FROM students ORDER BY name",
        conn,
    )
    if students_df.empty:
        st.info("No students in the database yet.")
        return

    st.text_input(
        "Search students",
        placeholder="Name, student code, or grade…",
        key="carry_on_search",
    )
    _q = st.session_state.get("carry_on_search") or ""
    filtered = _filter_carry_on_student_list(students_df, _q)
    st.caption(f"Showing **{len(filtered)}** of **{len(students_df)}** students.")

    if filtered.empty:
        st.info("No students match your search. Clear the search box to see everyone.")
        return

    _opts = filtered["id"].astype(int).tolist()
    _labels = {
        int(r["id"]): f"{r['name']} — {_student_code_display_cell(r.get('student_code'))} — {r['grade']}"
        for _, r in filtered.iterrows()
    }
    picked = st.selectbox(
        "Select a student",
        options=_opts,
        format_func=lambda x: _labels[int(x)],
        key="carry_on_student_pick",
    )
    if picked:
        _render_carry_on_student_detail(conn, int(picked))


def render_student_term_ledger(conn, student_id):
    """Per-term billing history for View / Manage Students."""
    ledger = get_student_term_ledger(conn, int(student_id))
    with st.expander("Term billing history", expanded=False):
        if not ledger:
            st.caption("No term billing rows yet. Billing is created when a school term opens or when fees are synced.")
            return
        rows = []
        for row in ledger:
            op = (row.get("opening_date") or "")[:10]
            cl = (row.get("closing_date") or "")[:10]
            period = f"{op} – {cl}" if op and cl else (op or cl or "—")
            rows.append({
                "Year": row["year_label"],
                "Term": row["term_label"],
                "Grade billed": row.get("grade_at_billing") or "—",
                "Opening balance": f"KSH {float(row.get('opening_balance') or 0):,.0f}",
                "Amount billed": f"KSH {float(row.get('amount_billed') or 0):,.0f}",
                "Admission in term": "Yes" if row.get("admission_included") else "No",
                "Interview in term": "Yes" if row.get("interview_fee_included") else "No",
                "Period": period,
                "Current": "Yes" if row.get("is_current") else "",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def get_db_connection():
    """Get database connection from the global connection pool"""
    return conn_pool.get_connection()

# Authentication and Security Settings
ADMIN_PASSWORD = "@947654"


# Only these gate accounts may open hidden sidebar tabs (staff, etc.) and use admin-gated actions.
_ELEVATED_GATE_SLUGS = frozenset({"user1", "user2", "user4", "user5"})


def gate_user_can_access_hidden_tabs() -> bool:
    """user3 and any other slug outside user1/2/4/5 cannot open hidden tabs or re-verify into them."""
    slug = str(st.session_state.get("gate_user") or "").strip().lower()
    return slug in _ELEVATED_GATE_SLUGS


def gate_user_has_admin_privileges() -> bool:
    """Same allowlist as hidden tabs: admin password actions are not available to user3 (or unknown slugs)."""
    return gate_user_can_access_hidden_tabs()


def evaluate_admin_password_input(pw: str | None) -> str | None:
    """
    Return None if the submitted admin password is accepted for the current operator.
    Otherwise return a short error message for flash / invalidation.
    """
    entered = (pw or "").strip()
    if not gate_user_has_admin_privileges():
        if entered == ADMIN_PASSWORD:
            return "You do not have permission for this action."
        return "Incorrect password."
    if entered != ADMIN_PASSWORD:
        return "Incorrect password."
    return None


def verify_current_gate_login_password(pw: str | None) -> bool:
    """True if pw matches the signed-in operator's gate password (from env)."""
    passwords, missing = load_gate_password_map()
    if missing:
        return False
    slug = st.session_state.get("gate_user")
    if not slug:
        return False
    expected = passwords.get(str(slug))
    if expected is None:
        return False
    return (pw or "").strip() == str(expected)


def _staff_series_text(row, column: str, default: str = "") -> str:
    """Safe string from a staff table row (pandas Series); missing/NaN → default."""
    if column not in row.index:
        return default
    v = row[column]
    if v is None or pd.isna(v):
        return default
    s = str(v).strip()
    return s if s else default


# Tabs that call check_password() at page entry (sign-in password re-check; user1/2/4/5 only). Add Expense is open without a tab password; Save now still requires the admin password.
PROTECTED_TABS = [
    "Add Staff",
    "View Staff",
    "Manage Staff",
    "Expense Categories & Reports",
]

# Protected tab unlock (sign-in password), cleared on gate logout — see check_password().
if "protected_tabs_unlocked" not in st.session_state:
    st.session_state.protected_tabs_unlocked = False
if "protected_tabs_unlock_gate_user" not in st.session_state:
    st.session_state.protected_tabs_unlock_gate_user = None
if "pending_student_edits" not in st.session_state:
    st.session_state.pending_student_edits = {}
if "pending_student_transfers" not in st.session_state:
    st.session_state.pending_student_transfers = {}
if "pending_student_deletions" not in st.session_state:
    st.session_state.pending_student_deletions = {}
if "pending_expense_drafts" not in st.session_state:
    st.session_state.pending_expense_drafts = []
if "pending_manual_payment_drafts" not in st.session_state:
    st.session_state.pending_manual_payment_drafts = []
if "pending_club_drafts" not in st.session_state:
    st.session_state.pending_club_drafts = []
if "pending_grade_contact_drafts" not in st.session_state:
    st.session_state.pending_grade_contact_drafts = []
if "pending_balance_drafts" not in st.session_state:
    st.session_state.pending_balance_drafts = []
if "pending_meal_drafts" not in st.session_state:
    st.session_state.pending_meal_drafts = []
if "pending_transport_drafts" not in st.session_state:
    st.session_state.pending_transport_drafts = []
if "sidebar_staff_revealed" not in st.session_state:
    st.session_state.sidebar_staff_revealed = False
if "gr_replace_confirm" not in st.session_state:
    st.session_state.gr_replace_confirm = False
if "bank_statement_upload_authorized" not in st.session_state:
    st.session_state.bank_statement_upload_authorized = False


def _clear_password_field_keys(*keys):
    """Clear password text_input widget keys so values do not linger after save or a failed attempt."""
    for k in keys:
        if not k:
            continue
        st.session_state.pop(k, None)


def _invalidate_admin_password_fields(message: str, *password_widget_keys: str, level: str = "error"):
    """
    Clear admin password widget(s) and rerun so typed secrets are not left in widget state.
    Message is shown once on the next run via _vine_app_flash_error / _vine_app_flash_warn.
    """
    flash_key = "_vine_app_flash_error" if level == "error" else "_vine_app_flash_warn"
    st.session_state[flash_key] = message
    _clear_password_field_keys(*password_widget_keys)
    st.rerun()


_STAFF_PAGES_MENU = ("Add Staff", "View Staff", "Manage Staff")


def check_password():
    """
    Protected tabs (staff, expense reports, etc.): require the signed-in operator's **gate password**
    again, and only **user1, user2, user4, or user5** may pass (user3 is always denied).
    """
    if not gate_user_can_access_hidden_tabs():
        st.markdown('<h2 class="section-header">Access restricted</h2>', unsafe_allow_html=True)
        st.markdown(
            '<div class="warning-message">Your account cannot open this section.</div>',
            unsafe_allow_html=True,
        )
        if st.button("Back to Dashboard", type="primary", key="protected_tab_denied_back"):
            st.session_state.current_page = "Dashboard"
            st.rerun()
        st.stop()

    _slug = str(st.session_state.get("gate_user") or "").strip()
    if st.session_state.get("protected_tabs_unlocked") and st.session_state.get("protected_tabs_unlock_gate_user") == _slug:
        return True

    st.session_state.protected_tabs_unlocked = False
    st.session_state.protected_tabs_unlock_gate_user = None

    st.markdown('<h2 class="section-header">Authentication Required</h2>', unsafe_allow_html=True)
    st.markdown(
        '<p class="vine-help-text">Enter the **same password you use to sign in** to this account (not the admin password).</p>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="form-container">', unsafe_allow_html=True)

    password = st.text_input("Sign-in password", type="password", key="password_input")

    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("Submit", type="primary", key="protected_tab_pw_submit"):
            if verify_current_gate_login_password(password):
                st.session_state.protected_tabs_unlocked = True
                st.session_state.protected_tabs_unlock_gate_user = _slug
                _clear_password_field_keys("password_input")
                st.rerun()
            else:
                _invalidate_admin_password_fields(
                    "Incorrect password.",
                    "password_input",
                )

    with col2:
        if st.button("Cancel", key="protected_tab_pw_cancel"):
            st.session_state.current_page = "Dashboard"
            st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)
    st.stop()


def _gate_audit_user():
    return str(st.session_state.get("gate_user") or "unknown")


def _audit_log(conn, action_area, summary, **kwargs):
    """Append a business-action row to app_action_audit (gate user)."""
    log_app_action(conn, _gate_audit_user(), action_area, summary, **kwargs)


def _audit_student_payload_detail(merged):
    """Short JSON of editable fields for audit detail."""
    keys = (
        "name",
        "grade",
        "parent_name",
        "parent_phone",
        "parent2_name",
        "parent2_phone",
        "balance",
        "balance_status",
        "has_transport",
        "has_meal",
        "is_sponsored",
        "include_admission_fees",
        "include_interview_fee",
    )
    chunk = {k: merged.get(k) for k in keys if k in merged}
    cc = merged.get("co_curricular_ids")
    if cc is not None:
        chunk["co_curricular_ids"] = cc
    try:
        s = json.dumps(chunk, default=str)
    except TypeError:
        s = str(chunk)
    return s[:4000]


_EXPENSE_CAT_NONE = "— None —"

# Payment Management → Add payment purpose (dropdown). Also used for receipt purpose defaults.
# Payment Management → Add payment purpose (dropdown). Open school activities append by title (see School Activity page).
PAYMENT_PURPOSE_BASE_OPTIONS = (
    "School Fees",
    "Transport",
    "Uniform",
    "Exam Fee",
    "Admission Fee",
    "Interview Fee",
    "Diary",
    "Assessment Book",
    "Co-curricular",
    "Meal program",
)


def _fetch_school_activity_purpose_labels(conn):
    """Titles of planned activities — each becomes a payment purpose option."""
    rows = conn.execute(
        """
        SELECT title FROM school_activities
        WHERE status = 'planned' AND TRIM(COALESCE(title, '')) != ''
        ORDER BY
            CASE WHEN activity_date IS NULL OR TRIM(COALESCE(activity_date, '')) = '' THEN 1 ELSE 0 END,
            activity_date,
            title
        """
    ).fetchall()
    return [str(r[0]).strip() for r in rows if r and str(r[0]).strip()]


def get_payment_purpose_options(conn, extra_purposes=None):
    """Standard purposes plus any open school activities (unique labels)."""
    extra = tuple(extra_purposes or ())
    out = []
    seen = set()
    for bucket in (PAYMENT_PURPOSE_BASE_OPTIONS, tuple(_fetch_school_activity_purpose_labels(conn)), extra):
        for x in bucket:
            s = (x or "").strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return tuple(out)


def get_receipt_purpose_options(conn):
    return get_payment_purpose_options(conn) + ("Other",)


# Payment Management → Add payment (manual entry) — how the fee was received.
MANUAL_PAYMENT_METHOD_OPTIONS = (
    "Cash",
    "KCB",
    "Equity",
    "Family Bank",
    "M-Pesa",
    "Credit Card",
    "Check",
    "Other",
)

def _week_start_monday(ts):
    ts = pd.Timestamp(ts).normalize()
    return ts - pd.Timedelta(days=int(ts.weekday()))


def _payment_trend_bucket_amounts(payments_df):
    """
    Totals per time bucket for the payment trends chart (no hourly buckets).
    By span of filtered payment dates: day (≤14d), week (15–28d), month (29d–12mo), year (>12mo).
    """
    from datetime import date

    if payments_df is None or payments_df.empty:
        return None, ""
    dfp = payments_df.copy()
    dfp["payment_date"] = pd.to_datetime(dfp["payment_date"])
    min_d = dfp["payment_date"].min().normalize()
    max_d = dfp["payment_date"].max().normalize()
    span_days = int((max_d - min_d).days) + 1
    span_days = max(span_days, 1)
    ref_ws = _week_start_monday(pd.Timestamp(date.today()))

    if span_days <= 14:
        dfp["_b"] = dfp["payment_date"].dt.floor("D")
        agg = dfp.groupby("_b", sort=True)["amount"].sum()
        labels = agg.index.strftime("%Y-%m-%d")
        hint = "Buckets: **one bar per calendar day** (range in data ≤ 14 days)."
    elif span_days <= 28:
        dfp["_b"] = dfp["payment_date"].map(_week_start_monday)
        agg = dfp.groupby("_b", sort=True)["amount"].sum()

        def _week_label(ws):
            ws = pd.Timestamp(ws).normalize()
            w = max(int((ref_ws - ws).days) // 7, 0)
            if w == 0:
                return "This week"
            if w == 1:
                return "1 week ago"
            return f"{w} weeks ago"

        labels = [_week_label(x) for x in agg.index]
        hint = "Buckets: **by week** (range 15–28 days; labels relative to today)."
    elif span_days <= 366:
        dfp["_b"] = dfp["payment_date"].dt.to_period("M").dt.to_timestamp()
        agg = dfp.groupby("_b", sort=True)["amount"].sum()
        labels = agg.index.strftime("%b %Y")
        hint = "Buckets: **one bar per calendar month** (range 29 days to 12 months)."
    else:
        dfp["_b"] = dfp["payment_date"].dt.year
        agg = dfp.groupby("_b", sort=True)["amount"].sum()
        labels = agg.index.astype(str)
        hint = "Buckets: **one bar per calendar year** (range over 12 months)."

    chart_df = pd.DataFrame({"Amount (KSH)": agg.values}, index=labels)
    return chart_df, hint


def _validate_expense_entry(*, category_db, custom_label, amount, description):
    """category_db: truthy string from catalogue, or None/empty if user chose no suggestion."""
    desc = (description or "").strip()
    if not desc:
        return False, "Description is required."
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return False, "Enter a valid amount."
    if amt <= 0:
        return False, "Amount must be greater than zero."
    clab = (custom_label or "").strip()
    cat_ok = bool(category_db and str(category_db).strip())
    if not cat_ok and not clab:
        return (
            False,
            "Choose a category suggestion or enter a custom label (at least one is required).",
        )
    return True, None


def _insert_expense_row(
    conn,
    *,
    category,
    custom_label,
    amount,
    expense_date_str,
    description,
    payment_method,
    vendor,
    receipt_number,
):
    cur = conn.execute(
        """
        INSERT INTO expenses 
        (category, custom_label, amount, expense_date, description, payment_method, vendor, receipt_number)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            category if category else None,
            (custom_label or "").strip() or None,
            float(amount),
            expense_date_str,
            (description or "").strip(),
            payment_method,
            vendor or "",
            receipt_number or "",
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _validate_manual_payment_entry(
    *,
    student_id,
    amount,
    purpose,
    payment_method,
    transaction_id,
):
    if student_id is None:
        return False, "Select a student."
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return False, "Enter a valid amount."
    if amt <= 0:
        return False, "Amount must be greater than zero."
    if not (purpose or "").strip():
        return False, "Purpose is required."
    pm = (payment_method or "").strip().lower()
    tx = (transaction_id or "").strip()
    if pm != "cash" and not tx:
        return False, "Enter a transaction or reference code (not required for Cash)."
    return True, None


def _insert_manual_payment_row(
    conn,
    *,
    student_id,
    amount,
    payment_date_iso,
    payment_method,
    purpose,
    transaction_id,
    description_notes,
):
    """Insert one manual payment, increment total_paid, and resync balance. Returns internal_payment_id and row id."""
    desc = (description_notes or "").strip()
    pay_dt = f"{payment_date_iso} 12:00:00"
    pm = (payment_method or "").strip()
    tx = (transaction_id or "").strip()
    if not tx and pm.lower() == "cash":
        tx = new_payment_alnum_ref(9)
    ipid = new_internal_payment_id()
    cur = conn.execute(
        """
        INSERT INTO payments (student_id, amount, payment_date, transaction_id, description, matched, purpose, payment_method, internal_payment_id)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            int(student_id),
            float(amount),
            pay_dt,
            tx[:200] if tx else "",
            desc,
            (purpose or "").strip(),
            pm,
            ipid,
        ),
    )
    conn.execute(
        "UPDATE students SET total_paid = total_paid + ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (float(amount), int(student_id)),
    )
    conn.commit()
    sync_student_fees_from_db(conn, int(student_id))
    invalidate_student_cache()
    return {"internal_payment_id": ipid, "payment_id": int(cur.lastrowid)}


def student_age_from_dob(dob):
    """Whole years old today; subtracts one year if their birthday has not occurred yet this calendar year."""
    if dob is None or (isinstance(dob, float) and pd.isna(dob)):
        return None
    try:
        bday = pd.Timestamp(dob).normalize()
    except (TypeError, ValueError):
        return None
    if pd.isna(bday):
        return None
    today = pd.Timestamp.now().normalize()
    years = today.year - bday.year
    if (today.month, today.day) < (bday.month, bday.day):
        years -= 1
    return int(years) if years >= 0 else None


def format_student_dob_display(dob):
    if dob is None or (isinstance(dob, float) and pd.isna(dob)) or str(dob).strip() == "":
        return "—"
    try:
        return pd.Timestamp(dob).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return "—"


def meal_program_checkbox_label(conn):
    """Label for meal checkbox from fee_structure (e.g. Lunch and Break)."""
    row = conn.execute(
        "SELECT fee_name, fee_amount FROM fee_structure WHERE fee_category='meal' LIMIT 1"
    ).fetchone()
    if row:
        return f"Meal Program ({row[0]}) - KSH {float(row[1]):,.0f}"
    return "Meal Program (Lunch and Break) - KSH 5,000"


def dob_to_storage(dob_input):
    """ISO date string for SQLite, or None."""
    if dob_input is None:
        return None
    if isinstance(dob_input, date):
        return dob_input.strftime("%Y-%m-%d")
    try:
        ts = pd.Timestamp(dob_input)
        if pd.isna(ts):
            return None
        return ts.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def enrich_students_for_view(df, conn):
    """Human-readable transport, clubs, transferred flag, and latest payment for View Students."""
    import json

    out = df.copy()
    routes = pd.read_sql(
        "SELECT id, fee_name FROM fee_structure WHERE fee_category='transport'", conn
    )
    route_map = {int(r["id"]): r["fee_name"] for _, r in routes.iterrows()}
    cc_rows = pd.read_sql(
        "SELECT id, fee_name FROM fee_structure WHERE fee_category='co_curricular'", conn
    )
    cc_map = {int(r["id"]): r["fee_name"] for _, r in cc_rows.iterrows()}

    def _route_label(row):
        if not student_row_bool(row, "has_transport", False):
            return "—"
        rid = row.get("transport_route_id")
        if rid is None or (isinstance(rid, float) and pd.isna(rid)):
            return "—"
        try:
            return route_map.get(int(rid), "—")
        except (TypeError, ValueError):
            return "—"

    out["transport_route"] = out.apply(_route_label, axis=1)

    def _cc_label_row(row):
        ids = parse_co_curricular_ids(
            row.get("co_curricular_activities"), conn=conn, student_id=row.get("id")
        )
        if not ids:
            return "—"
        names = [cc_map.get(i, str(i)) for i in ids]
        return ", ".join(names) if names else "—"

    if "co_curricular_activities" in out.columns:
        out["co_curricular_activities"] = out.apply(_cc_label_row, axis=1)

    out["transferred"] = out["status"].apply(
        lambda s: "Yes" if str(s or "").strip().casefold() == "transferred" else "No"
    )

    payments_df = pd.read_sql(
        "SELECT student_id, amount, payment_date FROM payments", conn
    )
    if payments_df.empty:
        out["most_recent_payment"] = "—"
    else:
        payments_df = payments_df.copy()
        payments_df["payment_date"] = pd.to_datetime(
            payments_df["payment_date"], errors="coerce"
        )
        recent_idx = payments_df.groupby("student_id")["payment_date"].idxmax()
        recent = payments_df.loc[recent_idx, ["student_id", "amount", "payment_date"]]

        def _pay_cell(r):
            amt = f"KSH {float(r['amount']):,.0f}"
            if pd.notna(r["payment_date"]):
                return f"{amt} ({r['payment_date'].strftime('%Y-%m-%d')})"
            return amt

        recent["most_recent_payment"] = recent.apply(_pay_cell, axis=1)
        pay_by_student = dict(zip(recent["student_id"], recent["most_recent_payment"]))
        out["most_recent_payment"] = out["id"].map(lambda sid: pay_by_student.get(sid, "—"))

    if "date_of_birth" in out.columns:
        def _age_cell(d):
            a = student_age_from_dob(d)
            return a if a is not None else "—"

        out["age"] = out["date_of_birth"].apply(_age_cell)
        out["date_of_birth"] = out["date_of_birth"].apply(format_student_dob_display)

    if "joined_date" in out.columns:
        def _joined_cell(v):
            if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
                return "—"
            return str(v).strip()[:10]

        out["joined_date"] = out["joined_date"].apply(_joined_cell)

    now = pd.Timestamp.now().normalize()
    _exit_pending_cf = frozenset({"scheduled for deletion", "transferred"})

    def _deletion_display(row):
        status_cf = str(row.get("status") or "").strip().casefold()
        sched = row.get("deletion_scheduled")
        exit_pending = status_cf in _exit_pending_cf
        if not exit_pending:
            return pd.Series({"to_be_deleted": "No", "days_till_deleted": "—"})
        sched_missing = sched is None or (
            isinstance(sched, float) and pd.isna(sched)
        ) or (isinstance(sched, str) and not str(sched).strip())
        if sched_missing:
            return pd.Series({"to_be_deleted": "Yes", "days_till_deleted": "—"})
        due = pd.to_datetime(sched, errors="coerce")
        if pd.isna(due):
            return pd.Series({"to_be_deleted": "Yes", "days_till_deleted": "—"})
        days_left = max(0, (due.normalize() - now).days)
        return pd.Series({"to_be_deleted": "Yes", "days_till_deleted": str(days_left)})

    _del_cols = out.apply(_deletion_display, axis=1)
    out["to_be_deleted"] = _del_cols["to_be_deleted"]
    out["days_till_deleted"] = _del_cols["days_till_deleted"]

    def _reason_display(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "—"
        t = str(v).strip()
        return t if t else "—"

    if "deletion_reason" in out.columns:
        out["deletion_reason"] = out["deletion_reason"].apply(_reason_display)
    if "transfer_reason" in out.columns:
        out["transfer_reason"] = out["transfer_reason"].apply(_reason_display)

    if "balance" in out.columns:
        out["balance"] = out.apply(
            lambda r: format_student_balance_display(student_row=r), axis=1
        )

    return out


def students_in_co_curricular_club(students_df, club_id, conn=None):
    """Rows enrolled in a co-curricular club (JSON ids, legacy names, or student_fee_items)."""
    cid = int(club_id)
    if students_df.empty or "co_curricular_activities" not in students_df.columns:
        return students_df.iloc[0:0].copy()

    def _member(row):
        sid = row["id"] if "id" in row.index else None
        ids = parse_co_curricular_ids(
            row.get("co_curricular_activities"), conn=conn, student_id=sid
        )
        return cid in ids

    mask = students_df.apply(_member, axis=1)
    return students_df.loc[mask].copy()


def apply_pending_student_edits_to_df(df, pending_edits):
    """Overlay unsaved Manage Students drafts onto a students dataframe for View Students."""
    if not pending_edits or df is None or df.empty:
        return df
    out = df.copy()
    for sid, pl in pending_edits.items():
        mask = out["id"].astype(int) == int(sid)
        if not mask.any():
            continue
        if pl.get("name") is not None:
            out.loc[mask, "name"] = pl["name"]
        if pl.get("parent_name") is not None:
            out.loc[mask, "parent_name"] = pl["parent_name"]
        if pl.get("parent_phone") is not None:
            out.loc[mask, "parent_phone"] = normalize_kenya_msisdn(pl["parent_phone"])
        if pl.get("parent2_name") is not None:
            out.loc[mask, "parent2_name"] = pl["parent2_name"]
        if pl.get("parent2_phone") is not None:
            out.loc[mask, "parent2_phone"] = normalize_kenya_msisdn(pl["parent2_phone"])
        if pl.get("date_of_birth") is not None:
            out.loc[mask, "date_of_birth"] = pl["date_of_birth"]
        if pl.get("co_curricular_ids") is not None:
            out.loc[mask, "co_curricular_activities"] = co_curricular_ids_to_json(pl["co_curricular_ids"])
        if pl.get("grade") is not None:
            out.loc[mask, "grade"] = pl["grade"]
        if pl.get("has_transport") is not None:
            out.loc[mask, "has_transport"] = int(pl["has_transport"])
        if "selected_transport_id" in pl:
            out.loc[mask, "transport_route_id"] = pl.get("selected_transport_id")
        if pl.get("has_meal") is not None:
            out.loc[mask, "has_meal"] = int(pl["has_meal"])
        if pl.get("include_admission_fees") is not None:
            out.loc[mask, "include_admission_fees"] = int(pl["include_admission_fees"])
        if pl.get("include_interview_fee") is not None:
            out.loc[mask, "include_interview_fee"] = int(pl["include_interview_fee"])
        if pl.get("is_sponsored") is not None:
            out.loc[mask, "is_sponsored"] = int(pl["is_sponsored"])
        if pl.get("balance_status") is not None:
            out.loc[mask, "balance_status"] = pl["balance_status"]
        if pl.get("balance_set") is not None:
            out.loc[mask, "balance_set"] = int(pl["balance_set"])
        if pl.get("balance") is not None:
            out.loc[mask, "balance"] = float(pl["balance"])
    return out


def add_pending_draft_status_column(df, pending_edits):
    """Mark rows that only reflect a Save-for-later draft (not yet written to the database)."""
    if not pending_edits or df is None or df.empty or "id" not in df.columns:
        return df
    out = df.copy()
    pending_ids = {int(k) for k in pending_edits}
    out["pending_save_status"] = out["id"].astype(int).apply(
        lambda sid: "Pending review (not saved)" if sid in pending_ids else "Saved"
    )
    return out


def _student_edit_widget_keys(student_id):
    sid = int(student_id)
    return {
        "name": f"edit_name_{sid}",
        "grade": f"edit_grade_{sid}",
        "parent_name": f"edit_parent_name_{sid}",
        "parent_phone": f"edit_parent_phone_{sid}",
        "parent2_name": f"edit_parent2_name_{sid}",
        "parent2_phone": f"edit_parent2_phone_{sid}",
        "dob": f"edit_dob_{sid}",
        "transport": f"edit_student_transport_{sid}",
        "meal": f"edit_meal_{sid}",
        "admission": f"edit_admission_{sid}",
        "interview": f"edit_interview_{sid}",
        "sponsored": f"edit_sponsored_{sid}",
        "balance": f"edit_balance_{sid}",
        "balance_mode": f"edit_balance_mode_{sid}",
    }


def clear_student_edit_widget_state(student_id):
    """Drop cached edit-form widget values for one student."""
    sid = int(student_id)
    keys = _student_edit_widget_keys(sid)
    for k in keys.values():
        st.session_state.pop(k, None)
    for extra in (
        f"edit_meal_{sid}",
        f"edit_admission_{sid}",
        f"edit_interview_{sid}",
        f"edit_sponsored_{sid}",
        f"edit_balance_{sid}",
        f"edit_balance_mode_{sid}",
    ):
        st.session_state.pop(extra, None)
    for k in list(st.session_state.keys()):
        if k.startswith(f"edit_cc_{sid}_"):
            st.session_state.pop(k, None)


def seed_student_edit_widget_state(student_id, student, pdraft, co_curricular_items, transport_keys, conn):
    """Initialize edit-form session keys from DB row and optional pending draft."""
    sid = int(student_id)
    wk = _student_edit_widget_keys(sid)

    def _draft(field, fallback):
        if pdraft is not None and field in pdraft and pdraft[field] is not None:
            return pdraft[field]
        return fallback

    st.session_state[wk["name"]] = str(_draft("name", student.get("name") or ""))
    _g = student.get("grade")
    if pdraft and pdraft.get("grade"):
        _gp = str(pdraft["grade"]).strip()
        if _gp in GRADE_CHOICES_EDIT:
            _g = _gp
    if _g is None or (isinstance(_g, float) and pd.isna(_g)) or str(_g).strip() == "":
        _g = INCOMPLETE_GRADE_LABEL
    else:
        _g = str(_g).strip()
        if _g not in GRADE_CHOICES_EDIT:
            _g = INCOMPLETE_GRADE_LABEL
    st.session_state[wk["grade"]] = _g
    st.session_state[wk["parent_name"]] = str(_draft("parent_name", student.get("parent_name") or ""))
    st.session_state[wk["parent_phone"]] = str(_draft("parent_phone", student.get("parent_phone") or ""))
    st.session_state[wk["parent2_name"]] = str(_draft("parent2_name", student.get("parent2_name") or ""))
    st.session_state[wk["parent2_phone"]] = str(_draft("parent2_phone", student.get("parent2_phone") or ""))

    _dob_val = None
    _dob_src = _draft("date_of_birth", student.get("date_of_birth"))
    if _dob_src is not None and not (isinstance(_dob_src, float) and pd.isna(_dob_src)):
        try:
            _dob_val = pd.to_datetime(_dob_src).date()
        except (TypeError, ValueError):
            pass
    st.session_state[wk["dob"]] = _dob_val

    default_tkey = "__none__"
    if pdraft and pdraft.get("transport_choice") is not None:
        _tk = str(pdraft["transport_choice"])
        if _tk in transport_keys:
            default_tkey = _tk
    elif bool(student.get("has_transport")) and student.get("transport_route_id") is not None:
        try:
            rid = str(int(student["transport_route_id"]))
            if rid in transport_keys:
                default_tkey = rid
        except (TypeError, ValueError):
            pass
    st.session_state[wk["transport"]] = default_tkey

    existing_cc_ids = parse_co_curricular_ids(
        pdraft.get("co_curricular_ids") if pdraft and pdraft.get("co_curricular_ids") is not None
        else student.get("co_curricular_activities"),
        conn=conn,
        student_id=sid,
    )
    existing_cc_set = {int(x) for x in existing_cc_ids}
    for item in co_curricular_items:
        st.session_state[f"edit_cc_{sid}_{item[0]}"] = int(item[0]) in existing_cc_set

    st.session_state[wk["meal"]] = bool(_draft("has_meal", student.get("has_meal")))
    st.session_state[wk["admission"]] = bool(
        _draft("include_admission_fees", student_row_bool(student, "include_admission_fees", False))
    )
    st.session_state[wk["interview"]] = bool(
        _draft("include_interview_fee", student_row_bool(student, "include_interview_fee", False))
    )
    st.session_state[wk["sponsored"]] = bool(
        _draft("is_sponsored", student_is_sponsored(student))
    )
    if pdraft and pdraft.get("balance_status"):
        _bst = pdraft["balance_status"]
    else:
        _bst = student_balance_status(student)
    if _bst == BALANCE_STATUS_NOT_SET:
        st.session_state[wk["balance_mode"]] = BALANCE_DISPLAY_NOT_SET
        st.session_state[wk["balance"]] = 0.0
    elif _bst == BALANCE_STATUS_CLEARED:
        st.session_state[wk["balance_mode"]] = BALANCE_DISPLAY_CLEARED
        st.session_state[wk["balance"]] = 0.0
    else:
        st.session_state[wk["balance_mode"]] = "Amount (KSH)"
        _bal = float(pdraft.get("balance") if pdraft and pdraft.get("balance") is not None else student.get("balance") or 0)
        st.session_state[wk["balance"]] = _bal


def collect_student_edit_payload(student_id, co_curricular_items):
    """Read every edit-form field from session state after form submit."""
    sid = int(student_id)
    wk = _student_edit_widget_keys(sid)
    transport_choice = st.session_state.get(wk["transport"], "__none__")
    has_transport = transport_choice != "__none__"
    selected_transport_id = int(transport_choice) if has_transport else None
    return {
        "name": (st.session_state.get(wk["name"]) or "").strip(),
        "grade": st.session_state.get(wk["grade"]),
        "parent_name": (st.session_state.get(wk["parent_name"]) or "").strip(),
        "parent_phone": st.session_state.get(wk["parent_phone"]) or "",
        "parent2_name": (st.session_state.get(wk["parent2_name"]) or "").strip(),
        "parent2_phone": st.session_state.get(wk["parent2_phone"]) or "",
        "date_of_birth": dob_to_storage(st.session_state.get(wk["dob"])),
        "transport_choice": transport_choice,
        "has_transport": has_transport,
        "selected_transport_id": selected_transport_id,
        "has_meal": bool(st.session_state.get(wk["meal"], False)),
        "include_admission_fees": int(bool(st.session_state.get(wk["admission"], False))),
        "include_interview_fee": int(bool(st.session_state.get(wk["interview"], False))),
        "is_sponsored": int(bool(st.session_state.get(wk["sponsored"], False))),
        **_balance_fields_from_edit_form(sid),
        "co_curricular_ids": read_form_co_curricular_ids("edit_cc", sid, co_curricular_items),
    }


def _balance_fields_from_edit_form(student_id):
    """Map edit-form balance mode to balance_status + balance for persist."""
    sid = int(student_id)
    mode = st.session_state.get(f"edit_balance_mode_{sid}", BALANCE_DISPLAY_NOT_SET)
    if mode == BALANCE_DISPLAY_NOT_SET:
        return {"balance_status": BALANCE_STATUS_NOT_SET, "balance": None}
    if mode == BALANCE_DISPLAY_CLEARED:
        return {"balance_status": BALANCE_STATUS_CLEARED, "balance": 0.0}
    return {
        "balance_status": BALANCE_STATUS_SET,
        "balance": float(st.session_state.get(f"edit_balance_{sid}", 0)),
    }


def validate_student_edit_payload(payload):
    """Minimal validation — parent contact is optional; change one field at a time."""
    errors = []
    if not (payload.get("name") or "").strip():
        errors.append("Student name cannot be empty.")
    return errors


def total_pending_draft_count():
    return (
        len(st.session_state.get("pending_student_edits") or {})
        + len(st.session_state.get("pending_student_transfers") or {})
        + len(st.session_state.get("pending_student_deletions") or {})
        + len(st.session_state.get("pending_club_drafts") or [])
        + len(st.session_state.get("pending_grade_contact_drafts") or [])
        + len(st.session_state.get("pending_balance_drafts") or [])
        + len(st.session_state.get("pending_meal_drafts") or [])
        + len(st.session_state.get("pending_transport_drafts") or [])
        + len(st.session_state.get("pending_manual_payment_drafts") or [])
        + len(st.session_state.get("pending_expense_drafts") or [])
    )


def manage_students_pending_draft_count():
    """Pending items shown under Manage Students → Pending Reviews (excludes payments/expenses)."""
    return (
        len(st.session_state.get("pending_student_edits") or {})
        + len(st.session_state.get("pending_student_transfers") or {})
        + len(st.session_state.get("pending_student_deletions") or {})
        + len(st.session_state.get("pending_club_drafts") or [])
        + len(st.session_state.get("pending_grade_contact_drafts") or [])
        + len(st.session_state.get("pending_balance_drafts") or [])
        + len(st.session_state.get("pending_meal_drafts") or [])
        + len(st.session_state.get("pending_transport_drafts") or [])
    )


def pending_reviews_notification_surface_count():
    """How many main areas have at least one pending review (Manage Students, payments, expenses)."""
    n = 0
    if manage_students_pending_draft_count() > 0:
        n += 1
    if len(st.session_state.get("pending_manual_payment_drafts") or []) > 0:
        n += 1
    if len(st.session_state.get("pending_expense_drafts") or []) > 0:
        n += 1
    return n


def ensure_pending_reviews_loaded(conn):
    """Restore Save-for-later queues from SQLite after a browser refresh."""
    if st.session_state.get("_pending_reviews_hydrated"):
        return
    data = fetch_all_pending_reviews(conn)
    st.session_state.pending_student_edits = data["students"]
    st.session_state.pending_student_transfers = data.get("student_transfers") or {}
    st.session_state.pending_student_deletions = data.get("student_deletions") or {}
    st.session_state.pending_manual_payment_drafts = data["payments"]
    st.session_state.pending_expense_drafts = data["expenses"]
    st.session_state.pending_club_drafts = data.get("club_drafts") or []
    st.session_state.pending_grade_contact_drafts = data.get("grade_contact_drafts") or []
    st.session_state.pending_balance_drafts = data.get("balance_drafts") or []
    st.session_state.pending_meal_drafts = data.get("meal_drafts") or []
    st.session_state.pending_transport_drafts = data.get("transport_drafts") or []
    st.session_state._pending_reviews_hydrated = True


def queue_pending_student_edit(conn, student_id, payload):
    sid = int(student_id)
    st.session_state.pending_student_edits[sid] = payload
    upsert_pending_student_review(conn, sid, payload)


def remove_pending_student_edit(conn, student_id):
    sid = int(student_id)
    st.session_state.pending_student_edits.pop(sid, None)
    delete_pending_student_review(conn, sid)


def queue_pending_student_transfer(conn, student_id, payload):
    sid = int(student_id)
    st.session_state.pending_student_transfers[sid] = payload
    upsert_pending_student_transfer_review(conn, sid, payload)


def remove_pending_student_transfer(conn, student_id):
    sid = int(student_id)
    st.session_state.pending_student_transfers.pop(sid, None)
    delete_pending_student_transfer_review(conn, sid)


def queue_pending_student_deletion(conn, student_id, payload):
    sid = int(student_id)
    st.session_state.pending_student_deletions[sid] = payload
    upsert_pending_student_deletion_review(conn, sid, payload)


def remove_pending_student_deletion(conn, student_id):
    sid = int(student_id)
    st.session_state.pending_student_deletions.pop(sid, None)
    delete_pending_student_deletion_review(conn, sid)


def queue_pending_manual_payment(conn, draft):
    st.session_state.pending_manual_payment_drafts.append(draft)
    insert_pending_manual_payment_review(conn, draft)


def remove_pending_manual_payment(conn, draft_id):
    st.session_state.pending_manual_payment_drafts = [
        x for x in st.session_state.pending_manual_payment_drafts if x["id"] != draft_id
    ]
    delete_pending_manual_payment_review(conn, draft_id)


def queue_pending_expense(conn, draft):
    st.session_state.pending_expense_drafts.append(draft)
    insert_pending_expense_review(conn, draft)


def remove_pending_expense(conn, draft_id):
    st.session_state.pending_expense_drafts = [
        x for x in st.session_state.pending_expense_drafts if x["id"] != draft_id
    ]
    delete_pending_expense_review(conn, draft_id)


def new_bulk_draft_id():
    return str(uuid.uuid4())


def _upsert_bulk_draft_list(session_key, draft):
    drafts = [d for d in st.session_state.get(session_key) or [] if d.get("id") != draft.get("id")]
    drafts.append(draft)
    st.session_state[session_key] = drafts


def queue_pending_club_draft(conn, draft):
    _upsert_bulk_draft_list("pending_club_drafts", draft)
    upsert_pending_bulk_draft(conn, DRAFT_TYPE_CLUB_BULK, draft)


def remove_pending_club_draft(conn, draft_id):
    st.session_state.pending_club_drafts = [
        d for d in st.session_state.get("pending_club_drafts") or [] if d.get("id") != draft_id
    ]
    delete_pending_bulk_draft(conn, DRAFT_TYPE_CLUB_BULK, draft_id)


def queue_pending_grade_contact_draft(conn, draft):
    _upsert_bulk_draft_list("pending_grade_contact_drafts", draft)
    upsert_pending_bulk_draft(conn, DRAFT_TYPE_GRADE_CONTACT_BULK, draft)


def remove_pending_grade_contact_draft(conn, draft_id):
    st.session_state.pending_grade_contact_drafts = [
        d
        for d in st.session_state.get("pending_grade_contact_drafts") or []
        if d.get("id") != draft_id
    ]
    delete_pending_bulk_draft(conn, DRAFT_TYPE_GRADE_CONTACT_BULK, draft_id)


def queue_pending_balance_draft(conn, draft):
    _upsert_bulk_draft_list("pending_balance_drafts", draft)
    upsert_pending_bulk_draft(conn, DRAFT_TYPE_BALANCE_BULK, draft)


def remove_pending_balance_draft(conn, draft_id):
    st.session_state.pending_balance_drafts = [
        d for d in st.session_state.get("pending_balance_drafts") or [] if d.get("id") != draft_id
    ]
    delete_pending_bulk_draft(conn, DRAFT_TYPE_BALANCE_BULK, draft_id)


def queue_pending_meal_draft(conn, draft):
    _upsert_bulk_draft_list("pending_meal_drafts", draft)
    upsert_pending_bulk_draft(conn, DRAFT_TYPE_MEAL_BULK, draft)


def remove_pending_meal_draft(conn, draft_id):
    st.session_state.pending_meal_drafts = [
        d for d in st.session_state.get("pending_meal_drafts") or [] if d.get("id") != draft_id
    ]
    delete_pending_bulk_draft(conn, DRAFT_TYPE_MEAL_BULK, draft_id)


def queue_pending_transport_draft(conn, draft):
    _upsert_bulk_draft_list("pending_transport_drafts", draft)
    upsert_pending_bulk_draft(conn, DRAFT_TYPE_TRANSPORT_BULK, draft)


def remove_pending_transport_draft(conn, draft_id):
    st.session_state.pending_transport_drafts = [
        d for d in st.session_state.get("pending_transport_drafts") or [] if d.get("id") != draft_id
    ]
    delete_pending_bulk_draft(conn, DRAFT_TYPE_TRANSPORT_BULK, draft_id)


def _dataframe_to_contact_rows(df):
    """Serialize grade bulk editor rows for Save-for-later drafts."""
    rows = []
    for _, r in df.iterrows():
        rows.append(
            {
                "id": int(r["id"]),
                "parent_name": str(r.get("parent_name") or "").strip(),
                "parent_phone": str(r.get("parent_phone") or "").strip(),
                "parent2_name": str(r.get("parent2_name") or "").strip(),
                "parent2_phone": str(r.get("parent2_phone") or "").strip(),
                "date_of_birth": str(r.get("date_of_birth") or "").strip(),
            }
        )
    return rows


def _contact_rows_to_dataframe(rows, base_df):
    """Rebuild editor dataframe from draft rows merged with base names/codes."""
    by_id = {int(r["id"]): r for r in rows}
    out = base_df.copy()
    for idx, row in out.iterrows():
        sid = int(row["id"])
        patch = by_id.get(sid)
        if not patch:
            continue
        for col in (
            "parent_name",
            "parent_phone",
            "parent2_name",
            "parent2_phone",
            "date_of_birth",
        ):
            if patch.get(col) is not None:
                out.at[idx, col] = patch[col]
    return out


def _dataframe_to_balance_rows(df):
    rows = []
    for _, r in df.iterrows():
        entry = parse_balance_editor_value(r.get("balance"))
        if entry is None:
            continue
        status, amt = entry
        rows.append(
            {
                "id": int(r["id"]),
                "balance_status": status,
                "balance": amt,
            }
        )
    return rows


def _balance_rows_to_dataframe(rows, base_df):
    by_id = {int(r["id"]): r for r in rows}
    out = base_df.copy()
    for idx, row in out.iterrows():
        sid = int(row["id"])
        patch = by_id.get(sid)
        if patch is None:
            continue
        st = patch.get("balance_status", BALANCE_STATUS_SET)
        if st == BALANCE_STATUS_NOT_SET:
            out.at[idx, "balance"] = BALANCE_DISPLAY_NOT_SET
        elif st == BALANCE_STATUS_CLEARED:
            out.at[idx, "balance"] = BALANCE_DISPLAY_CLEARED
        elif patch.get("balance") is not None:
            out.at[idx, "balance"] = f"{float(patch['balance']):,.0f}".replace(",", "")
    return out


def apply_pending_club_draft(conn, draft):
    kind = draft.get("kind")
    p = draft.get("payload") or {}
    if kind == "club_assign":
        return enroll_students_in_club(
            conn,
            int(p["club_id"]),
            [int(x) for x in p.get("student_ids") or []],
            mode=p.get("mode") or "add",
            resync_fees=True,
            do_commit=True,
        )
    if kind == "club_import":
        return import_club_roster_assignments(
            conn,
            p.get("rows") or [],
            mode=p.get("mode") or "add",
            dry_run=False,
            resync_fees=True,
        )
    raise ValueError(f"Unknown club draft kind: {kind!r}")


def apply_pending_grade_contact_draft(conn, draft):
    kind = draft.get("kind")
    p = draft.get("payload") or {}
    if kind == "grade_bulk":
        grade = p.get("grade")
        base = _grade_contact_bulk_dataframe(conn, grade)
        if base.empty:
            return {"updated": 0, "skipped": 0, "errors": []}
        edited = _contact_rows_to_dataframe(p.get("rows") or [], base)
        return persist_grade_bulk_contact_edits(conn, base, edited, resync_fees=False)
    if kind == "grade_import":
        return import_grade_roster_updates(
            conn, p.get("rows") or [], dry_run=False, resync_fees=False
        )
    raise ValueError(f"Unknown grade contact draft kind: {kind!r}")


def apply_pending_balance_draft(conn, draft):
    kind = draft.get("kind")
    p = draft.get("payload") or {}
    if kind == "balance_bulk":
        grade = p.get("grade")
        base = _balance_bulk_dataframe(conn, grade)
        if base.empty:
            return {"updated": 0, "skipped": 0, "errors": []}
        edited = _balance_rows_to_dataframe(p.get("rows") or [], base)
        return persist_balance_bulk_edits(conn, base, edited)
    if kind == "balance_import":
        return import_balance_roster_updates(conn, p.get("rows") or [], dry_run=False)
    raise ValueError(f"Unknown balance draft kind: {kind!r}")


def apply_pending_meal_draft(conn, draft):
    if draft.get("kind") != "meal_bulk":
        raise ValueError(f"Unknown meal draft kind: {draft.get('kind')!r}")
    p = draft.get("payload") or {}
    grade = p.get("grade")
    base = _meal_program_bulk_dataframe(conn, grade)
    if base.empty:
        return {"updated": 0, "skipped": 0, "errors": []}
    edited = _meal_rows_to_dataframe(p.get("rows") or [], base)
    return persist_meal_program_bulk_edits(conn, base, edited)


def apply_pending_transport_draft(conn, draft):
    if draft.get("kind") != "transport_bulk":
        raise ValueError(f"Unknown transport draft kind: {draft.get('kind')!r}")
    p = draft.get("payload") or {}
    grade = p.get("grade")
    base = _transport_users_bulk_dataframe(conn, grade)
    if base.empty:
        return {"updated": 0, "skipped": 0, "errors": []}
    routes = conn.execute(
        "SELECT id FROM fee_structure WHERE fee_category='transport'"
    ).fetchall()
    valid = {"__none__"} | {str(int(r[0])) for r in routes}
    rows = []
    for r in p.get("rows") or []:
        try:
            sid = int(r["id"])
        except (TypeError, ValueError):
            continue
        tc = str(r.get("transport_choice") or "__none__").strip()
        if tc not in valid:
            tc = "__none__"
        rows.append({"id": sid, "transport_choice": tc})
    edited = _transport_rows_to_dataframe(rows, base)
    return persist_transport_users_bulk_edits(conn, base, edited)


def _pending_draft_student_ids(draft):
    """Collect student id integers from a Manage Students pending-review draft payload."""
    p = draft.get("payload") or {}
    kind = draft.get("kind")
    if kind == "club_assign":
        out = []
        for x in p.get("student_ids") or []:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out
    rows = p.get("rows") or []
    out = []
    seen = set()
    for r in rows:
        if not isinstance(r, dict) or "id" not in r:
            continue
        try:
            sid = int(r["id"])
        except (TypeError, ValueError):
            continue
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _pending_review_student_names_bits(conn, student_ids, *, max_show=10, max_query=400):
    """Return (plain_for_search, html_fragment) for student display names."""
    uniq = []
    seen = set()
    for x in student_ids or []:
        try:
            ix = int(x)
        except (TypeError, ValueError):
            continue
        if ix not in seen:
            seen.add(ix)
            uniq.append(ix)
        if len(uniq) >= max_query:
            break
    if not uniq:
        return "", ""
    qh = ",".join("?" * len(uniq))
    rows = conn.execute(
        f"SELECT id, name FROM students WHERE id IN ({qh}) ORDER BY name",
        tuple(uniq),
    ).fetchall()
    id_to_name = {}
    for r in rows:
        sid = int(r[0])
        nm = str(r[1]).strip() if r[1] is not None else ""
        id_to_name[sid] = nm or f"id {sid}"
    names_in_order = [id_to_name.get(i, f"id {i}") for i in uniq]
    plain = " ".join(names_in_order)
    shown = names_in_order[:max_show]
    esc = " · ".join(html_module.escape(n) for n in shown)
    more = len(names_in_order) - max_show
    if more > 0:
        esc += f" · … (+{more} more)"
    return plain, esc


def format_club_draft_summary_html(conn, draft):
    kind = draft.get("kind")
    p = draft.get("payload") or {}
    label = html_module.escape(draft.get("label") or "Club changes")
    _ids = _pending_draft_student_ids(draft)
    _, names_esc = _pending_review_student_names_bits(conn, _ids)
    names_li = f'<li><strong>Students:</strong> {names_esc}</li>' if names_esc else ""
    if kind == "club_assign":
        n = len(p.get("student_ids") or [])
        mode = "Add to club" if p.get("mode") == "add" else "Set club roster"
        return (
            f"<p><strong>{label}</strong></p>"
            f"<ul><li>{html_module.escape(mode)}</li>"
            f"<li><strong>{n}</strong> student(s) selected</li>"
            f"{names_li}</ul>"
        )
    n = len(p.get("rows") or [])
    mode = p.get("mode") or "add"
    return (
        f"<p><strong>{label}</strong></p>"
        f"<ul><li>Import <strong>{n}</strong> club membership row(s)</li>"
        f"<li>Mode: {html_module.escape(mode)}</li>"
        f"{names_li}</ul>"
    )


def format_grade_contact_draft_summary_html(draft):
    p = draft.get("payload") or {}
    label = html_module.escape(draft.get("label") or "Grade contact changes")
    if draft.get("kind") == "grade_bulk":
        return (
            f"<p><strong>{label}</strong></p>"
            f"<ul><li>Bulk edit for <strong>{html_module.escape(str(p.get('grade')))}</strong></li>"
            f"<li><strong>{len(p.get('rows') or [])}</strong> student row(s) in draft</li></ul>"
        )
    return (
        f"<p><strong>{label}</strong></p>"
        f"<ul><li>Import <strong>{len(p.get('rows') or [])}</strong> contact row(s)</li></ul>"
    )


def format_balance_draft_summary_html(draft):
    p = draft.get("payload") or {}
    label = html_module.escape(draft.get("label") or "Balance changes")
    if draft.get("kind") == "balance_bulk":
        return (
            f"<p><strong>{label}</strong></p>"
            f"<ul><li>Bulk balances for <strong>{html_module.escape(str(p.get('grade')))}</strong></li>"
            f"<li><strong>{len(p.get('rows') or [])}</strong> student row(s) in draft</li></ul>"
        )
    return (
        f"<p><strong>{label}</strong></p>"
        f"<ul><li>Import <strong>{len(p.get('rows') or [])}</strong> balance row(s)</li></ul>"
    )


def format_meal_draft_summary_html(conn, draft):
    p = draft.get("payload") or {}
    label = html_module.escape(draft.get("label") or "Meals program changes")
    _ids = _pending_draft_student_ids(draft)
    _, names_esc = _pending_review_student_names_bits(conn, _ids)
    names_li = f'<li><strong>Students:</strong> {names_esc}</li>' if names_esc else ""
    return (
        f"<p><strong>{label}</strong></p>"
        f"<ul><li>Bulk meals for <strong>{html_module.escape(str(p.get('grade')))}</strong></li>"
        f"<li><strong>{len(p.get('rows') or [])}</strong> student row(s) in draft</li>"
        f"{names_li}</ul>"
    )


def format_transport_draft_summary_html(conn, draft):
    p = draft.get("payload") or {}
    label = html_module.escape(draft.get("label") or "Transport changes")
    _ids = _pending_draft_student_ids(draft)
    _, names_esc = _pending_review_student_names_bits(conn, _ids)
    names_li = f'<li><strong>Students:</strong> {names_esc}</li>' if names_esc else ""
    return (
        f"<p><strong>{label}</strong></p>"
        f"<ul><li>Bulk transport for <strong>{html_module.escape(str(p.get('grade')))}</strong></li>"
        f"<li><strong>{len(p.get('rows') or [])}</strong> student row(s) in draft</li>"
        f"{names_li}</ul>"
    )


def format_pending_review_count(n):
    """Exact count on tab badges up to 10; show 10+ when there are more than 10."""
    n = int(n or 0)
    if n <= 0:
        return ""
    return "10+" if n > 10 else str(n)


def pending_review_tab_label(count=0):
    """Tab title for the pending-review queue."""
    base = "Pending Reviews"
    c = int(count or 0)
    if c > 0:
        return f"{base} ({format_pending_review_count(c)})"
    return base


def render_sidebar_nav_pending_badge(count):
    """Red count circle on the right of the sidebar button — call immediately after that button."""
    c = int(count or 0)
    if c <= 0:
        return
    label = format_pending_review_count(c)
    st.sidebar.markdown(
        f"""
        <div class="nav-pending-float nav-pending-float--after"
             title="{c} pending review{'s' if c != 1 else ''}">
            <span class="nav-pending-float__badge">{label}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_sidebar_pending_notification():
    """How many menu areas have pending work — badge shows that tab count (not item totals)."""
    tabs = pending_reviews_notification_surface_count()
    if not tabs:
        return
    label = str(tabs)
    tab_word = "tab" if tabs == 1 else "tabs"
    st.sidebar.markdown(
        f"""
        <div class="nav-pending-float nav-pending-float--header"
             title="{tabs} menu area{'s' if tabs != 1 else ''} with pending reviews — password required to apply">
            <span class="nav-pending-float__badge nav-pending-float__badge--header">{label}</span>
        </div>
        <p class="yt-bell-widget__hint">Notifications from <strong>{tabs}</strong> {tab_word}</p>
        """,
        unsafe_allow_html=True,
    )


def _pending_flow_key(draft_key):
    return f"pending_flow_{draft_key}"


def render_pending_apply_workflow(draft_key, summary_html, on_apply, on_discard, *, confirm_prompt=None):
    """
    Two-step apply: confirm intent, then admin password.
    on_apply(conn) is called after password OK; on_discard() removes the draft (admin password required).
    """
    flow = st.session_state.get(_pending_flow_key(draft_key))
    prompt = confirm_prompt or "Are you sure you want to apply these changes to the database?"

    st.markdown(summary_html, unsafe_allow_html=True)

    if flow == "discard_pw":
        st.warning(
            "This draft will be removed from the queue permanently. Enter the **admin password** to confirm discard."
        )
        dpw = st.text_input(
            "Admin password",
            type="password",
            key=f"pending_discard_pw_{draft_key}",
            placeholder="Admin password to discard",
        )
        dc1, dc2 = st.columns(2)
        with dc1:
            if st.button("Confirm discard", type="primary", key=f"pending_discard_ok_{draft_key}"):
                err = evaluate_admin_password_input(dpw)
                if err is not None:
                    _invalidate_admin_password_fields(err, f"pending_discard_pw_{draft_key}")
                else:
                    on_discard()
                    _clear_password_field_keys(f"pending_discard_pw_{draft_key}")
                    st.session_state.pop(_pending_flow_key(draft_key), None)
                    st.rerun()
        with dc2:
            if st.button("Cancel", key=f"pending_discard_cancel_{draft_key}"):
                _clear_password_field_keys(f"pending_discard_pw_{draft_key}")
                st.session_state.pop(_pending_flow_key(draft_key), None)
                st.rerun()
        return

    if flow == "password":
        st.warning(prompt)
        apw = st.text_input(
            "Admin password",
            type="password",
            key=f"pending_pw_{draft_key}",
            placeholder="Enter admin password to apply",
        )
        pc1, pc2, pc3 = st.columns(3)
        with pc1:
            if st.button("Confirm and apply", type="primary", key=f"pending_ok_{draft_key}"):
                err = evaluate_admin_password_input(apw)
                if err is not None:
                    _invalidate_admin_password_fields(
                        err,
                        f"pending_pw_{draft_key}",
                    )
                else:
                    on_apply()
                    _clear_password_field_keys(f"pending_pw_{draft_key}")
                    st.session_state.pop(_pending_flow_key(draft_key), None)
                    st.rerun()
        with pc2:
            if st.button("Back", key=f"pending_back_{draft_key}"):
                st.session_state[_pending_flow_key(draft_key)] = "confirm"
                st.rerun()
        with pc3:
            if st.button("Discard draft", key=f"pending_disc_{draft_key}"):
                st.session_state[_pending_flow_key(draft_key)] = "discard_pw"
                st.rerun()
        return

    if flow == "confirm":
        st.warning(prompt)
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Yes, apply changes", type="primary", key=f"pending_yes_{draft_key}"):
                st.session_state[_pending_flow_key(draft_key)] = "password"
                st.rerun()
        with c2:
            if st.button("No, go back", key=f"pending_no_{draft_key}"):
                st.session_state.pop(_pending_flow_key(draft_key), None)
                st.rerun()
        with c3:
            if st.button("Discard draft", key=f"pending_disc2_{draft_key}"):
                st.session_state[_pending_flow_key(draft_key)] = "discard_pw"
                st.rerun()
        return

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Review & apply", type="primary", key=f"pending_start_{draft_key}"):
            st.session_state[_pending_flow_key(draft_key)] = "confirm"
            st.rerun()
    with c2:
        if st.button("Discard draft", key=f"pending_start_disc_{draft_key}"):
            st.session_state[_pending_flow_key(draft_key)] = "discard_pw"
            st.rerun()


def format_student_edit_draft_summary(conn, student_id, payload):
    """HTML summary of proposed student changes vs database."""
    rec = get_student_record(conn, student_id) or {}
    cc_map = {
        int(r[0]): r[1]
        for r in conn.execute(
            "SELECT id, fee_name FROM fee_structure WHERE fee_category='co_curricular'"
        ).fetchall()
    }
    route_map = {
        int(r[0]): r[1]
        for r in conn.execute(
            "SELECT id, fee_name FROM fee_structure WHERE fee_category='transport'"
        ).fetchall()
    }

    def _cc_label(ids):
        if not ids:
            return "—"
        return ", ".join(cc_map.get(int(i), str(i)) for i in ids)

    def _line(label, old, new):
        if str(old or "—") == str(new or "—"):
            return ""
        return f"<li><strong>{label}:</strong> {html_module.escape(str(old or '—'))} → {html_module.escape(str(new or '—'))}</li>"

    old_cc = parse_co_curricular_ids(rec.get("co_curricular_activities"), conn=conn, student_id=student_id)
    new_cc = payload.get("co_curricular_ids") or []
    old_tid = rec.get("transport_route_id") if int(rec.get("has_transport") or 0) else None
    new_tid = payload.get("selected_transport_id") if payload.get("has_transport") else None

    lines = [
        _line("Name", rec.get("name"), payload.get("name")),
        _line("Grade", rec.get("grade"), payload.get("grade")),
        _line("Parent/Guardian 1 name", rec.get("parent_name"), payload.get("parent_name")),
        _line(
            "Parent/Guardian 1 phone",
            rec.get("parent_phone"),
            normalize_kenya_msisdn(payload.get("parent_phone") or "") or payload.get("parent_phone"),
        ),
        _line("Parent/Guardian 2 name", rec.get("parent2_name"), payload.get("parent2_name")),
        _line(
            "Parent/Guardian 2 phone",
            rec.get("parent2_phone"),
            normalize_kenya_msisdn(payload.get("parent2_phone") or "") or payload.get("parent2_phone"),
        ),
        _line("Date of birth", rec.get("date_of_birth"), payload.get("date_of_birth")),
        _line("Transport", route_map.get(int(old_tid), "No transport") if old_tid else "No transport", route_map.get(int(new_tid), "No transport") if new_tid else "No transport"),
        _line("Meals", "Yes" if rec.get("has_meal") else "No", "Yes" if payload.get("has_meal") else "No"),
        _line("Admission fee (one-time)", "Yes" if int(rec.get("include_admission_fees") or 0) else "No", "Yes" if int(payload.get("include_admission_fees") or 0) else "No"),
        _line("Interview fee (one-time)", "Yes" if int(rec.get("include_interview_fee") or 0) else "No", "Yes" if int(payload.get("include_interview_fee") or 0) else "No"),
        _line(
            "Sponsored",
            "Yes" if student_is_sponsored(rec) else "No",
            "Yes" if int(payload.get("is_sponsored") or 0) else "No",
        ),
        _line("Clubs", _cc_label(old_cc), _cc_label(new_cc)),
        _line(
            "Balance",
            format_student_balance_display(student_row=rec),
            format_student_balance_display(
                balance=payload.get("balance"),
                balance_status=payload.get("balance_status"),
            ),
        ),
    ]
    lines = [x for x in lines if x]
    if not lines:
        return "<p>No differences from the current database record.</p>"
    return "<ul style='margin: 0.5rem 0 1rem 1.25rem;'>" + "".join(lines) + "</ul>"


def _apply_all_pending_manage_student_reviews(conn):
    """
    Apply every Manage Students pending draft in a fixed order (same as the tab).
    Returns (applied_count, error_messages). Does not check admin password — caller must.
    """
    errors = []
    applied = 0

    for _sid, _pl in list((st.session_state.get("pending_student_transfers") or {}).items()):
        try:
            _rec = get_student_record(conn, int(_sid))
            _sn = (_rec or {}).get("name") or _pl.get("name") or f"Student id {_sid}"
            if not student_record_is_editable(_rec or {}):
                raise ValueError("This student is no longer Active.")
            schedule_student_transfer(conn, int(_sid), _pl.get("transfer_reason"))
            remove_pending_student_transfer(conn, int(_sid))
            _rc = (get_student_record(conn, int(_sid)) or {}).get("student_code")
            _s = "" if _rc is None else str(_rc).strip()
            _code = display_student_code(_s) if _s else str(_sid)
            _audit_log(
                conn,
                "Manage Student",
                f"Manage Student, {_code} ({_sn}): marked as transferred (from pending review, confirm all).",
                save_mode="approved_from_pending",
                entity_type="student",
                entity_id=int(_sid),
                entity_code=str(_code),
            )
            applied += 1
        except Exception as ex:
            errors.append(f"Transfer (student {_sid}): {ex}")

    for _sid, _pl in list((st.session_state.get("pending_student_deletions") or {}).items()):
        try:
            _rec = get_student_record(conn, int(_sid))
            _sn = (_rec or {}).get("name") or _pl.get("name") or f"Student id {_sid}"
            if not student_record_is_editable(_rec or {}):
                raise ValueError("This student is no longer Active.")
            schedule_student_deletion(conn, int(_sid), _pl.get("deletion_reason"))
            remove_pending_student_deletion(conn, int(_sid))
            _rc = (get_student_record(conn, int(_sid)) or {}).get("student_code")
            _s = "" if _rc is None else str(_rc).strip()
            _code = display_student_code(_s) if _s else str(_sid)
            _audit_log(
                conn,
                "Manage Student",
                f"Manage Student, {_code} ({_sn}): scheduled for deletion (from pending review, confirm all). "
                f"Reason: {_pl.get('deletion_reason')}",
                save_mode="approved_from_pending",
                entity_type="student",
                entity_id=int(_sid),
                entity_code=str(_code),
            )
            applied += 1
        except Exception as ex:
            errors.append(f"Deletion (student {_sid}): {ex}")

    for _d in list(st.session_state.get("pending_club_drafts") or []):
        try:
            apply_pending_club_draft(conn, _d)
            remove_pending_club_draft(conn, _d["id"])
            applied += 1
        except Exception as ex:
            errors.append(f"Club draft “{_d.get('label', _d.get('id'))}”: {ex}")

    for _d in list(st.session_state.get("pending_grade_contact_drafts") or []):
        try:
            apply_pending_grade_contact_draft(conn, _d)
            remove_pending_grade_contact_draft(conn, _d["id"])
            applied += 1
        except Exception as ex:
            errors.append(f"Grade contact draft “{_d.get('label', _d.get('id'))}”: {ex}")

    for _d in list(st.session_state.get("pending_balance_drafts") or []):
        try:
            apply_pending_balance_draft(conn, _d)
            remove_pending_balance_draft(conn, _d["id"])
            applied += 1
        except Exception as ex:
            errors.append(f"Balance draft “{_d.get('label', _d.get('id'))}”: {ex}")

    for _d in list(st.session_state.get("pending_meal_drafts") or []):
        try:
            apply_pending_meal_draft(conn, _d)
            remove_pending_meal_draft(conn, _d["id"])
            applied += 1
        except Exception as ex:
            errors.append(f"Meals draft “{_d.get('label', _d.get('id'))}”: {ex}")

    for _d in list(st.session_state.get("pending_transport_drafts") or []):
        try:
            apply_pending_transport_draft(conn, _d)
            remove_pending_transport_draft(conn, _d["id"])
            applied += 1
        except Exception as ex:
            errors.append(f"Transport draft “{_d.get('label', _d.get('id'))}”: {ex}")

    for _sid, _pl in list((st.session_state.get("pending_student_edits") or {}).items()):
        _sn = _pl.get("name", f"Student id {_sid}")
        _rec = get_student_record(conn, int(_sid))
        if _rec is not None and not student_record_is_editable(_rec):
            errors.append(
                f"Student edit ({_sn}): learner is **{student_status_label(_rec)}** — skipped (discard manually)."
            )
            continue
        try:
            if not student_record_is_editable(get_student_record(conn, int(_sid)) or {}):
                raise ValueError("This student record is no longer Active and cannot be updated.")
            _merged = merge_student_edit_payload(conn, int(_sid), _pl)
            persist_student_edit(conn, int(_sid), _merged)
            _ok, _errs = verify_student_edit_saved(conn, int(_sid), _merged)
            remove_pending_student_edit(conn, int(_sid))
            clear_student_edit_widget_state(int(_sid))
            _rec2 = get_student_record(conn, int(_sid))
            _rc = (_rec2 or {}).get("student_code")
            _s = "" if _rc is None else str(_rc).strip()
            _code = display_student_code(_s) if _s else str(_sid)
            _qb = _pl.get("queued_by_gate_user")
            _audit_log(
                conn,
                "Manage Student",
                f"Manage Student, {_code} ({_merged.get('name', _sn)}): applied pending changes (confirm all)."
                + (f" Draft had been saved for later by {_qb}." if _qb else ""),
                save_mode="approved_from_pending",
                detail=_audit_student_payload_detail(_merged),
                entity_type="student",
                entity_id=int(_sid),
                entity_code=str(_code),
            )
            applied += 1
            if not _ok:
                errors.append(f"Student edit ({_sn}): verification — " + "; ".join(_errs))
        except Exception as ex:
            errors.append(f"Student edit ({_sn}): {ex}")

    if applied:
        invalidate_student_cache()
    return applied, errors


def _apply_all_pending_manual_payments(conn):
    """Apply every pending manual payment draft. Returns (applied_count, error_messages)."""
    errors = []
    applied = 0
    for _d in list(st.session_state.get("pending_manual_payment_drafts") or []):
        _did = _d["id"]
        try:
            res_pay = _insert_manual_payment_row(
                conn,
                student_id=_d["student_id"],
                amount=_d["amount"],
                payment_date_iso=_d["payment_date"],
                payment_method=_d["payment_method"],
                purpose=_d["purpose"],
                transaction_id=_d.get("transaction_id") or "",
                description_notes=_d.get("description") or "",
            )
            apply_optional_other_payer_for_payment(
                conn,
                int(_d["student_id"]),
                res_pay["payment_id"],
                _d.get("other_payer_name"),
                _d.get("other_payer_phone"),
            )
            remove_pending_manual_payment(conn, _did)
            qb = (_d.get("queued_by_gate_user") or "unknown")
            _audit_log(
                conn,
                "Payment",
                f"Approved pending payment {res_pay['internal_payment_id']} (KSH {float(_d['amount']):,.0f}) "
                f"for student id {_d['student_id']} (confirm all). Draft had been saved for later by {qb}.",
                save_mode="approved_from_pending",
                internal_payment_id=res_pay["internal_payment_id"],
                detail=json.dumps(
                    {"draft_id": str(_did), "queued_by": qb, "payment_row_id": res_pay["payment_id"]},
                    default=str,
                ),
                entity_type="student",
                entity_id=int(_d["student_id"]),
            )
            applied += 1
        except Exception as ex:
            errors.append(f"Payment draft {_did}: {ex}")
    if applied:
        invalidate_student_cache()
    return applied, errors


def _apply_all_pending_expenses(conn):
    """Apply every pending expense draft. Returns (applied_count, error_messages)."""
    errors = []
    applied = 0
    for _d in list(st.session_state.get("pending_expense_drafts") or []):
        _did = _d["id"]
        try:
            eid = _insert_expense_row(
                conn,
                category=_d.get("category"),
                custom_label=_d.get("custom_label"),
                amount=_d["amount"],
                expense_date_str=_d["expense_date"],
                description=_d["description"],
                payment_method=_d["payment_method"],
                vendor=_d.get("vendor") or "",
                receipt_number=_d.get("receipt_number") or "",
            )
            remove_pending_expense(conn, _did)
            qb = (_d.get("queued_by_gate_user") or "unknown")
            _audit_log(
                conn,
                "Expense",
                f"Approved pending expense row {eid} (KSH {float(_d['amount']):,.0f}) (confirm all). "
                f"Draft had been saved for later by {qb}.",
                save_mode="approved_from_pending",
                entity_type="expense",
                entity_id=int(eid),
                detail=json.dumps({"draft_id": str(_did), "queued_by": qb}, default=str),
            )
            applied += 1
        except Exception as ex:
            errors.append(f"Expense draft {_did}: {ex}")
    return applied, errors


PENDING_CB_MS_PREFIX = "pending_cb_ms_"
PENDING_CB_PAY_PREFIX = "pending_cb_pay_"
PENDING_CB_EXP_PREFIX = "pending_cb_exp_"

_PENDING_MS_ACTION_OPTIONS = (
    "All actions",
    "Transfers",
    "Deletions",
    "Clubs",
    "Grade & contact",
    "Balance",
    "Meals",
    "Transport",
    "Student edits",
)

_MS_APPLY_ORDER = {
    "transfer": 0,
    "deletion": 1,
    "club": 2,
    "grade": 3,
    "balance": 4,
    "meal": 5,
    "transport": 6,
    "student": 7,
}


def _pending_ms_club_assign_matches_grade(conn, payload, selected_grade):
    if not selected_grade or selected_grade == "All grades":
        return True
    ids = []
    for x in (payload or {}).get("student_ids") or []:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            pass
    if not ids:
        return False
    qmarks = ",".join("?" * len(ids))
    row = conn.execute(
        f"SELECT 1 FROM students WHERE id IN ({qmarks}) AND TRIM(COALESCE(grade,'')) = TRIM(?) LIMIT 1",
        tuple(ids) + (selected_grade,),
    ).fetchone()
    return row is not None


def _pending_ms_bulk_matches_grade(conn, draft, grade_sel):
    if not grade_sel or grade_sel == "All grades":
        return True
    kind = draft.get("kind")
    p = draft.get("payload") or {}
    if kind == "club_assign":
        return _pending_ms_club_assign_matches_grade(conn, p, grade_sel)
    if kind in ("grade_bulk", "balance_bulk", "meal_bulk", "transport_bulk"):
        g = str(p.get("grade") or "").strip()
        return bool(g) and g == str(grade_sel).strip()
    return False


def _pending_ms_student_grade_matches(rec, grade_sel):
    if not grade_sel or grade_sel == "All grades":
        return True
    g = str((rec or {}).get("grade") or "").strip()
    return g == str(grade_sel).strip()


def _parse_pending_ms_checkbox_suffix(suf):
    for kind in (
        "transfer",
        "deletion",
        "club",
        "grade",
        "balance",
        "meal",
        "transport",
        "student",
    ):
        pre = f"{kind}_"
        if suf.startswith(pre):
            return kind, suf[len(pre) :]
    return None, None


def _apply_selected_manage_student_reviews(conn, suffixes):
    errors = []
    applied = 0
    parsed = []
    for suf in suffixes:
        kind, rest = _parse_pending_ms_checkbox_suffix(suf)
        if not kind:
            errors.append(f"Unknown selection: {suf}")
            continue
        parsed.append((kind, rest))
    parsed.sort(key=lambda t: (_MS_APPLY_ORDER.get(t[0], 99), str(t[1])))

    for kind, rest in parsed:
        try:
            if kind == "transfer":
                sid = int(rest)
                pl = (st.session_state.get("pending_student_transfers") or {}).get(sid)
                if not pl:
                    continue
                rec = get_student_record(conn, sid)
                sn = (rec or {}).get("name") or pl.get("name") or f"Student id {sid}"
                if not student_record_is_editable(rec or {}):
                    raise ValueError("This student is no longer Active.")
                schedule_student_transfer(conn, sid, pl.get("transfer_reason"))
                remove_pending_student_transfer(conn, sid)
                rc = (get_student_record(conn, sid) or {}).get("student_code")
                s = "" if rc is None else str(rc).strip()
                code = display_student_code(s) if s else str(sid)
                _audit_log(
                    conn,
                    "Manage Student",
                    f"Manage Student, {code} ({sn}): marked as transferred (pending review, confirm selected).",
                    save_mode="approved_from_pending",
                    entity_type="student",
                    entity_id=int(sid),
                    entity_code=str(code),
                )
                applied += 1
            elif kind == "deletion":
                sid = int(rest)
                pl = (st.session_state.get("pending_student_deletions") or {}).get(sid)
                if not pl:
                    continue
                rec = get_student_record(conn, sid)
                sn = (rec or {}).get("name") or pl.get("name") or f"Student id {sid}"
                if not student_record_is_editable(rec or {}):
                    raise ValueError("This student is no longer Active.")
                schedule_student_deletion(conn, sid, pl.get("deletion_reason"))
                remove_pending_student_deletion(conn, sid)
                rc = (get_student_record(conn, sid) or {}).get("student_code")
                s = "" if rc is None else str(rc).strip()
                code = display_student_code(s) if s else str(sid)
                _audit_log(
                    conn,
                    "Manage Student",
                    f"Manage Student, {code} ({sn}): scheduled for deletion (pending review, confirm selected). "
                    f"Reason: {pl.get('deletion_reason')}",
                    save_mode="approved_from_pending",
                    entity_type="student",
                    entity_id=int(sid),
                    entity_code=str(code),
                )
                applied += 1
            elif kind == "club":
                did = str(rest)
                by_id = {str(d.get("id")): d for d in (st.session_state.get("pending_club_drafts") or [])}
                draf = by_id.get(did)
                if not draf:
                    continue
                apply_pending_club_draft(conn, draf)
                remove_pending_club_draft(conn, draf["id"])
                applied += 1
            elif kind == "grade":
                did = str(rest)
                by_id = {str(d.get("id")): d for d in (st.session_state.get("pending_grade_contact_drafts") or [])}
                draf = by_id.get(did)
                if not draf:
                    continue
                apply_pending_grade_contact_draft(conn, draf)
                remove_pending_grade_contact_draft(conn, draf["id"])
                applied += 1
            elif kind == "balance":
                did = str(rest)
                by_id = {str(d.get("id")): d for d in (st.session_state.get("pending_balance_drafts") or [])}
                draf = by_id.get(did)
                if not draf:
                    continue
                apply_pending_balance_draft(conn, draf)
                remove_pending_balance_draft(conn, draf["id"])
                applied += 1
            elif kind == "meal":
                did = str(rest)
                by_id = {str(d.get("id")): d for d in (st.session_state.get("pending_meal_drafts") or [])}
                draf = by_id.get(did)
                if not draf:
                    continue
                apply_pending_meal_draft(conn, draf)
                remove_pending_meal_draft(conn, draf["id"])
                applied += 1
            elif kind == "transport":
                did = str(rest)
                by_id = {str(d.get("id")): d for d in (st.session_state.get("pending_transport_drafts") or [])}
                draf = by_id.get(did)
                if not draf:
                    continue
                apply_pending_transport_draft(conn, draf)
                remove_pending_transport_draft(conn, draf["id"])
                applied += 1
            elif kind == "student":
                sid = int(rest)
                pl = (st.session_state.get("pending_student_edits") or {}).get(sid)
                if not pl:
                    continue
                sn = pl.get("name", f"Student id {sid}")
                rec = get_student_record(conn, sid)
                if rec is not None and not student_record_is_editable(rec):
                    errors.append(
                        f"Student edit ({sn}): learner is {student_status_label(rec)} — skipped."
                    )
                    continue
                if not student_record_is_editable(get_student_record(conn, sid) or {}):
                    raise ValueError("This student record is no longer Active and cannot be updated.")
                merged = merge_student_edit_payload(conn, sid, pl)
                persist_student_edit(conn, sid, merged)
                ok, errs = verify_student_edit_saved(conn, sid, merged)
                remove_pending_student_edit(conn, sid)
                clear_student_edit_widget_state(int(sid))
                rec2 = get_student_record(conn, sid)
                rc = (rec2 or {}).get("student_code")
                s = "" if rc is None else str(rc).strip()
                code = display_student_code(s) if s else str(sid)
                qb = pl.get("queued_by_gate_user")
                _audit_log(
                    conn,
                    "Manage Student",
                    f"Manage Student, {code} ({merged.get('name', sn)}): applied pending changes (confirm selected)."
                    + (f" Draft had been saved for later by {qb}." if qb else ""),
                    save_mode="approved_from_pending",
                    detail=_audit_student_payload_detail(merged),
                    entity_type="student",
                    entity_id=int(sid),
                    entity_code=str(code),
                )
                applied += 1
                if not ok:
                    errors.append(f"Student edit ({sn}): verification — " + "; ".join(errs))
        except Exception as ex:
            errors.append(f"{kind} ({rest}): {ex}")

    if applied:
        invalidate_student_cache()
    return applied, errors


def _gather_pending_checkbox_keys(prefix):
    out = []
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith(prefix) and st.session_state.get(k):
            out.append(k[len(prefix) :])
    return out


def _apply_selected_pending_manual_payments(conn, draft_ids):
    errors = []
    applied = 0
    id_set = {str(x) for x in draft_ids}
    for _d in list(st.session_state.get("pending_manual_payment_drafts") or []):
        if str(_d["id"]) not in id_set:
            continue
        _did = _d["id"]
        try:
            res_pay = _insert_manual_payment_row(
                conn,
                student_id=_d["student_id"],
                amount=_d["amount"],
                payment_date_iso=_d["payment_date"],
                payment_method=_d["payment_method"],
                purpose=_d["purpose"],
                transaction_id=_d.get("transaction_id") or "",
                description_notes=_d.get("description") or "",
            )
            apply_optional_other_payer_for_payment(
                conn,
                int(_d["student_id"]),
                res_pay["payment_id"],
                _d.get("other_payer_name"),
                _d.get("other_payer_phone"),
            )
            remove_pending_manual_payment(conn, _did)
            qb = (_d.get("queued_by_gate_user") or "unknown")
            _audit_log(
                conn,
                "Payment",
                f"Approved pending payment {res_pay['internal_payment_id']} (KSH {float(_d['amount']):,.0f}) "
                f"for student id {_d['student_id']} (confirm selected). Draft had been saved for later by {qb}.",
                save_mode="approved_from_pending",
                internal_payment_id=res_pay["internal_payment_id"],
                detail=json.dumps(
                    {"draft_id": str(_did), "queued_by": qb, "payment_row_id": res_pay["payment_id"]},
                    default=str,
                ),
                entity_type="student",
                entity_id=int(_d["student_id"]),
            )
            applied += 1
        except Exception as ex:
            errors.append(f"Payment draft {_did}: {ex}")
    if applied:
        invalidate_student_cache()
    return applied, errors


def _apply_selected_pending_expenses(conn, draft_ids):
    errors = []
    applied = 0
    id_set = {str(x) for x in draft_ids}
    for _d in list(st.session_state.get("pending_expense_drafts") or []):
        if str(_d["id"]) not in id_set:
            continue
        _did = _d["id"]
        try:
            eid = _insert_expense_row(
                conn,
                category=_d.get("category"),
                custom_label=_d.get("custom_label"),
                amount=_d["amount"],
                expense_date_str=_d["expense_date"],
                description=_d["description"],
                payment_method=_d["payment_method"],
                vendor=_d.get("vendor") or "",
                receipt_number=_d.get("receipt_number") or "",
            )
            remove_pending_expense(conn, _did)
            qb = (_d.get("queued_by_gate_user") or "unknown")
            _audit_log(
                conn,
                "Expense",
                f"Approved pending expense row {eid} (KSH {float(_d['amount']):,.0f}) (confirm selected). "
                f"Draft had been saved for later by {qb}.",
                save_mode="approved_from_pending",
                entity_type="expense",
                entity_id=int(eid),
                detail=json.dumps({"draft_id": str(_did), "queued_by": qb}, default=str),
            )
            applied += 1
        except Exception as ex:
            errors.append(f"Expense draft {_did}: {ex}")
    return applied, errors


def _render_pending_reviews_tab(conn):
    """Student edits, transfers, deletions, plus club, grade, balance, meal, transport bulk drafts."""
    _pend_all = st.session_state.get("pending_student_edits") or {}
    _xfer_all = st.session_state.get("pending_student_transfers") or {}
    _del_all = st.session_state.get("pending_student_deletions") or {}
    _club_drafts = list(st.session_state.get("pending_club_drafts") or [])
    _grade_drafts = list(st.session_state.get("pending_grade_contact_drafts") or [])
    _bal_drafts = list(st.session_state.get("pending_balance_drafts") or [])
    _meal_drafts = list(st.session_state.get("pending_meal_drafts") or [])
    _transport_drafts = list(st.session_state.get("pending_transport_drafts") or [])
    _total = (
        len(_pend_all)
        + len(_xfer_all)
        + len(_del_all)
        + len(_club_drafts)
        + len(_grade_drafts)
        + len(_bal_drafts)
        + len(_meal_drafts)
        + len(_transport_drafts)
    )

    if _total == 0:
        st.info(
            "No pending reviews. Use **Save for later** on **Find & edit students** (including "
            "**Mark as Transferred** / **Delete Student Record**), **Manage clubs**, **Manage grade**, "
            "**Manage balance**, **Manage meal program**, **Manage transport**, or related bulk tabs "
            "to queue work here."
        )
        return

    st.caption(
        f"**{_total}** draft(s) waiting. Review each item, confirm, then enter the admin password to apply."
    )
    _g_rows = conn.execute(
        "SELECT DISTINCT TRIM(grade) AS g FROM students WHERE grade IS NOT NULL AND TRIM(grade) != ''"
    ).fetchall()
    _uniq_gr = sorted(
        {str(r[0]) for r in _g_rows if r and r[0]},
        key=lambda g: (GRADE_CHOICES_EDIT.index(g) if g in GRADE_CHOICES_EDIT else 999, g),
    )
    _grade_opts = ["All grades"] + _uniq_gr

    _sf1, _sf2, _sf3 = st.columns((2.0, 1.2, 1.2))
    with _sf1:
        _pend_needle = st.text_input(
            "Search pending items",
            placeholder="Student name, code, draft label, details…",
            key="pending_reviews_search",
        )
    with _sf2:
        _action_sel = st.selectbox(
            "Filter by action",
            _PENDING_MS_ACTION_OPTIONS,
            key="pending_reviews_action_filter",
        )
    with _sf3:
        _grade_sel = st.selectbox(
            "Filter by grade",
            _grade_opts,
            key="pending_reviews_grade_filter",
        )
    _pn = (_pend_needle or "").strip()

    visible_ms_cb_keys = []

    st.markdown("---")
    st.subheader("Batch approval")
    st.caption(
        "**Confirm all** applies the entire queue in order. **Confirm selected** only applies rows whose checkboxes "
        "are ticked (including off-screen selections). Both require the admin password."
    )
    _batch_pw = st.text_input(
        "Admin password (batch)",
        type="password",
        key="pending_reviews_batch_pw",
        placeholder="Enter admin password",
    )
    _b1, _b2 = st.columns(2)
    with _b1:
        if st.button("Confirm all", type="primary", key="pending_reviews_confirm_all_btn"):
            if not (_batch_pw or "").strip():
                st.warning("Enter the admin password to confirm all pending drafts.")
            elif (e := evaluate_admin_password_input(_batch_pw)) is not None:
                _invalidate_admin_password_fields(
                    e,
                    "pending_reviews_batch_pw",
                )
            else:
                _n, _errs = _apply_all_pending_manage_student_reviews(conn)
                _parts = [f"**Confirm all:** applied **{_n}** draft(s)."]
                if _errs:
                    _parts.append("Notes:")
                    for _e in _errs[:15]:
                        _parts.append(f"- {_e}")
                    if len(_errs) > 15:
                        _parts.append(f"… and {len(_errs) - 15} more.")
                st.session_state["_student_flash_msg"] = "\n\n".join(_parts)
                _clear_password_field_keys("pending_reviews_batch_pw")
                st.rerun()
    with _b2:
        if st.button("Confirm selected", type="primary", key="pending_reviews_confirm_selected_btn"):
            if not (_batch_pw or "").strip():
                st.warning("Enter the admin password to confirm selected drafts.")
            elif (e := evaluate_admin_password_input(_batch_pw)) is not None:
                _invalidate_admin_password_fields(
                    e,
                    "pending_reviews_batch_pw",
                )
            else:
                _suffs = _gather_pending_checkbox_keys(PENDING_CB_MS_PREFIX)
                if not _suffs:
                    st.warning("No items selected — tick one or more checkboxes first.")
                else:
                    _n, _errs = _apply_selected_manage_student_reviews(conn, _suffs)
                    _parts = [f"**Confirm selected:** applied **{_n}** draft(s)."]
                    if _errs:
                        _parts.append("Notes:")
                        for _e in _errs[:15]:
                            _parts.append(f"- {_e}")
                        if len(_errs) > 15:
                            _parts.append(f"… and {len(_errs) - 15} more.")
                    st.session_state["_student_flash_msg"] = "\n\n".join(_parts)
                    for _pr in _suffs:
                        st.session_state.pop(f"{PENDING_CB_MS_PREFIX}{_pr}", None)
                    _clear_password_field_keys("pending_reviews_batch_pw")
                    st.rerun()

    st.markdown("---")

    _shown = 0

    if _action_sel in ("All actions", "Transfers"):
        for _sid, _pl in list(_xfer_all.items()):
            _rec = get_student_record(conn, int(_sid))
            _sn = (_rec or {}).get("name") or _pl.get("name") or f"Student id {_sid}"
            _rc = (_rec or {}).get("student_code")
            _s = "" if _rc is None else str(_rc).strip()
            _cd = display_student_code(_s) if _s else ""
            _hay = " ".join(
                str(x)
                for x in (
                    "transfer",
                    _sid,
                    _sn,
                    _cd,
                    _pl.get("name"),
                    _pl.get("transfer_reason"),
                )
            )
            if not _pending_ms_student_grade_matches(_rec, _grade_sel):
                continue
            if not _text_matches_needle(_hay, _pn):
                continue
            _shown += 1
            _cbk = f"{PENDING_CB_MS_PREFIX}transfer_{_sid}"
            visible_ms_cb_keys.append(_cbk)
            _bx, _hd = st.columns([0.08, 0.92])
            with _bx:
                st.checkbox("Select", key=_cbk, label_visibility="collapsed")
            with _hd:
                st.markdown(f"### Transfer — {_sn}")
            _dk = f"transfer_{_sid}"

            def _apply_xfer(_sid=_sid, _pl=_pl, _sn=_sn):
                if not student_record_is_editable(get_student_record(conn, int(_sid)) or {}):
                    raise ValueError("This student is no longer Active.")
                schedule_student_transfer(conn, int(_sid), _pl.get("transfer_reason"))
                invalidate_student_cache()
                remove_pending_student_transfer(conn, int(_sid))
                _rc = (get_student_record(conn, int(_sid)) or {}).get("student_code")
                _s = "" if _rc is None else str(_rc).strip()
                _code = display_student_code(_s) if _s else str(_sid)
                _audit_log(
                    conn,
                    "Manage Student",
                    f"Manage Student, {_code} ({_sn}): marked as transferred (from pending review).",
                    save_mode="approved_from_pending",
                    entity_type="student",
                    entity_id=int(_sid),
                    entity_code=str(_code),
                )
                st.session_state["_student_flash_msg"] = f"**{_sn}** marked as transferred."

            def _discard_xfer(_sid=_sid):
                remove_pending_student_transfer(conn, int(_sid))
                st.session_state["_student_flash_msg"] = "Transfer draft discarded."

            render_pending_apply_workflow(
                _dk,
                format_student_transfer_draft_summary_html(conn, int(_sid), _pl),
                _apply_xfer,
                _discard_xfer,
                confirm_prompt=f"Mark **{_sn}** as transferred?",
            )
            st.markdown("---")

    if _action_sel in ("All actions", "Deletions"):
        for _sid, _pl in list(_del_all.items()):
            _rec = get_student_record(conn, int(_sid))
            _sn = (_rec or {}).get("name") or _pl.get("name") or f"Student id {_sid}"
            _rc = (_rec or {}).get("student_code")
            _s = "" if _rc is None else str(_rc).strip()
            _cd = display_student_code(_s) if _s else ""
            _hay = " ".join(
                str(x)
                for x in (
                    "deletion",
                    "delete",
                    _sid,
                    _sn,
                    _cd,
                    _pl.get("name"),
                    _pl.get("deletion_reason"),
                )
            )
            if not _pending_ms_student_grade_matches(_rec, _grade_sel):
                continue
            if not _text_matches_needle(_hay, _pn):
                continue
            _shown += 1
            _cbk = f"{PENDING_CB_MS_PREFIX}deletion_{_sid}"
            visible_ms_cb_keys.append(_cbk)
            _bx, _hd = st.columns([0.08, 0.92])
            with _bx:
                st.checkbox("Select", key=_cbk, label_visibility="collapsed")
            with _hd:
                st.markdown(f"### Deletion — {_sn}")
            _dk = f"deletion_{_sid}"

            def _apply_del(_sid=_sid, _pl=_pl, _sn=_sn):
                if not student_record_is_editable(get_student_record(conn, int(_sid)) or {}):
                    raise ValueError("This student is no longer Active.")
                rep = schedule_student_deletion(conn, int(_sid), _pl.get("deletion_reason"))
                invalidate_student_cache()
                remove_pending_student_deletion(conn, int(_sid))
                _rc = (get_student_record(conn, int(_sid)) or {}).get("student_code")
                _s = "" if _rc is None else str(_rc).strip()
                _code = display_student_code(_s) if _s else str(_sid)
                _audit_log(
                    conn,
                    "Manage Student",
                    f"Manage Student, {_code} ({_sn}): scheduled for deletion (from pending review). "
                    f"Reason: {_pl.get('deletion_reason')}",
                    save_mode="approved_from_pending",
                    entity_type="student",
                    entity_id=int(_sid),
                    entity_code=str(_code),
                )
                st.session_state["_student_flash_msg"] = (
                    f"**{_sn}** scheduled for deletion (on or after {rep['deletion_scheduled'][:10]})."
                )

            def _discard_del(_sid=_sid):
                remove_pending_student_deletion(conn, int(_sid))
                st.session_state["_student_flash_msg"] = "Deletion draft discarded."

            render_pending_apply_workflow(
                _dk,
                format_student_deletion_draft_summary_html(conn, int(_sid), _pl),
                _apply_del,
                _discard_del,
                confirm_prompt=f"Schedule **{_sn}** for deletion?",
            )
            st.markdown("---")

    if _action_sel in ("All actions", "Clubs"):
        for draft in _club_drafts:
            _club_ids = _pending_draft_student_ids(draft)
            _club_plain_names, _club_names_esc = _pending_review_student_names_bits(conn, _club_ids)
            _hay = " ".join(
                str(x)
                for x in (
                    "club",
                    draft.get("label"),
                    draft.get("kind"),
                    draft.get("id"),
                    _club_plain_names,
                    json.dumps(draft.get("payload") or {}, default=str)[:2000],
                )
            )
            if not _pending_ms_bulk_matches_grade(conn, draft, _grade_sel):
                continue
            if not _text_matches_needle(_hay, _pn):
                continue
            _shown += 1
            _cbk = f"{PENDING_CB_MS_PREFIX}club_{draft['id']}"
            visible_ms_cb_keys.append(_cbk)
            _bx, _hd = st.columns([0.08, 0.92])
            with _bx:
                st.checkbox("Select", key=_cbk, label_visibility="collapsed")
            with _hd:
                _club_h3 = html_module.escape(str(draft.get("label", "Club draft")))
                if _club_names_esc:
                    st.markdown(
                        f"<h3>{_club_h3} — {_club_names_esc}</h3>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(f"### {_club_h3}", unsafe_allow_html=True)
            _dk = f"club_{draft['id']}"

            def _apply_club(_d=draft):
                apply_pending_club_draft(conn, _d)
                remove_pending_club_draft(conn, _d["id"])
                invalidate_student_cache()
                st.session_state["_student_flash_msg"] = f"Applied: **{_d.get('label')}**"

            def _discard_club(_did=draft["id"]):
                remove_pending_club_draft(conn, _did)
                st.session_state["_student_flash_msg"] = "Club draft discarded."

            render_pending_apply_workflow(
                _dk,
                format_club_draft_summary_html(conn, draft),
                _apply_club,
                _discard_club,
                confirm_prompt="Apply these club membership changes to the database?",
            )
            st.markdown("---")

    if _action_sel in ("All actions", "Grade & contact"):
        for draft in _grade_drafts:
            _hay = " ".join(
                str(x)
                for x in (
                    "grade",
                    "contact",
                    draft.get("label"),
                    draft.get("kind"),
                    draft.get("id"),
                    json.dumps(draft.get("payload") or {}, default=str)[:2000],
                )
            )
            if not _pending_ms_bulk_matches_grade(conn, draft, _grade_sel):
                continue
            if not _text_matches_needle(_hay, _pn):
                continue
            _shown += 1
            _cbk = f"{PENDING_CB_MS_PREFIX}grade_{draft['id']}"
            visible_ms_cb_keys.append(_cbk)
            _bx, _hd = st.columns([0.08, 0.92])
            with _bx:
                st.checkbox("Select", key=_cbk, label_visibility="collapsed")
            with _hd:
                st.markdown(f"### {draft.get('label', 'Grade contact draft')}")
            _dk = f"grade_{draft['id']}"

            def _apply_grade(_d=draft):
                rep = apply_pending_grade_contact_draft(conn, _d)
                remove_pending_grade_contact_draft(conn, _d["id"])
                invalidate_student_cache()
                st.session_state["_student_flash_msg"] = (
                    f"Applied: **{_d.get('label')}** — **{rep.get('updated', 0)}** student(s) updated."
                )

            def _discard_grade(_did=draft["id"]):
                remove_pending_grade_contact_draft(conn, _did)
                st.session_state["_student_flash_msg"] = "Grade contact draft discarded."

            render_pending_apply_workflow(
                _dk,
                format_grade_contact_draft_summary_html(draft),
                _apply_grade,
                _discard_grade,
                confirm_prompt="Apply these parent/guardian and date-of-birth updates?",
            )
            st.markdown("---")

    if _action_sel in ("All actions", "Balance"):
        for draft in _bal_drafts:
            _hay = " ".join(
                str(x)
                for x in (
                    "balance",
                    draft.get("label"),
                    draft.get("kind"),
                    draft.get("id"),
                    json.dumps(draft.get("payload") or {}, default=str)[:2000],
                )
            )
            if not _pending_ms_bulk_matches_grade(conn, draft, _grade_sel):
                continue
            if not _text_matches_needle(_hay, _pn):
                continue
            _shown += 1
            _cbk = f"{PENDING_CB_MS_PREFIX}balance_{draft['id']}"
            visible_ms_cb_keys.append(_cbk)
            _bx, _hd = st.columns([0.08, 0.92])
            with _bx:
                st.checkbox("Select", key=_cbk, label_visibility="collapsed")
            with _hd:
                st.markdown(f"### {draft.get('label', 'Balance draft')}")
            _dk = f"balance_{draft['id']}"

            def _apply_bal(_d=draft):
                rep = apply_pending_balance_draft(conn, _d)
                remove_pending_balance_draft(conn, _d["id"])
                invalidate_student_cache()
                st.session_state["_student_flash_msg"] = (
                    f"Applied: **{_d.get('label')}** — **{rep.get('updated', 0)}** balance(s) updated."
                )

            def _discard_bal(_did=draft["id"]):
                remove_pending_balance_draft(conn, _did)
                st.session_state["_student_flash_msg"] = "Balance draft discarded."

            render_pending_apply_workflow(
                _dk,
                format_balance_draft_summary_html(draft),
                _apply_bal,
                _discard_bal,
                confirm_prompt="Apply these outstanding balance updates?",
            )
            st.markdown("---")

    if _action_sel in ("All actions", "Meals"):
        for draft in _meal_drafts:
            _meal_ids = _pending_draft_student_ids(draft)
            _meal_plain_names, _meal_names_esc = _pending_review_student_names_bits(conn, _meal_ids)
            _hay = " ".join(
                str(x)
                for x in (
                    "meal",
                    "meals",
                    draft.get("label"),
                    draft.get("kind"),
                    draft.get("id"),
                    _meal_plain_names,
                    json.dumps(draft.get("payload") or {}, default=str)[:2000],
                )
            )
            if not _pending_ms_bulk_matches_grade(conn, draft, _grade_sel):
                continue
            if not _text_matches_needle(_hay, _pn):
                continue
            _shown += 1
            _cbk = f"{PENDING_CB_MS_PREFIX}meal_{draft['id']}"
            visible_ms_cb_keys.append(_cbk)
            _bx, _hd = st.columns([0.08, 0.92])
            with _bx:
                st.checkbox("Select", key=_cbk, label_visibility="collapsed")
            with _hd:
                _meal_h3 = html_module.escape(str(draft.get("label", "Meals program draft")))
                if _meal_names_esc:
                    st.markdown(
                        f"<h3>{_meal_h3} — {_meal_names_esc}</h3>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(f"### {_meal_h3}", unsafe_allow_html=True)
            _dk = f"meal_{draft['id']}"

            def _apply_meal(_d=draft):
                rep = apply_pending_meal_draft(conn, _d)
                remove_pending_meal_draft(conn, _d["id"])
                invalidate_student_cache()
                st.session_state["_student_flash_msg"] = (
                    f"Applied: **{_d.get('label')}** — **{rep.get('updated', 0)}** student(s) updated."
                )

            def _discard_meal(_did=draft["id"]):
                remove_pending_meal_draft(conn, _did)
                st.session_state["_student_flash_msg"] = "Meals program draft discarded."

            render_pending_apply_workflow(
                _dk,
                format_meal_draft_summary_html(conn, draft),
                _apply_meal,
                _discard_meal,
                confirm_prompt="Apply these meals program updates to the database?",
            )
            st.markdown("---")

    if _action_sel in ("All actions", "Transport"):
        for draft in _transport_drafts:
            _tr_ids = _pending_draft_student_ids(draft)
            _tr_plain_names, _tr_names_esc = _pending_review_student_names_bits(conn, _tr_ids)
            _hay = " ".join(
                str(x)
                for x in (
                    "transport",
                    "route",
                    draft.get("label"),
                    draft.get("kind"),
                    draft.get("id"),
                    _tr_plain_names,
                    json.dumps(draft.get("payload") or {}, default=str)[:2000],
                )
            )
            if not _pending_ms_bulk_matches_grade(conn, draft, _grade_sel):
                continue
            if not _text_matches_needle(_hay, _pn):
                continue
            _shown += 1
            _cbk = f"{PENDING_CB_MS_PREFIX}transport_{draft['id']}"
            visible_ms_cb_keys.append(_cbk)
            _bx, _hd = st.columns([0.08, 0.92])
            with _bx:
                st.checkbox("Select", key=_cbk, label_visibility="collapsed")
            with _hd:
                _tr_h3 = html_module.escape(str(draft.get("label", "Transport draft")))
                if _tr_names_esc:
                    st.markdown(
                        f"<h3>{_tr_h3} — {_tr_names_esc}</h3>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(f"### {_tr_h3}", unsafe_allow_html=True)
            _dk = f"transport_{draft['id']}"

            def _apply_tr(_d=draft):
                rep = apply_pending_transport_draft(conn, _d)
                remove_pending_transport_draft(conn, _d["id"])
                invalidate_student_cache()
                st.session_state["_student_flash_msg"] = (
                    f"Applied: **{_d.get('label')}** — **{rep.get('updated', 0)}** student(s) updated."
                )

            def _discard_tr(_did=draft["id"]):
                remove_pending_transport_draft(conn, _did)
                st.session_state["_student_flash_msg"] = "Transport draft discarded."

            render_pending_apply_workflow(
                _dk,
                format_transport_draft_summary_html(conn, draft),
                _apply_tr,
                _discard_tr,
                confirm_prompt="Apply these transport route updates to the database?",
            )
            st.markdown("---")

    if _action_sel in ("All actions", "Student edits"):
        for _sid, _pl in list(_pend_all.items()):
            _sn = _pl.get("name", f"Student id {_sid}")
            _rec0 = get_student_record(conn, int(_sid))
            _rc0 = (_rec0 or {}).get("student_code")
            _s0 = "" if _rc0 is None else str(_rc0).strip()
            _cd0 = display_student_code(_s0) if _s0 else ""
            _hay = f"student edit {_sid} {_sn} {_cd0} {json.dumps(_pl, default=str)[:2000]}"
            if not _pending_ms_student_grade_matches(_rec0, _grade_sel):
                continue
            if not _text_matches_needle(_hay, _pn):
                continue
            _shown += 1
            _rec = _rec0
            if _rec is not None and not student_record_is_editable(_rec):
                st.warning(
                    f"This learner is **{student_status_label(_rec)}** and cannot be edited. "
                    "Discard this draft — the live record is read-only until permanent removal."
                )
                _lfk = f"locked_discard_flow_{_sid}"
                if st.session_state.get(_lfk) == "discard_pw":
                    st.warning(
                        "This draft will be removed from the queue permanently. Enter the **admin password** to confirm discard."
                    )
                    _ldpw = st.text_input(
                        "Admin password",
                        type="password",
                        key=f"locked_discard_pw_{_sid}",
                        placeholder="Admin password to discard",
                    )
                    _ldc1, _ldc2 = st.columns(2)
                    with _ldc1:
                        if st.button("Confirm discard", type="primary", key=f"locked_discard_ok_{_sid}"):
                            _ld_err = evaluate_admin_password_input(_ldpw)
                            if _ld_err is not None:
                                _invalidate_admin_password_fields(
                                    _ld_err,
                                    f"locked_discard_pw_{_sid}",
                                )
                            else:
                                remove_pending_student_edit(conn, int(_sid))
                                clear_student_edit_widget_state(int(_sid))
                                st.session_state.pop(_lfk, None)
                                _clear_password_field_keys(f"locked_discard_pw_{_sid}")
                                st.session_state["_student_flash_msg"] = f"Draft for **{_sn}** discarded."
                                st.rerun()
                    with _ldc2:
                        if st.button("Cancel", key=f"locked_discard_cancel_{_sid}"):
                            _clear_password_field_keys(f"locked_discard_pw_{_sid}")
                            st.session_state.pop(_lfk, None)
                            st.rerun()
                elif st.button("Discard draft", key=f"discard_locked_student_{_sid}"):
                    st.session_state[_lfk] = "discard_pw"
                    st.rerun()
                st.markdown("---")
                continue

            _cbk = f"{PENDING_CB_MS_PREFIX}student_{_sid}"
            visible_ms_cb_keys.append(_cbk)
            _bx, _hd = st.columns([0.08, 0.92])
            with _bx:
                st.checkbox("Select", key=_cbk, label_visibility="collapsed")
            with _hd:
                st.markdown(f"### {_sn}")

            _merged = merge_student_edit_payload(conn, int(_sid), _pl)
            _summary = format_student_edit_draft_summary(conn, int(_sid), _merged)
            _dk = f"student_{_sid}"

            def _apply(_sid=_sid, _merged=_merged, _sn=_sn, _qb=_pl.get("queued_by_gate_user")):
                if not student_record_is_editable(get_student_record(conn, int(_sid)) or {}):
                    raise ValueError(
                        "This student record is no longer Active and cannot be updated."
                    )
                persist_student_edit(conn, int(_sid), _merged)
                invalidate_student_cache()
                _ok, _errs = verify_student_edit_saved(conn, int(_sid), _merged)
                remove_pending_student_edit(conn, int(_sid))
                clear_student_edit_widget_state(int(_sid))
                _rec = get_student_record(conn, int(_sid))
                _rc = (_rec or {}).get("student_code")
                _s = "" if _rc is None else str(_rc).strip()
                _code = display_student_code(_s) if _s else str(_sid)
                _audit_log(
                    conn,
                    "Manage Student",
                    f"Manage Student, {_code} ({_merged.get('name', _sn)}): applied pending changes to the database."
                    + (f" Draft had been saved for later by {_qb}." if _qb else ""),
                    save_mode="approved_from_pending",
                    detail=_audit_student_payload_detail(_merged),
                    entity_type="student",
                    entity_id=int(_sid),
                    entity_code=str(_code),
                )
                if _ok:
                    st.session_state["_student_flash_msg"] = (
                        f"**{_sn}** — all changes applied to the database."
                    )
                else:
                    st.session_state["_student_flash_msg"] = (
                        f"Applied **{_sn}** but verification reported: " + "; ".join(_errs)
                    )

            def _discard(_sid=_sid):
                remove_pending_student_edit(conn, int(_sid))
                clear_student_edit_widget_state(int(_sid))
                st.session_state["_student_flash_msg"] = "Draft discarded."

            render_pending_apply_workflow(
                _dk,
                _summary,
                _apply,
                _discard,
                confirm_prompt="Are you sure you want to save these student record changes?",
            )
            st.markdown("---")

    st.markdown("---")
    _sa1, _sa2 = st.columns(2)
    with _sa1:
        if st.button("Select all visible", key="pending_ms_select_all_visible"):
            for _k in visible_ms_cb_keys:
                st.session_state[_k] = True
            st.rerun()
    with _sa2:
        if st.button("Clear visible checkboxes", key="pending_ms_clear_visible_checks"):
            for _k in visible_ms_cb_keys:
                st.session_state[_k] = False
            st.rerun()

    if _shown == 0 and _total > 0:
        if _pn or _action_sel != "All actions" or _grade_sel != "All grades":
            st.warning("No pending items match your filters or search.")


def _render_manage_clubs_tab(conn):
    """Add clubs (fee rows), bulk assign, or import rosters."""
    st.caption(
        "Use **Add club** to register a new co-curricular fee, then assign learners under **Assign members** or **Import**. "
        "Names are matched to **Active** students (exact name; fix duplicates under **Find & edit students**). "
        "**Save for later** or **Save now** (admin password) for roster changes."
    )

    tab_add, tab_assign, tab_import = st.tabs(["Add club", "Assign members", "Import from spreadsheet"])

    with tab_add:
        st.markdown(
            '<p class="vine-help-text">'
            "Creates a **co-curricular** fee row (same as under **Fee Structure**). New clubs appear immediately in "
            "student forms, imports, and fee calculations. Set the per-term amount parents pay for this activity."
            "</p>",
            unsafe_allow_html=True,
        )
        _new_club_name = st.text_input(
            "Club / activity name *",
            placeholder="e.g. Robotics Club",
            key="manage_clubs_new_name",
            help="Shown everywhere learners pick clubs. Must be unique (ignoring spaces and capitalisation).",
        )
        _new_club_fee = st.number_input(
            "Fee per term (KSH) *",
            min_value=0.0,
            value=3000.0,
            step=100.0,
            format="%.0f",
            key="manage_clubs_new_fee",
            help="Optional club fee billed when a learner is enrolled in this activity.",
        )
        st.caption("Admin password is required to add a club.")
        _add_club_pw = st.text_input(
            "Admin password",
            type="password",
            key="manage_clubs_add_pw",
            label_visibility="collapsed",
            placeholder="Admin password",
        )
        if st.button("Add club to system", type="primary", key="manage_clubs_add_submit"):
            _nm = (_new_club_name or "").strip()
            if not _nm:
                st.warning("Enter a club name.")
            elif (e := evaluate_admin_password_input(_add_club_pw)) is not None:
                _invalidate_admin_password_fields(
                    e,
                    "manage_clubs_add_pw",
                )
            else:
                _dup = conn.execute(
                    """SELECT 1 FROM fee_structure
                       WHERE fee_category='co_curricular' AND TRIM(LOWER(fee_name)) = TRIM(LOWER(?))""",
                    (_nm,),
                ).fetchone()
                if _dup:
                    st.warning(f"A club named **{_nm}** already exists. Pick another name or edit the amount under **Fee Structure**.")
                else:
                    try:
                        cur = conn.execute(
                            """INSERT INTO fee_structure
                               (fee_category, fee_name, fee_amount, grade_applicable, transport_route, is_optional, is_one_time)
                               VALUES ('co_curricular', ?, ?, NULL, NULL, 1, 0)""",
                            (_nm, float(_new_club_fee)),
                        )
                        conn.commit()
                        _new_id = int(cur.lastrowid) if cur.lastrowid else None
                        _audit_log(
                            conn,
                            "Manage Student",
                            f"Added co-curricular club **{_nm}** (KSH {_new_club_fee:,.0f}/term, fee_structure id {_new_id}).",
                            save_mode="immediate",
                            detail=json.dumps(
                                {"fee_structure_id": _new_id, "fee_name": _nm, "fee_amount": float(_new_club_fee)},
                                default=str,
                            )[:4000],
                        )
                        invalidate_student_cache()
                        _clear_password_field_keys("manage_clubs_add_pw")
                        st.session_state.pop("manage_clubs_new_name", None)
                        st.session_state.pop("manage_clubs_new_fee", None)
                        st.session_state["_student_flash_msg"] = (
                            f"Club **{_nm}** added (KSH {float(_new_club_fee):,.0f}/term). Assign members in **Manage clubs → Assign members**."
                        )
                        st.rerun()
                    except Exception as ex:
                        conn.rollback()
                        _invalidate_admin_password_fields(
                            f"Could not add club: {ex}",
                            "manage_clubs_add_pw",
                        )

    _club_rows = conn.execute(
        "SELECT id, fee_name FROM fee_structure WHERE fee_category='co_curricular' ORDER BY fee_name"
    ).fetchall()
    _club_labels = {int(r[0]): str(r[1]) for r in _club_rows} if _club_rows else {}
    _club_ids = list(_club_labels.keys())

    with tab_assign:
        if not _club_rows:
            st.info("No clubs yet. Add one in **Add club** above (or under **Fee Structure**), then return here to assign members.")
        else:
            _pick_cid = st.selectbox(
                "Club",
                options=_club_ids,
                format_func=lambda cid: _club_labels[int(cid)],
                key="manage_clubs_pick_club",
            )
            _pick_cid = int(_pick_cid)

            if st.session_state.pop("_club_assign_clear_ui", None):
                st.session_state.pop("manage_clubs_member_search", None)
                _clear_password_field_keys("manage_clubs_assign_pw")
                st.session_state.pop(f"manage_clubs_pool_pick_{_pick_cid}", None)

            _active_df = pd.read_sql(
                "SELECT id, name, student_code, grade, co_curricular_activities, status "
                "FROM students ORDER BY name",
                conn,
            )
            _active_df = _active_df.loc[active_students_mask(_active_df)].copy()
            _members = students_in_co_curricular_club(_active_df, _pick_cid, conn=conn)
            st.markdown(
                f"**Current members:** {len(_members)} learner(s) in **{_club_labels[_pick_cid]}**"
            )
            if not _members.empty:
                st.dataframe(
                    _members[["name", "student_code", "grade"]]
                    .assign(student_code=lambda d: d["student_code"].map(_student_code_display_cell))
                    .rename(
                        columns={
                            "name": "Name",
                            "student_code": "Code",
                            "grade": "Grade",
                        }
                    ),
                    use_container_width=True,
                    hide_index=True,
                )

            st.markdown('<div class="form-container">', unsafe_allow_html=True)
            _sc1, _sc2 = st.columns([2, 1])
            with _sc1:
                _mc_search = st.text_input(
                    "Search students to add",
                    placeholder="Name, student code, or grade…",
                    key="manage_clubs_member_search",
                )
            with _sc2:
                _grade_order = list(REAL_GRADES) + [INCOMPLETE_GRADE_LABEL]
                _grades_in_df = sorted(
                    {str(g) for g in _active_df["grade"].tolist()},
                    key=lambda g: (_grade_order.index(g) if g in _grade_order else 999, g),
                )
                _grade_filter = st.selectbox(
                    "Grade filter",
                    options=["All grades"] + _grades_in_df,
                    key="manage_clubs_member_grade_filter",
                    help="Limit the search results to one class (works with the search box).",
                )

            _member_ids = set(_members["id"].astype(int).tolist()) if not _members.empty else set()
            _intent_key = f"manage_clubs_intent_{_pick_cid}"
            _all_active_ids = set(_active_df["id"].astype(int).tolist())

            _prev_assign_cid = st.session_state.get("_manage_clubs_intent_cid")
            if _prev_assign_cid != _pick_cid:
                st.session_state["_manage_clubs_intent_cid"] = _pick_cid
                st.session_state.pop(f"manage_clubs_ms_{_pick_cid}", None)
                st.session_state.pop(f"manage_clubs_pool_pick_{_pick_cid}", None)

            if _intent_key not in st.session_state:
                st.session_state[_intent_key] = []
            _pruned_intent = [int(x) for x in st.session_state[_intent_key] if int(x) in _all_active_ids]
            if _pruned_intent != list(st.session_state[_intent_key]):
                st.session_state[_intent_key] = _pruned_intent

            _label_map = {
                int(r["id"]): f"{r['name']} ({_student_code_display_cell(r.get('student_code'))}) — {r['grade']}"
                for _, r in _active_df.iterrows()
            }

            _assign_mode = st.radio(
                "How to apply",
                options=[
                    "add",
                    "replace",
                ],
                format_func=lambda m: (
                    "Add selected students to this club (keep existing members)"
                    if m == "add"
                    else "Set club roster to selected students only (remove others from this club)"
                ),
                key="manage_clubs_assign_mode",
                horizontal=True,
            )

            st.markdown("##### Suggestions")
            st.markdown(
                '<p class="vine-assign-hint">Type in the search box — up to 12 matches appear here. '
                "Click a suggestion to add it to <strong>Students selected so far</strong> "
                "(search clears after each add).</p>",
                unsafe_allow_html=True,
            )
            _intent_snapshot = [int(x) for x in st.session_state.get(_intent_key, [])]
            _suggest_df = _club_assign_suggestion_rows(
                _active_df,
                _mc_search,
                grade_filter=_grade_filter,
                intent_ids=_intent_snapshot,
                cap=12,
            )
            _qstrip = (_mc_search or "").strip()
            if not _qstrip:
                st.markdown(
                    '<p class="vine-assign-hint">Start typing a name, student code, or grade to see clickable suggestions.</p>',
                    unsafe_allow_html=True,
                )
            elif _suggest_df.empty:
                st.markdown(
                    '<p class="vine-assign-hint">No matching active students, or everyone matching is already in '
                    "<strong>Students selected so far</strong>.</p>",
                    unsafe_allow_html=True,
                )
            else:
                with st.container(border=True):
                    for _, srow in _suggest_df.iterrows():
                        sid = int(srow["id"])
                        _lbl = _label_map.get(sid, str(sid))
                        if st.button(
                            _lbl,
                            key=f"manage_clubs_sugg_add_{_pick_cid}_{sid}",
                            use_container_width=True,
                        ):
                            _cur = [int(x) for x in st.session_state.get(_intent_key, [])]
                            st.session_state[_intent_key] = list(dict.fromkeys(_cur + [sid]))
                            st.session_state.pop("manage_clubs_member_search", None)
                            st.rerun()

            st.markdown("##### Students selected so far")
            st.markdown(
                '<p class="vine-assign-hint">Only the learners you add from suggestions (or load below) — not the same list as '
                "<strong>Current members</strong> at the top. Use <strong>✕</strong> to remove someone before saving.</p>",
                unsafe_allow_html=True,
            )
            if _assign_mode == "replace":
                st.markdown(
                    '<p class="vine-assign-hint"><strong>Replace roster:</strong> the list below becomes the <strong>full</strong> '
                    "membership for this club. Use <strong>Load current members</strong> to start from today’s roster, then remove or add. "
                    "To remove a single member without rebuilding the roster, use <strong>Find &amp; edit students</strong>.</p>",
                    unsafe_allow_html=True,
                )
                if st.button(
                    "Load current members into selection",
                    key=f"manage_clubs_intent_load_{_pick_cid}",
                ):
                    if _members.empty:
                        st.session_state[_intent_key] = []
                    else:
                        st.session_state[_intent_key] = [
                            int(x) for x in _members.sort_values("name")["id"].tolist()
                        ]
                    st.rerun()

            _selected_ids = [int(x) for x in st.session_state.get(_intent_key, [])]
            if not _selected_ids:
                st.markdown(
                    '<p class="vine-assign-hint">No learners selected yet. Use suggestions above, or '
                    "<strong>Load current members</strong> in replace mode.</p>",
                    unsafe_allow_html=True,
                )
            else:
                for sid in _selected_ids:
                    _r1, _r2 = st.columns([6, 1])
                    with _r1:
                        st.text(_label_map.get(sid, str(sid)))
                    with _r2:
                        if st.button("✕", key=f"manage_clubs_rm_{_pick_cid}_{sid}", help="Remove from this save list"):
                            st.session_state[_intent_key] = [x for x in _selected_ids if int(x) != sid]
                            st.rerun()

            _assign_pw = st.text_input(
                "Admin password (for Save now)",
                type="password",
                key="manage_clubs_assign_pw",
            )
            st.markdown("</div>", unsafe_allow_html=True)

            _ca1, _ca2 = st.columns(2)
            with _ca1:
                if st.button("Save now", type="primary", key="manage_clubs_save_btn"):
                    if (e := evaluate_admin_password_input(_assign_pw)) is not None:
                        _invalidate_admin_password_fields(
                            e,
                            "manage_clubs_assign_pw",
                        )
                    elif not _selected_ids:
                        _msg = (
                            "Replace mode needs at least one learner in **Students selected so far** "
                            "(use **Load current members** or add from suggestions)."
                            if _assign_mode == "replace"
                            else "Add at least one learner from **Suggestions** before saving, or use import for a full roster."
                        )
                        _invalidate_admin_password_fields(
                            _msg,
                            "manage_clubs_assign_pw",
                            level="warn",
                        )
                    else:
                        try:
                            rep = enroll_students_in_club(
                                conn,
                                _pick_cid,
                                _selected_ids,
                                mode=_assign_mode,
                                resync_fees=True,
                                do_commit=True,
                            )
                            invalidate_student_cache()
                            _clear_password_field_keys("manage_clubs_assign_pw")
                            st.session_state.pop(_intent_key, None)
                            st.session_state.pop(f"manage_clubs_ms_{_pick_cid}", None)
                            st.session_state["_student_flash_msg"] = (
                                f"**{_club_labels[_pick_cid]}** — updated **{rep['updated']}** student record(s)."
                            )
                            st.session_state["_club_assign_clear_ui"] = True
                            st.rerun()
                        except Exception as ex:
                            _invalidate_admin_password_fields(
                                f"Could not save club members: {ex}",
                                "manage_clubs_assign_pw",
                            )
            with _ca2:
                if st.button("Save for later", key="manage_clubs_save_later_btn"):
                    if not _selected_ids:
                        _msg = (
                            "Replace mode needs at least one learner in **Students selected so far** first."
                            if _assign_mode == "replace"
                            else "Add at least one learner from **Suggestions** first."
                        )
                        _invalidate_admin_password_fields(
                            _msg,
                            "manage_clubs_assign_pw",
                            level="warn",
                        )
                    else:
                        _label = f"{_club_labels[_pick_cid]} — club assignment"
                        queue_pending_club_draft(
                            conn,
                            {
                                "id": new_bulk_draft_id(),
                                "kind": "club_assign",
                                "label": _label,
                                "payload": {
                                    "club_id": _pick_cid,
                                    "club_name": _club_labels[_pick_cid],
                                    "mode": _assign_mode,
                                    "student_ids": [int(x) for x in _selected_ids],
                                },
                            },
                        )
                        _clear_password_field_keys("manage_clubs_assign_pw")
                        st.session_state.pop(_intent_key, None)
                        st.session_state.pop(f"manage_clubs_ms_{_pick_cid}", None)
                        st.session_state["_student_flash_msg"] = (
                            f"Saved for later: **{_label}** — see **Pending Reviews**."
                        )
                        st.session_state["_club_assign_clear_ui"] = True
                        st.rerun()

    with tab_import:
        if not _club_rows:
            st.info(
                "No clubs in the database yet. Use the **Add club** tab (or **Fee Structure**) first, "
                "then import rosters so club names in your file match exactly."
            )
        st.markdown(
            '<p class="vine-help-text">'
            "Use one row per student (<strong>club</strong> + <strong>student_name</strong>), or one row per club "
            "with several names separated by commas or semicolons. Club names must match **Add club** / **Fee Structure**."
            "</p>",
            unsafe_allow_html=True,
        )
        st.download_button(
            "Download CSV template",
            data=CLUB_ROSTER_IMPORT_TEMPLATE_CSV,
            file_name="club_roster_template.csv",
            mime="text/csv",
            key="download_club_roster_template",
        )
        _club_file = st.file_uploader(
            "Club roster spreadsheet",
            type=["csv", "xlsx"],
            key="manage_clubs_upload",
        )
        if _club_file is not None:
            try:
                _club_df = read_student_spreadsheet(_club_file)
                st.caption(f"Loaded **{len(_club_df)}** row(s). Preview:")
                st.dataframe(_club_df.head(25), use_container_width=True, hide_index=True)
                _parsed = parse_club_roster_dataframe(_club_df)
                if not _parsed:
                    st.warning(
                        "Could not read club/name pairs. Use columns **club** and **student_name**, "
                        "or **club** and **students** (comma-separated names)."
                    )
                else:
                    st.caption(f"Parsed **{len(_parsed)}** club membership row(s).")
                    _import_mode = st.radio(
                        "Import mode",
                        options=["add", "replace"],
                        format_func=lambda m: (
                            "Add each student to the club (keep their other clubs and existing members)"
                            if m == "add"
                            else "Per club: roster becomes exactly the names in the file"
                        ),
                        key="manage_clubs_import_mode",
                    )
                    _dry = st.checkbox("Preview only (no database changes)", value=True, key="manage_clubs_dry")
                    if st.button("Run import preview", key="manage_clubs_preview_btn") or _dry:
                        _preview_rep = import_club_roster_assignments(
                            conn,
                            _parsed,
                            mode=_import_mode,
                            dry_run=True,
                            resync_fees=False,
                        )
                        if _preview_rep["preview"]:
                            _club_pv = pd.DataFrame(_preview_rep["preview"])
                            st.text_input(
                                "Filter preview rows",
                                placeholder="Any column text…",
                                key="manage_clubs_preview_filter",
                            )
                            _club_pf = (st.session_state.get("manage_clubs_preview_filter") or "").strip()
                            if _club_pf:
                                _pv_blob = _club_pv.astype(str).apply(
                                    lambda row: " ".join(row.fillna("")).lower(),
                                    axis=1,
                                )
                                _club_pv = _club_pv.loc[
                                    _pv_blob.str.contains(_club_pf.lower(), regex=False)
                                ].copy()
                                if _club_pv.empty:
                                    st.warning("No preview rows match your filter.")
                            if not _club_pv.empty:
                                st.dataframe(
                                    _club_pv,
                                    use_container_width=True,
                                    hide_index=True,
                                )
                        _unk = _preview_rep["unresolved"].get("unknown_clubs") or []
                        if _unk:
                            st.warning(f"Unknown clubs (add under **Add club**, **Manage clubs**, or **Fee Structure**): {', '.join(_unk)}")
                        _unm = _preview_rep["unresolved"].get("unmatched_names") or []
                        if _unm:
                            st.warning(f"**{len(_unm)}** name(s) not found in Active students.")
                        if _preview_rep["errors"]:
                            for row_no, msg in _preview_rep["errors"][:20]:
                                st.caption(f"Row {row_no}: {msg}")

                    if not _dry:
                        _import_pw = st.text_input(
                            "Admin password (for Save now)",
                            type="password",
                            key="manage_clubs_import_pw",
                        )
                        _ci1, _ci2 = st.columns(2)
                        with _ci1:
                            if st.button("Save now", type="primary", key="manage_clubs_import_btn"):
                                if (e := evaluate_admin_password_input(_import_pw)) is not None:
                                    _invalidate_admin_password_fields(
                                        e,
                                        "manage_clubs_import_pw",
                                    )
                                else:
                                    _live = import_club_roster_assignments(
                                        conn,
                                        _parsed,
                                        mode=_import_mode,
                                        dry_run=False,
                                        resync_fees=True,
                                    )
                                    invalidate_student_cache()
                                    _clear_password_field_keys("manage_clubs_import_pw")
                                    st.session_state["_student_flash_msg"] = (
                                        f"Club import — **{_live['clubs_processed']}** club(s), "
                                        f"**{_live['students_updated']}** student record(s) updated."
                                    )
                                    st.rerun()
                        with _ci2:
                            if st.button("Save for later", key="manage_clubs_import_later_btn"):
                                _label = f"Club import ({len(_parsed)} rows)"
                                queue_pending_club_draft(
                                    conn,
                                    {
                                        "id": new_bulk_draft_id(),
                                        "kind": "club_import",
                                        "label": _label,
                                        "payload": {
                                            "rows": _parsed,
                                            "mode": _import_mode,
                                        },
                                    },
                                )
                                st.session_state["_student_flash_msg"] = (
                                    f"Saved for later: **{_label}** — see **Pending Reviews**."
                                )
                                st.rerun()
            except Exception as ex:
                st.error(f"Could not read file: {ex}")


def _grade_contact_bulk_dataframe(conn, grade):
    """Active students in one grade — columns for bulk parent/DOB editing."""
    _gdf = pd.read_sql(
        """SELECT id, name, student_code, parent_name, parent_phone, parent2_name,
                  parent2_phone, date_of_birth, status
           FROM students WHERE grade = ? ORDER BY name""",
        conn,
        params=(grade,),
    )
    if _gdf.empty:
        return _gdf
    _gdf = _gdf.loc[active_students_mask(_gdf)].copy()
    _gdf["date_of_birth"] = _gdf["date_of_birth"].apply(
        lambda v: parse_date_of_birth_cell(v) or ""
    )
    return _gdf[
        [
            "id",
            "name",
            "student_code",
            "parent_name",
            "parent_phone",
            "parent2_name",
            "parent2_phone",
            "date_of_birth",
        ]
    ]


def _render_manage_grade_tab(conn):
    """Bulk fill parent/guardian contact and date of birth by grade (import or spreadsheet editor)."""
    st.caption(
        "Fill in parent/guardian names, phone numbers, and dates of birth for learners already in the system. "
        "Names are matched within the **grade** on each row. Only **Active** students are updated. "
        "Use **Save for later** or **Save now** (admin password)."
    )

    _grade_choices = list(REAL_GRADES) + [INCOMPLETE_GRADE_LABEL]
    tab_bulk, tab_import = st.tabs(["Edit by grade", "Import from spreadsheet"])

    with tab_bulk:
        _bulk_grade = st.selectbox(
            "Grade",
            options=_grade_choices,
            key="manage_grade_bulk_grade",
        )
        _base_df = _grade_contact_bulk_dataframe(conn, _bulk_grade)
        if _base_df.empty:
            st.info(f"No active students in **{_bulk_grade}** yet. Add learners under **Add Student** first.")
        else:
            st.text_input(
                "Search table",
                placeholder="Name, code, parent or phone…",
                key="manage_grade_bulk_search",
            )
            _qmg = (st.session_state.get("manage_grade_bulk_search") or "").strip()
            _gmask = _bulk_student_row_search_mask(_base_df, _qmg)
            _base_sub = _base_df.loc[_gmask].copy()
            if _qmg and _base_sub.empty:
                st.warning("No students match your search.")
            elif _base_sub.empty:
                st.info("No rows to show.")
            else:
                st.markdown(
                    f"**{len(_base_sub)}** of **{len(_base_df)}** active student(s) in **{_bulk_grade}** "
                    f"({'filtered' if _qmg else 'all'}). Edit the table below, then save all changes."
                )
            if not _base_sub.empty:
                _display = _base_sub.copy()
                _display["student_code"] = _display["student_code"].map(_student_code_display_cell)
                _display = _display.rename(
                    columns={
                        "name": "Student name",
                        "student_code": "Code",
                        "parent_name": "Parent/Guardian 1",
                        "parent_phone": "Phone 1",
                        "parent2_name": "Parent/Guardian 2",
                        "parent2_phone": "Phone 2",
                        "date_of_birth": "Date of birth",
                    }
                )
                _edited = st.data_editor(
                    _display,
                    column_config={
                        "id": None,
                        "Student name": st.column_config.TextColumn(disabled=True),
                        "Code": st.column_config.TextColumn(disabled=True),
                        "Date of birth": st.column_config.TextColumn(
                            help="YYYY-MM-DD, e.g. 2018-05-12",
                        ),
                    },
                    use_container_width=True,
                    hide_index=True,
                    key=f"manage_grade_editor_{_bulk_grade}",
                )
                _bulk_pw = st.text_input(
                    "Admin password (for Save now)",
                    type="password",
                    key="manage_grade_bulk_pw",
                )
                _rev_partial = _edited.rename(
                    columns={
                        "Student name": "name",
                        "Code": "student_code",
                        "Parent/Guardian 1": "parent_name",
                        "Phone 1": "parent_phone",
                        "Parent/Guardian 2": "parent2_name",
                        "Phone 2": "parent2_phone",
                        "Date of birth": "date_of_birth",
                    }
                )
                _gb1, _gb2 = st.columns(2)
                with _gb1:
                    if st.button("Save now", type="primary", key="manage_grade_bulk_save"):
                        if (e := evaluate_admin_password_input(_bulk_pw)) is not None:
                            _invalidate_admin_password_fields(
                                e,
                                "manage_grade_bulk_pw",
                            )
                        else:
                            _rev_full = _merge_partial_edits_into_full(_base_df, _rev_partial)
                            _rep = persist_grade_bulk_contact_edits(
                                conn, _base_df, _rev_full, resync_fees=False
                            )
                            invalidate_student_cache()
                            _clear_password_field_keys("manage_grade_bulk_pw")
                            if _rep["errors"]:
                                for sid, msg in _rep["errors"][:10]:
                                    st.warning(f"Student id {sid}: {msg}")
                            st.session_state["_student_flash_msg"] = (
                                f"**{_bulk_grade}** — updated **{_rep['updated']}** student(s)"
                                f" ({_rep['skipped']} unchanged)."
                            )
                            st.rerun()
                with _gb2:
                    if st.button("Save for later", key="manage_grade_bulk_later"):
                        _rows = _dataframe_to_contact_rows(_rev_partial)
                        _label = f"{_bulk_grade} — contact details"
                        queue_pending_grade_contact_draft(
                            conn,
                            {
                                "id": new_bulk_draft_id(),
                                "kind": "grade_bulk",
                                "label": _label,
                                "payload": {"grade": _bulk_grade, "rows": _rows},
                            },
                        )
                        st.session_state["_student_flash_msg"] = (
                            f"Saved for later: **{_label}** — see **Pending Reviews**."
                        )
                        st.rerun()

    with tab_import:
        st.markdown(
            '<p class="vine-help-text">'
            "<strong>One file per class is supported:</strong> put the class in the file name "
            "(e.g. <code>Grade_5_contacts.xlsx</code>, <code>Grade5 Parents.csv</code>) and you "
            "<strong>do not need a grade column</strong> in the sheet — everyone is matched in that grade. "
            "Parent/guardian names can be in <strong>one</strong> column (use <strong>&</strong> or "
            "<strong>and</strong> between two names) or in <strong>two</strong> columns "
            "(e.g. Mother / Father). Phones can be <strong>one</strong> column (comma-separated) or "
            "<strong>two</strong> columns (e.g. <code>mother_phone</code> / <code>father_phone</code>); "
            "<strong>only one parent number is fine</strong> — leave the other blank. "
            "Any non-empty contact field (name, phone, or DOB) is enough to update that learner."
            "</p>",
            unsafe_allow_html=True,
        )
        st.download_button(
            "Download CSV template",
            data=GRADE_ROSTER_IMPORT_TEMPLATE_CSV,
            file_name="grade_roster_template.csv",
            mime="text/csv",
            key="download_grade_roster_template",
        )
        _grade_file = st.file_uploader(
            "Grade contact spreadsheet",
            type=["csv", "xlsx"],
            key="manage_grade_upload",
        )
        if _grade_file is not None:
            try:
                _gdf = read_student_spreadsheet(_grade_file)
                st.caption(f"Loaded **{len(_gdf)}** row(s). Preview:")
                st.dataframe(_gdf.head(25), use_container_width=True, hide_index=True)
                _file_grade = infer_grade_from_filename(_grade_file.name)
                if _file_grade:
                    st.caption(f"**Grade from file name:** {_file_grade} — applied to every row (you can omit a grade column).")
                _parsed_g = parse_grade_roster_dataframe(_gdf, default_grade=_file_grade)
                if not _parsed_g:
                    st.warning(
                        "Could not read rows. Need at least a **student name** column; "
                        "optional: grade, parent/guardian names, parent phone, date of birth."
                    )
                else:
                    st.caption(f"Parsed **{len(_parsed_g)}** student row(s).")
                    _gcols = grade_roster_detected_columns(_gdf)
                    _gfn = (
                        f"- Grade (from file name): `{html_module.escape(str(_file_grade))}`  \n"
                        if _file_grade
                        else ""
                    )
                    st.markdown(
                        "**Columns detected for import:**  \n"
                        f"{_gfn}"
                        f"- Grade (column): `{_gcols.get('grade') or '—'}`  \n"
                        f"- Student name: `{_gcols.get('student_name') or '—'}`  \n"
                        f"- Parent/guardian names (1): `{_gcols.get('parent') or '—'}`  \n"
                        f"- Parent/guardian names (2): `{_gcols.get('parent2') or '—'}`  \n"
                        f"- Phone (1): `{_gcols.get('phone') or '—'}`  \n"
                        f"- Phone (2): `{_gcols.get('phone2') or '—'}`  \n"
                        f"- Date of birth: `{_gcols.get('dob') or '—'}`",
                        unsafe_allow_html=True,
                    )
                    if not any(
                        _gcols.get(k)
                        for k in ("parent", "parent2", "phone", "phone2", "dob")
                    ):
                        st.warning(
                            "**No parent/phone/date-of-birth column was recognized.** "
                            "The import only updates contact fields; without a detected column, every row shows "
                            "*Skipped (no contact data)* and **Save now** updates **0** records. "
                            "Rename a column to match the template (e.g. **parent_phone**, **parent_guardian_names**, "
                            "**date_of_birth**) or download **CSV template** above."
                        )
                    _g_dry = st.checkbox(
                        "Preview only (no database changes)",
                        value=True,
                        key="manage_grade_import_dry",
                    )
                    if st.button("Run import preview", key="manage_grade_preview_btn") or _g_dry:
                        _gprev = import_grade_roster_updates(
                            conn, _parsed_g, dry_run=True, resync_fees=False
                        )
                        if _gprev["preview"]:
                            st.dataframe(
                                pd.DataFrame(_gprev["preview"]),
                                use_container_width=True,
                                hide_index=True,
                            )
                        _gunm = _gprev["unresolved"].get("unmatched") or []
                        if _gunm:
                            st.warning(f"**{len(_gunm)}** name(s) not found in their grade.")
                        if _gprev["errors"]:
                            for row_no, msg in _gprev["errors"][:15]:
                                st.caption(f"Row {row_no}: {msg}")

                    if not _g_dry:
                        _g_import_pw = st.text_input(
                            "Admin password (for Save now)",
                            type="password",
                            key="manage_grade_import_pw",
                        )
                        _gi1, _gi2 = st.columns(2)
                        with _gi1:
                            if st.button(
                                "Save now", type="primary", key="manage_grade_import_btn"
                            ):
                                if (e := evaluate_admin_password_input(_g_import_pw)) is not None:
                                    _invalidate_admin_password_fields(
                                        e,
                                        "manage_grade_import_pw",
                                    )
                                else:
                                    _glive = import_grade_roster_updates(
                                        conn, _parsed_g, dry_run=False, resync_fees=False
                                    )
                                    invalidate_student_cache()
                                    _clear_password_field_keys("manage_grade_import_pw")
                                    _pv = _glive.get("preview") or []
                                    _skipped = sum(
                                        1
                                        for p in _pv
                                        if p.get("status") == "Skipped (no contact data)"
                                    )
                                    _nf = sum(
                                        1
                                        for p in _pv
                                        if p.get("status") == "Not found in grade"
                                    )
                                    _msg = (
                                        f"Grade import — updated **{_glive['updated']}** student record(s)."
                                    )
                                    if _glive["updated"] == 0:
                                        _msg += (
                                            f" Preview had **{_skipped}** row(s) with no contact data parsed, "
                                            f"**{_nf}** not found in grade. "
                                            "Check **Columns detected for import** above and the preview table."
                                        )
                                    st.session_state["_student_flash_msg"] = _msg
                                    st.rerun()
                        with _gi2:
                            if st.button("Save for later", key="manage_grade_import_later"):
                                _label = f"Grade contact import ({len(_parsed_g)} rows)"
                                queue_pending_grade_contact_draft(
                                    conn,
                                    {
                                        "id": new_bulk_draft_id(),
                                        "kind": "grade_import",
                                        "label": _label,
                                        "payload": {"rows": _parsed_g},
                                    },
                                )
                                st.session_state["_student_flash_msg"] = (
                                    f"Saved for later: **{_label}** — see **Pending Reviews**."
                                )
                                st.rerun()
            except Exception as ex:
                st.error(f"Could not read file: {ex}")


def _balance_bulk_dataframe(conn, grade):
    """Active students in one grade — name, code, balance for bulk editing."""
    _bdf = pd.read_sql(
        """SELECT id, name, student_code, balance, balance_set, balance_status, status
           FROM students WHERE grade = ? ORDER BY name""",
        conn,
        params=(grade,),
    )
    if _bdf.empty:
        return _bdf
    _bdf = _bdf.loc[active_students_mask(_bdf)].copy()
    _bdf["balance"] = _bdf.apply(balance_editor_display, axis=1)
    return _bdf[["id", "name", "student_code", "balance"]]


def _render_manage_balance_tab(conn):
    """Bulk set outstanding balances by grade (import or spreadsheet editor)."""
    st.caption(
        "Set each learner's balance from your spreadsheets. Names match within the **grade** on each row. "
        "Use **Not set** (not entered yet), **Cleared** (nothing owed), or a **KSH amount** "
        "(e.g. `5000` or `KSH 5,000`). Sponsored learners are stored as **Cleared**. "
        "Use **Save for later** or **Save now** (admin password)."
    )

    _grade_choices = list(REAL_GRADES) + [INCOMPLETE_GRADE_LABEL]
    tab_bulk, tab_import = st.tabs(["Edit by grade", "Import from spreadsheet"])

    with tab_bulk:
        _bal_grade = st.selectbox(
            "Grade",
            options=_grade_choices,
            key="manage_balance_bulk_grade",
        )
        _bbase = _balance_bulk_dataframe(conn, _bal_grade)
        if _bbase.empty:
            st.info(f"No active students in **{_bal_grade}** yet.")
        else:
            st.text_input(
                "Search table",
                placeholder="Name, code, balance text…",
                key="manage_balance_bulk_search",
            )
            _qbal = (st.session_state.get("manage_balance_bulk_search") or "").strip()
            _bmask = _bulk_student_row_search_mask(_bbase, _qbal)
            _bbase_sub = _bbase.loc[_bmask].copy()
            if _qbal and _bbase_sub.empty:
                st.warning("No students match your search.")
            elif _bbase_sub.empty:
                st.info("No rows to show.")
            else:
                st.markdown(
                    f"**{len(_bbase_sub)}** of **{len(_bbase)}** active student(s) in **{_bal_grade}** "
                    f"({'filtered' if _qbal else 'all'}). Edit the **Balance** column: `Not set`, `Cleared`, or a number."
                )
            if not _bbase_sub.empty:
                _bdisp = _bbase_sub.copy()
                _bdisp["student_code"] = _bdisp["student_code"].map(_student_code_display_cell)
                _bdisp = _bdisp.rename(
                    columns={
                        "name": "Student name",
                        "student_code": "Code",
                        "balance": "Balance",
                    }
                )
                _bedited = st.data_editor(
                    _bdisp,
                    column_config={
                        "id": None,
                        "Student name": st.column_config.TextColumn(disabled=True),
                        "Code": st.column_config.TextColumn(disabled=True),
                        "Balance": st.column_config.TextColumn(
                            help="Not set · Cleared · or KSH amount (e.g. 5000)",
                        ),
                    },
                    use_container_width=True,
                    hide_index=True,
                    key=f"manage_balance_editor_{_bal_grade}",
                )
                _bpw = st.text_input(
                    "Admin password (for Save now)",
                    type="password",
                    key="manage_balance_bulk_pw",
                )
                _brev_partial = _bedited.rename(
                    columns={
                        "Student name": "name",
                        "Code": "student_code",
                        "Balance": "balance",
                    }
                )
                _bb1, _bb2 = st.columns(2)
                with _bb1:
                    if st.button("Save now", type="primary", key="manage_balance_bulk_save"):
                        if (e := evaluate_admin_password_input(_bpw)) is not None:
                            _invalidate_admin_password_fields(
                                e,
                                "manage_balance_bulk_pw",
                            )
                        else:
                            _brev_full = _merge_partial_edits_into_full(_bbase, _brev_partial)
                            _brep = persist_balance_bulk_edits(conn, _bbase, _brev_full)
                            invalidate_student_cache()
                            _clear_password_field_keys("manage_balance_bulk_pw")
                            if _brep["errors"]:
                                for sid, msg in _brep["errors"][:10]:
                                    st.warning(f"Student id {sid}: {msg}")
                            st.session_state["_student_flash_msg"] = (
                                f"**{_bal_grade}** — updated **{_brep['updated']}** balance(s)"
                                f" ({_brep['skipped']} unchanged)."
                            )
                            st.rerun()
                with _bb2:
                    if st.button("Save for later", key="manage_balance_bulk_later"):
                        _brows = _dataframe_to_balance_rows(_brev_partial)
                        _blabel = f"{_bal_grade} — balances"
                        queue_pending_balance_draft(
                            conn,
                            {
                                "id": new_bulk_draft_id(),
                                "kind": "balance_bulk",
                                "label": _blabel,
                                "payload": {"grade": _bal_grade, "rows": _brows},
                            },
                        )
                        st.session_state["_student_flash_msg"] = (
                            f"Saved for later: **{_blabel}** — see **Pending Reviews**."
                        )
                        st.rerun()

    with tab_import:
        st.markdown(
            '<p class="vine-help-text">'
            "One row per student with a <strong>name</strong> column (e.g. <strong>NAMES</strong>, "
            "<strong>student_name</strong>, <strong>name</strong>) and a <strong>balance</strong> column "
            "(e.g. <strong>BALANCE</strong>, <strong>school balance</strong>). Optional <strong>grade</strong> "
            "column, or pick the grade in <strong>Edit by grade</strong> and use a filename like "
            "<code>Grade_1_balances.xlsx</code>.<br/><br/>"
            "Fee sheets with a <strong>title row</strong> then a <strong>header row</strong> (your row 2) "
            "are supported — the importer finds the row that contains NAMES and BALANCE automatically."
            "</p>",
            unsafe_allow_html=True,
        )
        st.download_button(
            "Download CSV template",
            data=BALANCE_ROSTER_IMPORT_TEMPLATE_CSV,
            file_name="balance_roster_template.csv",
            mime="text/csv",
            key="download_balance_roster_template",
        )
        _bal_file = st.file_uploader(
            "Balance spreadsheet",
            type=["csv", "xlsx"],
            key="manage_balance_upload",
        )
        if _bal_file is not None:
            try:
                _bdf = read_student_spreadsheet(_bal_file)
                st.caption(f"Loaded **{len(_bdf)}** row(s). Preview:")
                st.dataframe(_bdf.head(25), use_container_width=True, hide_index=True)
                _bfile_grade = infer_grade_from_filename(_bal_file.name)
                _parsed_b = parse_balance_roster_dataframe(_bdf, default_grade=_bfile_grade)
                if not _parsed_b:
                    _bprep, _ = prepare_balance_roster_dataframe(_bdf)
                    _bdet = balance_roster_detected_columns(_bprep)
                    st.warning(
                        "Could not read balance rows. Need a **name** column (e.g. NAMES, student_name) "
                        "and a **balance** column (e.g. BALANCE, outstanding balance). "
                        f"Detected columns — name: **{_bdet.get('student_name') or '—'}**, "
                        f"balance: **{_bdet.get('balance') or '—'}**. "
                        "If your sheet has a title row, the header row should be the one with NAMES and BALANCE."
                    )
                else:
                    st.caption(f"Parsed **{len(_parsed_b)}** balance row(s).")
                    _bdry = st.checkbox(
                        "Preview only (no database changes)",
                        value=True,
                        key="manage_balance_import_dry",
                    )
                    if st.button("Run import preview", key="manage_balance_preview_btn") or _bdry:
                        _bprev = import_balance_roster_updates(conn, _parsed_b, dry_run=True)
                        if _bprev["preview"]:
                            st.dataframe(
                                pd.DataFrame(_bprev["preview"]),
                                use_container_width=True,
                                hide_index=True,
                            )
                        _bunm = _bprev["unresolved"].get("unmatched") or []
                        if _bunm:
                            st.warning(f"**{len(_bunm)}** name(s) not found in their grade.")

                    if not _bdry:
                        _bi_pw = st.text_input(
                            "Admin password (for Save now)",
                            type="password",
                            key="manage_balance_import_pw",
                        )
                        _bi1, _bi2 = st.columns(2)
                        with _bi1:
                            if st.button(
                                "Save now", type="primary", key="manage_balance_import_btn"
                            ):
                                if (e := evaluate_admin_password_input(_bi_pw)) is not None:
                                    _invalidate_admin_password_fields(
                                        e,
                                        "manage_balance_import_pw",
                                    )
                                else:
                                    _blive = import_balance_roster_updates(
                                        conn, _parsed_b, dry_run=False
                                    )
                                    invalidate_student_cache()
                                    _clear_password_field_keys("manage_balance_import_pw")
                                    st.session_state["_student_flash_msg"] = (
                                        f"Balance import — updated **{_blive['updated']}** student(s)."
                                    )
                                    st.rerun()
                        with _bi2:
                            if st.button("Save for later", key="manage_balance_import_later"):
                                _blabel = f"Balance import ({len(_parsed_b)} rows)"
                                queue_pending_balance_draft(
                                    conn,
                                    {
                                        "id": new_bulk_draft_id(),
                                        "kind": "balance_import",
                                        "label": _blabel,
                                        "payload": {"rows": _parsed_b},
                                    },
                                )
                                st.session_state["_student_flash_msg"] = (
                                    f"Saved for later: **{_blabel}** — see **Pending Reviews**."
                                )
                                st.rerun()
            except Exception as ex:
                st.error(f"Could not read file: {ex}")


def _meal_program_bulk_dataframe(conn, grade):
    _mdf = pd.read_sql(
        """SELECT id, name, student_code, has_meal, status
           FROM students WHERE grade = ? ORDER BY name""",
        conn,
        params=(grade,),
    )
    if _mdf.empty:
        return _mdf
    _mdf = _mdf.loc[active_students_mask(_mdf)].copy()
    _mdf["meals"] = _mdf["has_meal"].apply(lambda v: "Yes" if int(v or 0) else "No")
    return _mdf[["id", "name", "student_code", "has_meal", "meals"]]


def _transport_users_bulk_dataframe(conn, grade):
    _tdf = pd.read_sql(
        """SELECT id, name, student_code, has_transport, transport_route_id, status
           FROM students WHERE grade = ? ORDER BY name""",
        conn,
        params=(grade,),
    )
    if _tdf.empty:
        return _tdf
    _tdf = _tdf.loc[active_students_mask(_tdf)].copy()

    def _tc(row):
        if int(row.get("has_transport") or 0) and row.get("transport_route_id") is not None:
            try:
                return str(int(row["transport_route_id"]))
            except (TypeError, ValueError):
                pass
        return "__none__"

    _tdf["transport_choice"] = _tdf.apply(_tc, axis=1)
    return _tdf[["id", "name", "student_code", "transport_choice"]]


def _dataframe_to_meal_rows(df):
    rows = []
    if df is None or df.empty:
        return rows
    for _, r in df.iterrows():
        mv = str(r.get("meals") or "").strip()
        if mv not in ("Yes", "No"):
            continue
        rows.append({"id": int(r["id"]), "meals": mv})
    return rows


def _meal_rows_to_dataframe(rows, base_df):
    by_id = {int(r["id"]): r for r in rows}
    out = base_df.copy()
    for idx, row in out.iterrows():
        sid = int(row["id"])
        patch = by_id.get(sid)
        if not patch:
            continue
        mv = str(patch.get("meals") or "").strip()
        if mv not in ("Yes", "No"):
            continue
        out.at[idx, "meals"] = mv
        out.at[idx, "has_meal"] = 1 if mv == "Yes" else 0
    return out


def _dataframe_to_transport_rows(df):
    rows = []
    if df is None or df.empty:
        return rows
    for _, r in df.iterrows():
        tc = str(r.get("transport_choice") or "__none__").strip()
        rows.append({"id": int(r["id"]), "transport_choice": tc})
    return rows


def _transport_rows_to_dataframe(rows, base_df):
    by_id = {int(r["id"]): r for r in rows}
    out = base_df.copy()
    for idx, row in out.iterrows():
        sid = int(row["id"])
        patch = by_id.get(sid)
        if not patch:
            continue
        out.at[idx, "transport_choice"] = str(patch.get("transport_choice") or "__none__").strip()
    return out


def _sort_bulk_student_df(df, sort_mode):
    """Sort single-grade bulk editor rows (name / student code)."""
    if df is None or df.empty:
        return df
    out = df.copy()
    if sort_mode == "name_desc":
        return out.sort_values("name", ascending=False, key=lambda s: s.astype(str).str.lower())
    if sort_mode == "code_asc":
        return out.sort_values(
            "student_code", ascending=True, key=lambda s: s.astype(str).str.lower()
        )
    if sort_mode == "code_desc":
        return out.sort_values(
            "student_code", ascending=False, key=lambda s: s.astype(str).str.lower()
        )
    return out.sort_values("name", ascending=True, key=lambda s: s.astype(str).str.lower())


def _render_manage_meal_tab(conn):
    """Bulk enable or disable the meals program by grade."""
    st.caption(
        "Set **Meals program** (Yes/No) for active learners in a grade **before** you enter balances, "
        "so fee totals are correct when you import or edit balances. "
        "Use **Quick assign** to search and add learners, or **Full grade table** for spreadsheet-style edits. "
        "**Save now** needs the admin password; **Save for later** queues changes under **Pending Reviews**."
    )
    _grade_choices = list(REAL_GRADES) + [INCOMPLETE_GRADE_LABEL]
    _meal_grade = st.selectbox("Grade", options=_grade_choices, key="manage_meal_bulk_grade")
    _mbase = _meal_program_bulk_dataframe(conn, _meal_grade)
    if _mbase.empty:
        st.info(f"No active students in **{_meal_grade}** yet.")
        return

    _prev_meal_g = st.session_state.get("_manage_meal_intent_grade")
    if _prev_meal_g is not None and _prev_meal_g != _meal_grade:
        st.session_state.pop(f"manage_meal_intent_{_prev_meal_g}", None)
    st.session_state["_manage_meal_intent_grade"] = _meal_grade

    _meal_intent_key = f"manage_meal_intent_{_meal_grade}"
    if _meal_intent_key not in st.session_state:
        st.session_state[_meal_intent_key] = {}
    _ids_in_grade = set(int(x) for x in _mbase["id"].tolist())
    _meal_intent = {
        int(k): str(v)
        for k, v in st.session_state[_meal_intent_key].items()
        if int(k) in _ids_in_grade and str(v) in ("Yes", "No")
    }
    if _meal_intent != st.session_state[_meal_intent_key]:
        st.session_state[_meal_intent_key] = _meal_intent

    st.markdown(f"**{len(_mbase)}** active student(s) in **{_meal_grade}**.")

    _tab_quick, _tab_table = st.tabs(
        [
            "Quick assign — search & add learners",
            "Full grade table — search, sort & spreadsheet",
        ]
    )

    with _tab_quick:
        st.markdown('<div class="form-container">', unsafe_allow_html=True)
        _m1, _m2 = st.columns([2, 1])
        with _m1:
            _meal_search = st.text_input(
                "Search students to add",
                placeholder="Name, student code…",
                key="manage_meal_member_search",
            )
        with _m2:
            st.selectbox(
                "Meals when adding",
                options=["Yes", "No"],
                key="manage_meal_when_add",
                help="Each learner you add from suggestions gets this meals value until you save.",
            )

        _pool_meal = _pool_df_with_grade(
            _mbase[["id", "name", "student_code"]].copy(),
            _meal_grade,
        )
        _intent_ids = list(_meal_intent.keys())
        st.markdown("##### Suggestions")
        st.markdown(
            '<p class="vine-assign-hint">Type in the search box — up to 12 matches appear here. '
            "Click a row to add it to <strong>Students to update</strong> "
            "(search clears after each add).</p>",
            unsafe_allow_html=True,
        )
        _suggest_meal = _club_assign_suggestion_rows(
            _pool_meal,
            _meal_search,
            grade_filter="All grades",
            intent_ids=_intent_ids,
            cap=12,
        )
        _meal_qs = (_meal_search or "").strip()
        if not _meal_qs:
            st.markdown(
                '<p class="vine-assign-hint">Start typing a name or student code to see clickable suggestions.</p>',
                unsafe_allow_html=True,
            )
        elif _suggest_meal.empty:
            st.markdown(
                '<p class="vine-assign-hint">No matching active students in this grade, or they are already in '
                "<strong>Students to update</strong>.</p>",
                unsafe_allow_html=True,
            )
        else:
            _meal_label_map = {
                int(r["id"]): f"{r['name']} ({_student_code_display_cell(r.get('student_code'))}) — {_meal_grade}"
                for _, r in _pool_meal.iterrows()
            }
            with st.container(border=True):
                for _, srow in _suggest_meal.iterrows():
                    sid = int(srow["id"])
                    _lbl = _meal_label_map.get(sid, str(sid))
                    if st.button(
                        _lbl,
                        key=f"manage_meal_sugg_add_{_meal_grade}_{sid}",
                        use_container_width=True,
                    ):
                        _add_mv = st.session_state.get("manage_meal_when_add") or "Yes"
                        _cur = dict(st.session_state[_meal_intent_key])
                        _cur[sid] = str(_add_mv)
                        st.session_state[_meal_intent_key] = _cur
                        st.session_state.pop("manage_meal_member_search", None)
                        st.rerun()

        st.markdown("##### Students to update")
        st.markdown(
            '<p class="vine-assign-hint">Learners you add from suggestions — use <strong>✕</strong> to remove '
            "before saving. Switch to the <strong>Full grade table</strong> tab for spreadsheet-style edits.</p>",
            unsafe_allow_html=True,
        )
        if not _meal_intent:
            st.markdown(
                '<p class="vine-assign-hint">None yet. Add from suggestions above or use the other tab.</p>',
                unsafe_allow_html=True,
            )
        else:
            _name_by_id = {int(r["id"]): str(r["name"]) for _, r in _mbase.iterrows()}
            _code_by_id = {int(r["id"]): r.get("student_code") for _, r in _mbase.iterrows()}
            for sid in sorted(_meal_intent.keys(), key=lambda x: (_name_by_id.get(x, ""), x)):
                _r1, _r2 = st.columns([6, 1])
                with _r1:
                    _nm = _name_by_id.get(sid, str(sid))
                    _cd = _student_code_display_cell(_code_by_id.get(sid))
                    st.text(f"{_nm} ({_cd}) — meals: {_meal_intent[sid]}")
                with _r2:
                    if st.button(
                        "✕", key=f"manage_meal_rm_{_meal_grade}_{sid}", help="Remove from this save list"
                    ):
                        _cur = dict(st.session_state[_meal_intent_key])
                        _cur.pop(sid, None)
                        st.session_state[_meal_intent_key] = _cur
                        st.rerun()

        _mpw = st.text_input(
            "Admin password (for Save now)",
            type="password",
            key="manage_meal_bulk_pw",
        )
        st.markdown("</div>", unsafe_allow_html=True)
        _mq1, _mq2 = st.columns(2)
        with _mq1:
            if st.button("Save now", type="primary", key="manage_meal_intent_save"):
                if (e := evaluate_admin_password_input(_mpw)) is not None:
                    _invalidate_admin_password_fields(
                        e,
                        "manage_meal_bulk_pw",
                    )
                elif not _meal_intent:
                    _invalidate_admin_password_fields(
                        "Add at least one learner from **Suggestions** before saving, or use the **Full grade table** tab.",
                        "manage_meal_bulk_pw",
                        level="warn",
                    )
                else:
                    _mrev_full = _mbase.copy()
                    for sid, mv in _meal_intent.items():
                        m = _mrev_full["id"].astype(int) == int(sid)
                        if m.any():
                            _mrev_full.loc[m, "meals"] = mv
                            _mrev_full.loc[m, "has_meal"] = 1 if mv == "Yes" else 0
                    _mrep = persist_meal_program_bulk_edits(conn, _mbase, _mrev_full)
                    invalidate_student_cache()
                    _clear_password_field_keys("manage_meal_bulk_pw")
                    st.session_state[_meal_intent_key] = {}
                    if _mrep["errors"]:
                        for sid, msg in _mrep["errors"][:10]:
                            st.warning(f"Student id {sid}: {msg}")
                    st.session_state["_student_flash_msg"] = (
                        f"**{_meal_grade}** — updated meals for **{_mrep['updated']}** student(s)"
                        f" ({_mrep['skipped']} unchanged)."
                    )
                    st.rerun()
        with _mq2:
            if st.button("Save for later", key="manage_meal_intent_later"):
                if not _meal_intent:
                    st.warning("Add at least one learner from **Suggestions** before saving for later.")
                else:
                    _mrows = [{"id": int(sid), "meals": mv} for sid, mv in _meal_intent.items()]
                    _mlabel = f"{_meal_grade} — meals program (quick list, {len(_mrows)} learner(s))"
                    queue_pending_meal_draft(
                        conn,
                        {
                            "id": new_bulk_draft_id(),
                            "kind": "meal_bulk",
                            "label": _mlabel,
                            "payload": {"grade": _meal_grade, "rows": _mrows},
                        },
                    )
                    st.session_state[_meal_intent_key] = {}
                    st.session_state["_student_flash_msg"] = (
                        f"Saved for later: **{_mlabel}** — see **Pending Reviews**."
                    )
                    st.rerun()

    with _tab_table:
        st.info(
            "**Full grade table** — filter rows with search, change sort order, then edit the **Meals program** "
            "column. **Save now** applies with the admin password; **Save for later** sends this tab’s edits to **Pending Reviews**."
        )
        _sc1, _sc2 = st.columns([2, 1])
        with _sc1:
            st.text_input(
                "Search table",
                placeholder="Name, code, meals…",
                key="manage_meal_adv_search",
            )
        with _sc2:
            st.selectbox(
                "Sort rows",
                options=["name_asc", "name_desc", "code_asc", "code_desc"],
                format_func=lambda v: {
                    "name_asc": "Name A → Z",
                    "name_desc": "Name Z → A",
                    "code_asc": "Code A → Z",
                    "code_desc": "Code Z → A",
                }[v],
                key="manage_meal_table_sort",
            )
        _qma = (st.session_state.get("manage_meal_adv_search") or "").strip()
        _msort = st.session_state.get("manage_meal_table_sort") or "name_asc"
        _mmask = _bulk_student_row_search_mask(_mbase, _qma)
        _mbase_sub = _mbase.loc[_mmask].copy()
        _mbase_sub = _sort_bulk_student_df(_mbase_sub, _msort)
        if _qma and _mbase_sub.empty:
            st.warning("No students match your search.")
        elif not _mbase_sub.empty:
            st.markdown(
                f"**{len(_mbase_sub)}** of **{len(_mbase)}** row(s) "
                f"({'filtered' if _qma else 'all'})."
            )
            _mdisp = _mbase_sub.copy()
            _mdisp["student_code"] = _mdisp["student_code"].map(_student_code_display_cell)
            _mdisp = _mdisp.rename(
                columns={
                    "name": "Student name",
                    "student_code": "Code",
                    "meals": "Meals program",
                }
            )
            _medited = st.data_editor(
                _mdisp,
                column_config={
                    "id": None,
                    "has_meal": None,
                    "Student name": st.column_config.TextColumn(disabled=True),
                    "Code": st.column_config.TextColumn(disabled=True),
                    "Meals program": st.column_config.SelectboxColumn(
                        options=["Yes", "No"],
                        required=True,
                    ),
                },
                use_container_width=True,
                hide_index=True,
                key=f"manage_meal_editor_{_meal_grade}",
            )
            _mpw_adv = st.text_input(
                "Admin password (for Save now)",
                type="password",
                key="manage_meal_adv_pw",
            )
            _mrev_partial = _medited.rename(
                columns={
                    "Student name": "name",
                    "Code": "student_code",
                    "Meals program": "meals",
                }
            )
            _mt1, _mt2 = st.columns(2)
            with _mt1:
                if st.button("Save now (table)", type="primary", key="manage_meal_adv_save"):
                    if (e := evaluate_admin_password_input(_mpw_adv)) is not None:
                        _invalidate_admin_password_fields(
                            e,
                            "manage_meal_adv_pw",
                        )
                    else:
                        _mrev_full = _merge_partial_edits_into_full(_mbase, _mrev_partial)
                        _mrep = persist_meal_program_bulk_edits(conn, _mbase, _mrev_full)
                        invalidate_student_cache()
                        _clear_password_field_keys("manage_meal_adv_pw")
                        if _mrep["errors"]:
                            for sid, msg in _mrep["errors"][:10]:
                                st.warning(f"Student id {sid}: {msg}")
                        st.session_state["_student_flash_msg"] = (
                            f"**{_meal_grade}** — updated meals for **{_mrep['updated']}** student(s)"
                            f" ({_mrep['skipped']} unchanged)."
                        )
                        st.rerun()
            with _mt2:
                if st.button("Save for later (table)", key="manage_meal_adv_later"):
                    _mrows = _dataframe_to_meal_rows(_mrev_partial)
                    if not _mrows:
                        st.warning("Edit at least one **Meals program** cell in the table before saving for later.")
                    else:
                        _mlabel = f"{_meal_grade} — meals program (table, {len(_mrows)} row(s))"
                        queue_pending_meal_draft(
                            conn,
                            {
                                "id": new_bulk_draft_id(),
                                "kind": "meal_bulk",
                                "label": _mlabel,
                                "payload": {"grade": _meal_grade, "rows": _mrows},
                            },
                        )
                        st.session_state["_student_flash_msg"] = (
                            f"Saved for later: **{_mlabel}** — see **Pending Reviews**."
                        )
                        st.rerun()


def _render_manage_transport_tab(conn):
    """Bulk assign school transport routes by grade."""
    st.caption(
        "Set **School transport** for active learners in a grade **before** you enter balances, "
        "so fee totals are correct when you import or edit balances. "
        "Use **Quick assign** to search and add learners, or **Full grade table** for spreadsheet-style edits. "
        "**Save now** needs the admin password; **Save for later** queues changes under **Pending Reviews**."
    )
    _routes = conn.execute(
        "SELECT id, fee_name, fee_amount FROM fee_structure WHERE fee_category='transport' ORDER BY fee_amount"
    ).fetchall()
    if not _routes:
        st.info("No transport routes in **Fee Structure** yet. Add routes there first.")
        return
    _t_labels = {
        "__none__": "Does not use school transport",
        **{str(int(r[0])): f"{r[1]} — KSH {float(r[2]):,.0f}" for r in _routes},
    }
    _t_route_keys = ["__none__"] + [str(int(r[0])) for r in _routes]
    _t_label_options = list(_t_labels.values())
    _t_label_to_key = {label: key for key, label in _t_labels.items()}
    _grade_choices = list(REAL_GRADES) + [INCOMPLETE_GRADE_LABEL]
    _t_grade = st.selectbox("Grade", options=_grade_choices, key="manage_transport_bulk_grade")
    _tbase = _transport_users_bulk_dataframe(conn, _t_grade)
    if _tbase.empty:
        st.info(f"No active students in **{_t_grade}** yet.")
        return

    _prev_tr_g = st.session_state.get("_manage_transport_intent_grade")
    if _prev_tr_g is not None and _prev_tr_g != _t_grade:
        st.session_state.pop(f"manage_transport_intent_{_prev_tr_g}", None)
    st.session_state["_manage_transport_intent_grade"] = _t_grade

    _tr_intent_key = f"manage_transport_intent_{_t_grade}"
    if _tr_intent_key not in st.session_state:
        st.session_state[_tr_intent_key] = {}
    _valid_route = set(_t_route_keys)
    _ids_t = set(int(x) for x in _tbase["id"].tolist())
    _tr_intent = {}
    for k, v in st.session_state[_tr_intent_key].items():
        try:
            ik = int(k)
        except (TypeError, ValueError):
            continue
        if ik not in _ids_t:
            continue
        sv = str(v).strip()
        if sv not in _valid_route:
            sv = "__none__"
        _tr_intent[ik] = sv
    if _tr_intent != st.session_state[_tr_intent_key]:
        st.session_state[_tr_intent_key] = _tr_intent

    _ti_sync = dict(_tr_intent)
    for sid in list(_ti_sync.keys()):
        _wk = f"manage_transport_intsel_{_t_grade}_{sid}"
        if _wk in st.session_state:
            _rv = str(st.session_state[_wk]).strip()
            if _rv in _valid_route:
                _ti_sync[sid] = _rv
    if _ti_sync != _tr_intent:
        st.session_state[_tr_intent_key] = _ti_sync
        _tr_intent = _ti_sync

    st.markdown(f"**{len(_tbase)}** active student(s) in **{_t_grade}**.")

    _ttab_quick, _ttab_table = st.tabs(
        [
            "Quick assign — search & add learners",
            "Full grade table — search, sort & spreadsheet",
        ]
    )

    def _collect_transport_intent_routes():
        _final_tr = {}
        for _k, _v in st.session_state[_tr_intent_key].items():
            try:
                _sid = int(_k)
            except (TypeError, ValueError):
                continue
            _rv = str(_v or "").strip()
            if _rv not in _valid_route:
                _rv = "__none__"
            _final_tr[_sid] = _rv
        for _sid in list(_final_tr.keys()):
            _wk = f"manage_transport_intsel_{_t_grade}_{_sid}"
            if _wk in st.session_state:
                _rv = str(st.session_state[_wk]).strip()
                if _rv in _valid_route:
                    _final_tr[_sid] = _rv
        return _final_tr

    with _ttab_quick:
        st.markdown('<div class="form-container">', unsafe_allow_html=True)
        _tc1, _tc2 = st.columns([2, 1])
        with _tc1:
            _tr_search = st.text_input(
                "Search students to add",
                placeholder="Name, student code…",
                key="manage_transport_member_search",
            )
        with _tc2:
            st.selectbox(
                "Route for students you add next",
                options=_t_route_keys,
                format_func=lambda k: _t_labels.get(k, k),
                key="manage_transport_route_for_add",
                help="Each learner you add from suggestions gets this route until you change it in the list below.",
            )

        _pool_tr = _pool_df_with_grade(
            _tbase[["id", "name", "student_code"]].copy(),
            _t_grade,
        )
        _tr_intent_ids = list(_tr_intent.keys())
        st.markdown("##### Suggestions")
        st.markdown(
            '<p class="vine-assign-hint">Type in the search box — up to 12 matches appear here. '
            "Click a row to add it to <strong>Students to update</strong> "
            "(search clears after each add).</p>",
            unsafe_allow_html=True,
        )
        _suggest_tr = _club_assign_suggestion_rows(
            _pool_tr,
            _tr_search,
            grade_filter="All grades",
            intent_ids=_tr_intent_ids,
            cap=12,
        )
        _tr_qs = (_tr_search or "").strip()
        if not _tr_qs:
            st.markdown(
                '<p class="vine-assign-hint">Start typing a name or student code to see clickable suggestions.</p>',
                unsafe_allow_html=True,
            )
        elif _suggest_tr.empty:
            st.markdown(
                '<p class="vine-assign-hint">No matching active students in this grade, or they are already in '
                "<strong>Students to update</strong>.</p>",
                unsafe_allow_html=True,
            )
        else:
            _tr_label_map = {
                int(r["id"]): f"{r['name']} ({_student_code_display_cell(r.get('student_code'))}) — {_t_grade}"
                for _, r in _pool_tr.iterrows()
            }
            with st.container(border=True):
                for _, srow in _suggest_tr.iterrows():
                    sid = int(srow["id"])
                    _lbl = _tr_label_map.get(sid, str(sid))
                    if st.button(
                        _lbl,
                        key=f"manage_transport_sugg_add_{_t_grade}_{sid}",
                        use_container_width=True,
                    ):
                        _rk = st.session_state.get("manage_transport_route_for_add") or "__none__"
                        if str(_rk) not in _valid_route:
                            _rk = "__none__"
                        _cur = dict(st.session_state[_tr_intent_key])
                        _cur[sid] = str(_rk)
                        st.session_state[_tr_intent_key] = _cur
                        st.session_state.pop("manage_transport_member_search", None)
                        st.rerun()

        st.markdown("##### Students to update")
        st.markdown(
            '<p class="vine-assign-hint">Change the route per learner before saving. '
            "<strong>✕</strong> removes a row from this save list. Use the <strong>Full grade table</strong> tab for spreadsheet edits.</p>",
            unsafe_allow_html=True,
        )
        if not _tr_intent:
            st.markdown(
                '<p class="vine-assign-hint">None yet. Add from suggestions above or use the other tab.</p>',
                unsafe_allow_html=True,
            )
        else:
            _tname_by_id = {int(r["id"]): str(r["name"]) for _, r in _tbase.iterrows()}
            _tcode_by_id = {int(r["id"]): r.get("student_code") for _, r in _tbase.iterrows()}
            for sid in sorted(_tr_intent.keys(), key=lambda x: (_tname_by_id.get(x, ""), x)):
                _u1, _u2, _u3 = st.columns([4, 4, 1])
                with _u1:
                    _tnm = _tname_by_id.get(sid, str(sid))
                    _tcd = _student_code_display_cell(_tcode_by_id.get(sid))
                    st.text(f"{_tnm} ({_tcd})")
                with _u2:
                    _cur_rk = _tr_intent.get(sid, "__none__")
                    if _cur_rk not in _valid_route:
                        _cur_rk = "__none__"
                    try:
                        _ix = _t_route_keys.index(_cur_rk)
                    except ValueError:
                        _ix = 0
                    st.selectbox(
                        "Route",
                        options=_t_route_keys,
                        format_func=lambda k: _t_labels.get(k, k),
                        index=_ix,
                        key=f"manage_transport_intsel_{_t_grade}_{sid}",
                        label_visibility="collapsed",
                    )
                with _u3:
                    if st.button(
                        "✕", key=f"manage_transport_rm_{_t_grade}_{sid}", help="Remove from this save list"
                    ):
                        _curd = dict(st.session_state[_tr_intent_key])
                        _curd.pop(sid, None)
                        st.session_state[_tr_intent_key] = _curd
                        st.session_state.pop(f"manage_transport_intsel_{_t_grade}_{sid}", None)
                        st.rerun()

        _tpw = st.text_input(
            "Admin password (for Save now)",
            type="password",
            key="manage_transport_bulk_pw",
        )
        st.markdown("</div>", unsafe_allow_html=True)
        _tq1, _tq2 = st.columns(2)
        with _tq1:
            if st.button("Save now", type="primary", key="manage_transport_intent_save"):
                if (e := evaluate_admin_password_input(_tpw)) is not None:
                    _invalidate_admin_password_fields(
                        e,
                        "manage_transport_bulk_pw",
                    )
                elif not _tr_intent:
                    _invalidate_admin_password_fields(
                        "Add at least one learner from **Suggestions** before saving, or use the **Full grade table** tab.",
                        "manage_transport_bulk_pw",
                        level="warn",
                    )
                else:
                    _final_tr = _collect_transport_intent_routes()
                    _trev_full = _tbase.copy()
                    for sid, _rk in _final_tr.items():
                        m = _trev_full["id"].astype(int) == int(sid)
                        if m.any():
                            _trev_full.loc[m, "transport_choice"] = _rk
                    _trep = persist_transport_users_bulk_edits(conn, _tbase, _trev_full)
                    invalidate_student_cache()
                    _clear_password_field_keys("manage_transport_bulk_pw")
                    st.session_state[_tr_intent_key] = {}
                    if _trep["errors"]:
                        for sid, msg in _trep["errors"][:10]:
                            st.warning(f"Student id {sid}: {msg}")
                    st.session_state["_student_flash_msg"] = (
                        f"**{_t_grade}** — updated transport for **{_trep['updated']}** student(s)"
                        f" ({_trep['skipped']} unchanged)."
                    )
                    st.rerun()
        with _tq2:
            if st.button("Save for later", key="manage_transport_intent_later"):
                if not _tr_intent:
                    st.warning("Add at least one learner from **Suggestions** before saving for later.")
                else:
                    _final_tr = _collect_transport_intent_routes()
                    _trows = [{"id": int(sid), "transport_choice": rk} for sid, rk in _final_tr.items()]
                    _tlabel = f"{_t_grade} — transport (quick list, {len(_trows)} learner(s))"
                    queue_pending_transport_draft(
                        conn,
                        {
                            "id": new_bulk_draft_id(),
                            "kind": "transport_bulk",
                            "label": _tlabel,
                            "payload": {"grade": _t_grade, "rows": _trows},
                        },
                    )
                    st.session_state[_tr_intent_key] = {}
                    for _sid in list(_final_tr.keys()):
                        st.session_state.pop(f"manage_transport_intsel_{_t_grade}_{_sid}", None)
                    st.session_state["_student_flash_msg"] = (
                        f"Saved for later: **{_tlabel}** — see **Pending Reviews**."
                    )
                    st.rerun()

    with _ttab_table:
        st.info(
            "**Full grade table** — filter with search, change sort order, then edit **School transport**. "
            "**Save now** uses the admin password; **Save for later** sends this tab’s edits to **Pending Reviews**."
        )
        _trc1, _trc2 = st.columns([2, 1])
        with _trc1:
            st.text_input(
                "Search table",
                placeholder="Name, code, route id…",
                key="manage_transport_adv_search",
            )
        with _trc2:
            st.selectbox(
                "Sort rows",
                options=["name_asc", "name_desc", "code_asc", "code_desc"],
                format_func=lambda v: {
                    "name_asc": "Name A → Z",
                    "name_desc": "Name Z → A",
                    "code_asc": "Code A → Z",
                    "code_desc": "Code Z → A",
                }[v],
                key="manage_transport_table_sort",
            )
        _qta = (st.session_state.get("manage_transport_adv_search") or "").strip()
        _tsort = st.session_state.get("manage_transport_table_sort") or "name_asc"
        _tmask = _bulk_student_row_search_mask(_tbase, _qta)
        _tbase_sub = _tbase.loc[_tmask].copy()
        _tbase_sub = _sort_bulk_student_df(_tbase_sub, _tsort)
        if _qta and _tbase_sub.empty:
            st.warning("No students match your search.")
        elif not _tbase_sub.empty:
            st.markdown(
                f"**{len(_tbase_sub)}** of **{len(_tbase)}** row(s) "
                f"({'filtered' if _qta else 'all'})."
            )
            _tdisp = _tbase_sub.copy()
            _tdisp["student_code"] = _tdisp["student_code"].map(_student_code_display_cell)
            _tdisp["School transport"] = _tdisp["transport_choice"].map(
                lambda k: _t_labels.get(str(k).strip(), str(k))
            )
            _tdisp = _tdisp.rename(
                columns={
                    "name": "Student name",
                    "student_code": "Code",
                }
            )[["id", "Student name", "Code", "School transport"]]
            _tedited = st.data_editor(
                _tdisp,
                column_config={
                    "id": None,
                    "Student name": st.column_config.TextColumn(disabled=True),
                    "Code": st.column_config.TextColumn(disabled=True),
                    "School transport": st.column_config.SelectboxColumn(
                        options=_t_label_options,
                        required=True,
                    ),
                },
                use_container_width=True,
                hide_index=True,
                key=f"manage_transport_editor_{_t_grade}",
            )
            _tpw_adv = st.text_input(
                "Admin password (for Save now)",
                type="password",
                key="manage_transport_adv_pw",
            )
            _trev_partial = _tedited.rename(
                columns={
                    "Student name": "name",
                    "Code": "student_code",
                }
            )
            _trev_partial["transport_choice"] = _trev_partial["School transport"].map(
                lambda lbl: _t_label_to_key.get(str(lbl).strip(), "__none__")
            )
            _tbt1, _tbt2 = st.columns(2)
            with _tbt1:
                if st.button("Save now (table)", type="primary", key="manage_transport_adv_save"):
                    if (e := evaluate_admin_password_input(_tpw_adv)) is not None:
                        _invalidate_admin_password_fields(
                            e,
                            "manage_transport_adv_pw",
                        )
                    else:
                        _trev_full = _merge_partial_edits_into_full(_tbase, _trev_partial)
                        _trep = persist_transport_users_bulk_edits(conn, _tbase, _trev_full)
                        invalidate_student_cache()
                        _clear_password_field_keys("manage_transport_adv_pw")
                        if _trep["errors"]:
                            for sid, msg in _trep["errors"][:10]:
                                st.warning(f"Student id {sid}: {msg}")
                        st.session_state["_student_flash_msg"] = (
                            f"**{_t_grade}** — updated transport for **{_trep['updated']}** student(s)"
                            f" ({_trep['skipped']} unchanged)."
                        )
                        st.rerun()
            with _tbt2:
                if st.button("Save for later (table)", key="manage_transport_adv_later"):
                    _trows = _dataframe_to_transport_rows(_trev_partial)
                    if not _trows:
                        st.warning(
                            "Change at least one **School transport** cell in the table before saving for later."
                        )
                    else:
                        _tlabel = f"{_t_grade} — transport (table, {len(_trows)} row(s))"
                        queue_pending_transport_draft(
                            conn,
                            {
                                "id": new_bulk_draft_id(),
                                "kind": "transport_bulk",
                                "label": _tlabel,
                                "payload": {"grade": _t_grade, "rows": _trows},
                            },
                        )
                        st.session_state["_student_flash_msg"] = (
                            f"Saved for later: **{_tlabel}** — see **Pending Reviews**."
                        )
                        st.rerun()


def format_student_transfer_draft_summary_html(conn, student_id, payload):
    rec = get_student_record(conn, int(student_id)) or {}
    name = html_module.escape(str(rec.get("name") or payload.get("name") or f"id {student_id}"))
    code = html_module.escape(display_student_code(str(rec.get("student_code") or "")) or "—")
    reason = html_module.escape((payload.get("transfer_reason") or "").strip() or "—")
    return (
        "<ul style='margin: 0.5rem 0 1rem 1.25rem;'>"
        f"<li><strong>Action:</strong> Mark as transferred</li>"
        f"<li><strong>Student:</strong> {name} ({code})</li>"
        f"<li><strong>Transfer note:</strong> {reason}</li>"
        f"<li><strong>After apply:</strong> record stays visible until the grace period, then permanent removal</li>"
        "</ul>"
    )


def format_student_deletion_draft_summary_html(conn, student_id, payload):
    rec = get_student_record(conn, int(student_id)) or {}
    name = html_module.escape(str(rec.get("name") or payload.get("name") or f"id {student_id}"))
    code = html_module.escape(display_student_code(str(rec.get("student_code") or "")) or "—")
    reason = html_module.escape((payload.get("deletion_reason") or "").strip() or "—")
    return (
        "<ul style='margin: 0.5rem 0 1rem 1.25rem;'>"
        f"<li><strong>Action:</strong> Schedule for deletion</li>"
        f"<li><strong>Student:</strong> {name} ({code})</li>"
        f"<li><strong>Reason:</strong> {reason}</li>"
        f"<li><strong>After apply:</strong> scheduled removal after {STUDENT_DELETION_GRACE_DAYS} days</li>"
        "</ul>"
    )


def _render_pending_manual_payments_tab(conn):
    _pend = list(st.session_state.get("pending_manual_payment_drafts") or [])
    if not _pend:
        st.info("No pending payment drafts. Use **Save for later** on the **Add payment** tab.")
        return

    st.caption(f"**{len(_pend)}** payment draft(s) waiting for review and admin password.")
    _pay_needle = st.text_input(
        "Search pending payments",
        placeholder="Student, amount, date, method, purpose, notes…",
        key="pending_manual_payments_search",
    )
    _pyn = (_pay_needle or "").strip()

    visible_pay_cb_keys = []

    st.markdown("---")
    st.subheader("Batch approval")
    st.caption(
        "**Confirm all** records every queued payment. **Confirm selected** applies only ticked rows "
        "(including off-screen). Both require the admin password."
    )
    _pay_batch_pw = st.text_input(
        "Admin password (batch)",
        type="password",
        key="pending_manual_payments_batch_pw",
        placeholder="Enter admin password",
    )
    _bp1, _bp2 = st.columns(2)
    with _bp1:
        if st.button("Confirm all", type="primary", key="pending_manual_payments_confirm_all_btn"):
            if not (_pay_batch_pw or "").strip():
                st.warning("Enter the admin password to confirm all payment drafts.")
            elif (e := evaluate_admin_password_input(_pay_batch_pw)) is not None:
                _invalidate_admin_password_fields(
                    e,
                    "pending_manual_payments_batch_pw",
                )
            else:
                _n, _errs = _apply_all_pending_manual_payments(conn)
                _parts = [f"**Confirm all:** recorded **{_n}** payment(s)."]
                if _errs:
                    _parts.append("Notes:")
                    for _e in _errs[:15]:
                        _parts.append(f"- {_e}")
                    if len(_errs) > 15:
                        _parts.append(f"… and {len(_errs) - 15} more.")
                st.session_state["_payment_flash_msg"] = "\n\n".join(_parts)
                _clear_password_field_keys("pending_manual_payments_batch_pw")
                st.rerun()
    with _bp2:
        if st.button("Confirm selected", type="primary", key="pending_manual_payments_confirm_selected_btn"):
            if not (_pay_batch_pw or "").strip():
                st.warning("Enter the admin password to confirm selected payment drafts.")
            elif (e := evaluate_admin_password_input(_pay_batch_pw)) is not None:
                _invalidate_admin_password_fields(
                    e,
                    "pending_manual_payments_batch_pw",
                )
            else:
                _ids = _gather_pending_checkbox_keys(PENDING_CB_PAY_PREFIX)
                if not _ids:
                    st.warning("No payment drafts selected — tick one or more checkboxes first.")
                else:
                    _n, _errs = _apply_selected_pending_manual_payments(conn, _ids)
                    _parts = [f"**Confirm selected:** recorded **{_n}** payment(s)."]
                    if _errs:
                        _parts.append("Notes:")
                        for _e in _errs[:15]:
                            _parts.append(f"- {_e}")
                        if len(_errs) > 15:
                            _parts.append(f"… and {len(_errs) - 15} more.")
                    st.session_state["_payment_flash_msg"] = "\n\n".join(_parts)
                    for _pid in _ids:
                        st.session_state.pop(f"{PENDING_CB_PAY_PREFIX}{_pid}", None)
                    _clear_password_field_keys("pending_manual_payments_batch_pw")
                    st.rerun()

    st.markdown("---")
    students_df = pd.read_sql(
        "SELECT id, name, student_code, grade FROM students", conn
    )

    def _student_label(sid):
        m = students_df.loc[students_df["id"] == int(sid)]
        if m.empty:
            return f"Student id {sid}"
        r = m.iloc[0]
        return f"{r['name']} — {r['grade']} ({_student_code_display_cell(r.get('student_code'))})"

    _shown_pay = 0
    for _d in _pend:
        _did = _d["id"]
        _slab = _student_label(_d.get("student_id"))
        _hay = " ".join(
            str(x)
            for x in (
                _did,
                _slab,
                _d.get("student_id"),
                _d.get("amount"),
                _d.get("payment_date"),
                _d.get("payment_method"),
                _d.get("purpose"),
                _d.get("description"),
                _d.get("transaction_id"),
            )
        )
        if not _text_matches_needle(_hay, _pyn):
            continue
        _shown_pay += 1
        _pay_cbk = f"{PENDING_CB_PAY_PREFIX}{_did}"
        visible_pay_cb_keys.append(_pay_cbk)
        _bx, _hd = st.columns([0.08, 0.92])
        with _bx:
            st.checkbox("Select", key=_pay_cbk, label_visibility="collapsed")
        with _hd:
            st.markdown("### Payment draft")
        _summary = (
            "<ul style='margin: 0.5rem 0 1rem 1.25rem;'>"
            f"<li><strong>Student:</strong> {html_module.escape(_student_label(_d.get('student_id')))}</li>"
            f"<li><strong>Amount:</strong> KSH {float(_d.get('amount', 0)):,.0f}</li>"
            f"<li><strong>Date:</strong> {html_module.escape(str(_d.get('payment_date', '—')))}</li>"
            f"<li><strong>Method:</strong> {html_module.escape(str(_d.get('payment_method', '—')))}</li>"
            f"<li><strong>Purpose:</strong> {html_module.escape(str(_d.get('purpose', '—'))[:200])}</li>"
            f"<li><strong>Notes:</strong> {html_module.escape(str(_d.get('description', '—'))[:200])}</li>"
            "</ul>"
        )

        def _apply(_d=_d, _did=_did):
            res_pay = _insert_manual_payment_row(
                conn,
                student_id=_d["student_id"],
                amount=_d["amount"],
                payment_date_iso=_d["payment_date"],
                payment_method=_d["payment_method"],
                purpose=_d["purpose"],
                transaction_id=_d.get("transaction_id") or "",
                description_notes=_d.get("description") or "",
            )
            remove_pending_manual_payment(conn, _did)
            qb = (_d.get("queued_by_gate_user") or "unknown")
            _audit_log(
                conn,
                "Payment",
                f"Approved pending payment {res_pay['internal_payment_id']} (KSH {float(_d['amount']):,.0f}) for student id {_d['student_id']}. "
                f"Draft had been saved for later by {qb}.",
                save_mode="approved_from_pending",
                internal_payment_id=res_pay["internal_payment_id"],
                detail=json.dumps(
                    {"draft_id": str(_did), "queued_by": qb, "payment_row_id": res_pay["payment_id"]},
                    default=str,
                ),
                entity_type="student",
                entity_id=int(_d["student_id"]),
            )
            st.session_state["_payment_flash_msg"] = (
                f"Payment of KSH {float(_d['amount']):,.0f} recorded."
            )

        def _discard(_did=_did):
            remove_pending_manual_payment(conn, _did)
            st.session_state["_payment_flash_msg"] = "Draft discarded."

        render_pending_apply_workflow(
            f"pay_{_did}",
            _summary,
            _apply,
            _discard,
            confirm_prompt="Are you sure you want to record this payment in the database?",
        )
        st.markdown("---")

    st.markdown("---")
    _psa1, _psa2 = st.columns(2)
    with _psa1:
        if st.button("Select all visible", key="pending_pay_select_all_visible"):
            for _k in visible_pay_cb_keys:
                st.session_state[_k] = True
            st.rerun()
    with _psa2:
        if st.button("Clear visible checkboxes", key="pending_pay_clear_visible_checks"):
            for _k in visible_pay_cb_keys:
                st.session_state[_k] = False
            st.rerun()

    if _pyn and _shown_pay == 0:
        st.warning("No payment drafts match your search.")


def _render_school_activity_page(conn):
    """Trips and other activities: purposes for payments + participant rosters."""
    st.markdown('<h2 class="section-header">School Activity</h2>', unsafe_allow_html=True)
    st.caption(
        "Create **planned** activities (trips, events, etc.). Each title is offered as its own **purpose** when you "
        "record payments under **Payment Management → Add payment**. Use **Manage participants** to build and print a list of learners."
    )
    tab_act, tab_part = st.tabs(["Activities", "Manage participants"])

    with tab_act:
        with st.form("add_school_activity_form"):
            st.subheader("Add activity")
            atitle = st.text_input("Title *", placeholder="e.g. Grade 7 — Mombasa trip (May 2026)")
            adesc = st.text_area("Description (optional)", height=72)
            a_has_date = st.checkbox("Set a calendar date for this activity", value=False)
            a_date = st.date_input("Event date", value=date.today(), key="sa_add_event_date")
            aloc = st.text_input("Location (optional)")
            if st.form_submit_button("Save activity", type="primary"):
                t = (atitle or "").strip()
                if not t:
                    st.warning("Title is required.")
                else:
                    adiso = a_date.isoformat() if a_has_date else ""
                    conn.execute(
                        """
                        INSERT INTO school_activities (title, description, activity_date, location, status)
                        VALUES (?, ?, ?, ?, 'planned')
                        """,
                        (t, (adesc or "").strip(), adiso, (aloc or "").strip()),
                    )
                    conn.commit()
                    st.success("Activity saved as **planned**. It will appear in payment purposes.")
                    st.rerun()

        activities_all = pd.read_sql(
            """
            SELECT id, title, description, activity_date, location, status, created_at, updated_at
            FROM school_activities
            ORDER BY datetime(COALESCE(updated_at, created_at)) DESC
            """,
            conn,
        )
        st.subheader("All activities")
        if activities_all.empty:
            st.info("No activities yet. Add one above.")
        else:
            st.dataframe(activities_all, use_container_width=True, hide_index=True)

            st.markdown("---")
            st.subheader("Edit or change status")
            _id_to_title = {int(r["id"]): str(r["title"]) for _, r in activities_all.iterrows()}
            aid = st.selectbox(
                "Select activity",
                options=list(_id_to_title.keys()),
                format_func=lambda i: f"{_id_to_title[int(i)]} · id {int(i)}",
                key="sa_edit_pick",
            )
            row = activities_all.loc[activities_all["id"] == aid].iloc[0]
            _raw_d = (row.get("activity_date") or "")
            _has_d = bool(str(_raw_d).strip())
            try:
                _d0 = date.fromisoformat(str(_raw_d)[:10]) if _has_d else date.today()
            except ValueError:
                _d0 = date.today()
            with st.form("edit_school_activity_form"):
                etitle = st.text_input("Title *", value=str(row.get("title") or ""), key="sa_edit_title")
                edesc = st.text_area("Description", value=str(row.get("description") or ""), height=60, key="sa_edit_desc")
                eloc = st.text_input("Location", value=str(row.get("location") or ""), key="sa_edit_loc")
                e_has_date = st.checkbox("Set / keep a calendar date", value=_has_d, key="sa_edit_hasd")
                edate = st.date_input("Event date", value=_d0, key="sa_edit_date")
                _st_ix = ["planned", "completed", "cancelled"]
                _cur_st = str(row.get("status") or "planned")
                estatus = st.selectbox(
                    "Status",
                    _st_ix,
                    index=_st_ix.index(_cur_st) if _cur_st in _st_ix else 0,
                    help="Only **planned** activities appear in the payment purpose list.",
                )
                if st.form_submit_button("Save changes", type="primary"):
                    t2 = (etitle or "").strip()
                    if not t2:
                        st.warning("Title is required.")
                    else:
                        d_iso = edate.isoformat() if e_has_date else ""
                        conn.execute(
                            """
                            UPDATE school_activities
                            SET title=?, description=?, activity_date=?, location=?, status=?,
                                updated_at=CURRENT_TIMESTAMP
                            WHERE id=?
                            """,
                            (t2, (edesc or "").strip(), d_iso, (eloc or "").strip(), estatus, int(aid)),
                        )
                        conn.commit()
                        st.success("Activity updated.")
                        st.rerun()

    with tab_part:
        planned_rows = conn.execute(
            "SELECT id, title FROM school_activities WHERE status = 'planned' ORDER BY title"
        ).fetchall()
        if not planned_rows:
            st.info("There are no **planned** activities. Add or reopen one under **Activities**.")
            return

        act_labels = {int(r[0]): str(r[1]) for r in planned_rows}
        act_id = st.selectbox(
            "Activity",
            options=list(act_labels.keys()),
            format_func=lambda i: act_labels[int(i)],
            key="sa_part_activity",
        )

        students_df = pd.read_sql(
            "SELECT id, name, student_code, grade FROM students ORDER BY name",
            conn,
        )
        if students_df.empty:
            st.warning("Add students first under **Add Student**.")
            return

        def _fmt_stu(sid):
            r = students_df.loc[students_df["id"] == int(sid)].iloc[0]
            return f"{r['name']} — {r['grade']} ({_student_code_display_cell(r.get('student_code'))})"

        already = {
            int(r[0])
            for r in conn.execute(
                "SELECT student_id FROM school_activity_participants WHERE activity_id=?",
                (int(act_id),),
            ).fetchall()
        }
        avail = students_df.loc[~students_df["id"].isin(already), "id"].tolist()
        st.markdown("##### Add participant")
        if not avail:
            st.caption("Every student is already on this participant list (or there are no students).")
        else:
            cpa, cpb = st.columns((3, 1))
            with cpa:
                pick_sid = st.selectbox(
                    "Student",
                    options=avail,
                    format_func=_fmt_stu,
                    key="sa_part_add_student",
                )
            with cpb:
                if st.button("Add to list", type="primary", key="sa_part_add_btn"):
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO school_activity_participants (activity_id, student_id, notes)
                        VALUES (?, ?, '')
                        """,
                        (int(act_id), int(pick_sid)),
                    )
                    conn.commit()
                    st.rerun()

        roster = pd.read_sql(
            """
            SELECT sap.id AS participant_row_id,
                   s.id AS student_id,
                   s.name AS name,
                   s.student_code AS student_code,
                   s.grade AS grade
            FROM school_activity_participants sap
            JOIN students s ON s.id = sap.student_id
            WHERE sap.activity_id = ?
            ORDER BY s.name
            """,
            conn,
            params=(int(act_id),),
        )
        st.markdown("##### Participants")
        if roster.empty:
            st.caption("No students on this list yet.")
        else:
            show = roster.drop(columns=["participant_row_id"]).rename(
                columns={
                    "student_id": "Student id",
                    "name": "Name",
                    "student_code": "Code",
                    "grade": "Grade",
                }
            )
            st.dataframe(show, use_container_width=True, hide_index=True)
            _csv = roster.drop(columns=["participant_row_id"]).to_csv(index=False)
            st.download_button(
                "Download roster (CSV)",
                data=_csv.encode("utf-8"),
                file_name=f"activity_{act_id}_roster.csv",
                mime="text/csv",
                key="sa_roster_csv",
            )
            _title_esc = html_module.escape(act_labels[int(act_id)])
            _rows_html = "".join(
                "<tr>"
                f"<td>{html_module.escape(str(r['name']))}</td>"
                f"<td>{html_module.escape(_student_code_display_cell(r.get('student_code')))}</td>"
                f"<td>{html_module.escape(str(r.get('grade') or ''))}</td>"
                "</tr>"
                for _, r in roster.iterrows()
            )
            _html_doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Roster</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 1.5rem; }}
h1 {{ font-size: 1.25rem; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
th, td {{ border: 1px solid #ccc; padding: 0.35rem 0.5rem; text-align: left; }}
th {{ background: #f3f4f6; }}
@media print {{ button {{ display: none; }} }}
</style></head><body>
<h1>{_title_esc}</h1>
<p>Participant roster — generated from VineLedger.</p>
<table><thead><tr><th>Name</th><th>Code</th><th>Grade</th></tr></thead><tbody>
{_rows_html}
</tbody></table>
<p><button type="button" onclick="window.print()">Print</button></p>
</body></html>"""
            st.download_button(
                "Download printable roster (HTML)",
                data=_html_doc.encode("utf-8"),
                file_name=f"activity_{act_id}_roster.html",
                mime="text/html",
                key="sa_roster_html",
            )

            st.markdown("##### Remove from list")
            rm_opts = roster[["participant_row_id", "name", "student_code"]].copy()
            if not rm_opts.empty:
                rm_id = st.selectbox(
                    "Participant to remove",
                    options=rm_opts["participant_row_id"].tolist(),
                    format_func=lambda prid: (
                        f"{rm_opts.loc[rm_opts['participant_row_id']==prid, 'name'].iloc[0]} "
                        f"({_student_code_display_cell(rm_opts.loc[rm_opts['participant_row_id']==prid, 'student_code'].iloc[0])})"
                    ),
                    key="sa_part_remove_pick",
                )
                if st.button("Remove selected", key="sa_part_remove_btn"):
                    conn.execute("DELETE FROM school_activity_participants WHERE id=?", (int(rm_id),))
                    conn.commit()
                    st.rerun()


def _render_pending_expenses_tab(conn):
    _pend = list(st.session_state.get("pending_expense_drafts") or [])
    if not _pend:
        st.info("No pending expense drafts. Use **Save for later** on the **Record expense** tab.")
        return

    st.caption(f"**{len(_pend)}** expense draft(s) waiting for review and admin password.")
    _exp_needle = st.text_input(
        "Search pending expenses",
        placeholder="Category, vendor, amount, receipt, description…",
        key="pending_expenses_search",
    )
    _exn = (_exp_needle or "").strip()

    visible_exp_cb_keys = []

    st.markdown("---")
    st.subheader("Batch approval")
    st.caption(
        "**Confirm all** records every queued expense. **Confirm selected** applies only ticked rows "
        "(including off-screen). Both require the admin password."
    )
    _exp_batch_pw = st.text_input(
        "Admin password (batch)",
        type="password",
        key="pending_expenses_batch_pw",
        placeholder="Enter admin password",
    )
    _be1, _be2 = st.columns(2)
    with _be1:
        if st.button("Confirm all", type="primary", key="pending_expenses_confirm_all_btn"):
            if not (_exp_batch_pw or "").strip():
                st.warning("Enter the admin password to confirm all expense drafts.")
            elif (e := evaluate_admin_password_input(_exp_batch_pw)) is not None:
                _invalidate_admin_password_fields(
                    e,
                    "pending_expenses_batch_pw",
                )
            else:
                _n, _errs = _apply_all_pending_expenses(conn)
                _parts = [f"**Confirm all:** recorded **{_n}** expense(s)."]
                if _errs:
                    _parts.append("Notes:")
                    for _e in _errs[:15]:
                        _parts.append(f"- {_e}")
                    if len(_errs) > 15:
                        _parts.append(f"… and {len(_errs) - 15} more.")
                st.session_state["_expense_flash_msg"] = "\n\n".join(_parts)
                _clear_password_field_keys("pending_expenses_batch_pw")
                st.rerun()
    with _be2:
        if st.button("Confirm selected", type="primary", key="pending_expenses_confirm_selected_btn"):
            if not (_exp_batch_pw or "").strip():
                st.warning("Enter the admin password to confirm selected expense drafts.")
            elif (e := evaluate_admin_password_input(_exp_batch_pw)) is not None:
                _invalidate_admin_password_fields(
                    e,
                    "pending_expenses_batch_pw",
                )
            else:
                _eids = _gather_pending_checkbox_keys(PENDING_CB_EXP_PREFIX)
                if not _eids:
                    st.warning("No expense drafts selected — tick one or more checkboxes first.")
                else:
                    _n, _errs = _apply_selected_pending_expenses(conn, _eids)
                    _parts = [f"**Confirm selected:** recorded **{_n}** expense(s)."]
                    if _errs:
                        _parts.append("Notes:")
                        for _e in _errs[:15]:
                            _parts.append(f"- {_e}")
                        if len(_errs) > 15:
                            _parts.append(f"… and {len(_errs) - 15} more.")
                    st.session_state["_expense_flash_msg"] = "\n\n".join(_parts)
                    for _eid in _eids:
                        st.session_state.pop(f"{PENDING_CB_EXP_PREFIX}{_eid}", None)
                    _clear_password_field_keys("pending_expenses_batch_pw")
                    st.rerun()

    st.markdown("---")
    _shown_exp = 0
    for _d in _pend:
        _did = _d["id"]
        _cat = (_d.get("category") or "").strip()
        _cl = (_d.get("custom_label") or "").strip()
        _hay = " ".join(
            str(x)
            for x in (
                _did,
                _cat,
                _cl,
                _d.get("amount"),
                _d.get("expense_date"),
                _d.get("payment_method"),
                _d.get("vendor"),
                _d.get("receipt_number"),
                _d.get("description"),
            )
        )
        if not _text_matches_needle(_hay, _exn):
            continue
        _shown_exp += 1
        _exp_cbk = f"{PENDING_CB_EXP_PREFIX}{_did}"
        visible_exp_cb_keys.append(_exp_cbk)
        _bx, _hd = st.columns([0.08, 0.92])
        with _bx:
            st.checkbox("Select", key=_exp_cbk, label_visibility="collapsed")
        with _hd:
            st.markdown(f"### Expense draft — KSH {float(_d.get('amount', 0)):,.0f}")
        _summary = (
            "<ul style='margin: 0.5rem 0 1rem 1.25rem;'>"
            f"<li><strong>Date:</strong> {html_module.escape(str(_d.get('expense_date', '—')))}</li>"
            f"<li><strong>Category:</strong> {html_module.escape(_cat or '—')}</li>"
            f"<li><strong>Custom label:</strong> {html_module.escape(_cl or '—')}</li>"
            f"<li><strong>Amount:</strong> KSH {float(_d.get('amount', 0)):,.0f}</li>"
            f"<li><strong>Method:</strong> {html_module.escape(str(_d.get('payment_method', '—')))}</li>"
            f"<li><strong>Vendor:</strong> {html_module.escape(str(_d.get('vendor', '—')))}</li>"
            f"<li><strong>Receipt #:</strong> {html_module.escape(str(_d.get('receipt_number', '—')))}</li>"
            f"<li><strong>Description:</strong> {html_module.escape(str(_d.get('description', '—'))[:300])}</li>"
            "</ul>"
        )

        def _apply(_d=_d, _did=_did):
            eid = _insert_expense_row(
                conn,
                category=_d.get("category"),
                custom_label=_d.get("custom_label"),
                amount=_d["amount"],
                expense_date_str=_d["expense_date"],
                description=_d["description"],
                payment_method=_d["payment_method"],
                vendor=_d.get("vendor") or "",
                receipt_number=_d.get("receipt_number") or "",
            )
            remove_pending_expense(conn, _did)
            qb = (_d.get("queued_by_gate_user") or "unknown")
            _audit_log(
                conn,
                "Expense",
                f"Approved pending expense row {eid} (KSH {float(_d['amount']):,.0f}). Draft had been saved for later by {qb}.",
                save_mode="approved_from_pending",
                entity_type="expense",
                entity_id=int(eid),
                detail=json.dumps({"draft_id": str(_did), "queued_by": qb}, default=str),
            )
            st.session_state["_expense_flash_msg"] = (
                f"Expense of KSH {float(_d['amount']):,.0f} recorded in the database."
            )

        def _discard(_did=_did):
            remove_pending_expense(conn, _did)
            st.session_state["_expense_flash_msg"] = "Draft discarded."

        render_pending_apply_workflow(
            f"exp_{_did}",
            _summary,
            _apply,
            _discard,
            confirm_prompt="Are you sure you want to record this expense in the database?",
        )
        st.markdown("---")

    st.markdown("---")
    _esa1, _esa2 = st.columns(2)
    with _esa1:
        if st.button("Select all visible", key="pending_exp_select_all_visible"):
            for _k in visible_exp_cb_keys:
                st.session_state[_k] = True
            st.rerun()
    with _esa2:
        if st.button("Clear visible checkboxes", key="pending_exp_clear_visible_checks"):
            for _k in visible_exp_cb_keys:
                st.session_state[_k] = False
            st.rerun()

    if _exn and _shown_exp == 0:
        st.warning("No expense drafts match your search.")


def read_form_co_curricular_ids(widget_prefix, student_id, co_curricular_items):
    """Read club checkbox state after a form submit (reliable inside st.form)."""
    sid = int(student_id)
    selected = []
    for item in co_curricular_items:
        key = f"{widget_prefix}_{sid}_{item[0]}"
        if st.session_state.get(key, False):
            selected.append(int(item[0]))
    return selected


def club_roster_display_df(students_df):
    """Name, age, grade only for co-curricular club lists."""
    if students_df.empty:
        return pd.DataFrame(columns=["Name", "Age", "Grade"])
    out = students_df[["name", "grade", "date_of_birth"]].copy()
    def _club_age(d):
        a = student_age_from_dob(d)
        return a if a is not None else "—"

    out["Age"] = out["date_of_birth"].apply(_club_age)
    out = out.rename(columns={"name": "Name", "grade": "Grade"})
    return out[["Name", "Age", "Grade"]].sort_values("Name", key=lambda s: s.str.lower())


# Co-curricular picker: emoji + static/clubs/{slug} background (see CLUB_STATIC_DIR).
CLUB_META_RULES = (
    (("chess",), "♟️", "chess"),
    (("musical", "music instrument"), "🎹", "music"),
    (("taekwondo",), "🥋", "taekwondo"),
    (("skating", "skate"), "⛸️", "skating"),
    (("football", "soccer"), "⚽", "football"),
    (("sign language",), "🤟", "sign_language"),
    (("french",), "🇫🇷", "french"),
    (("first aid",), "🩹", "first_aid"),
    (("acrobat",), "🤸", "acrobat"),
    (("scouting", "scout"), "⛺", "scouting"),
    (("swimming", "swim"), "🏊", "swimming"),
)


def format_grade_picker_title_html(grade):
    """Split Grade N / PPN so the digit is larger than the prefix."""
    g = str(grade).strip()
    m = re.match(r"^Grade\s+(\d+)$", g, re.I)
    if m:
        num = html_module.escape(m.group(1))
        return (
            '<span class="picker-card__title picker-card__title--split">'
            '<span class="picker-card__title-prefix">Grade</span> '
            f'<span class="picker-card__title-num">{num}</span></span>'
        )
    m = re.match(r"^PP(\d+)$", g, re.I)
    if m:
        num = html_module.escape(m.group(1))
        return (
            '<span class="picker-card__title picker-card__title--split">'
            '<span class="picker-card__title-prefix">PP</span>'
            f'<span class="picker-card__title-num">{num}</span></span>'
        )
    safe = html_module.escape(g)
    return f'<span class="picker-card__title">{safe}</span>'


def club_display_meta(fee_name):
    """Emoji and static asset slug for a co-curricular fee name."""
    name = str(fee_name or "").lower()
    for keywords, emoji, slug in CLUB_META_RULES:
        if any(k in name for k in keywords):
            return {"emoji": emoji, "slug": slug}
    return {"emoji": "🎯", "slug": "generic"}


@st.cache_data(show_spinner=False)
def _club_bg_data_url(slug):
    """Bundled club image as a data URL (webp/png/svg under static/clubs/)."""
    for ext, mime in (
        (".webp", "image/webp"),
        (".png", "image/png"),
        (".svg", "image/svg+xml"),
        (".jpg", "image/jpeg"),
    ):
        path = CLUB_STATIC_DIR / f"{slug}{ext}"
        if path.is_file():
            raw = path.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
            return f"data:{mime};base64,{b64}"
    return None


def club_bg_data_url(slug):
    """Data URL for bundled club art, or None."""
    return _club_bg_data_url(slug)


def emit_picker_card(card_html):
    """Render picker card HTML without Markdown code-block escaping (indented tags)."""
    card_html = textwrap.dedent(card_html).strip()
    if hasattr(st, "html"):
        st.html(card_html)
    else:
        st.markdown(card_html, unsafe_allow_html=True)


def render_picker_card_html(
    title,
    count,
    count_label,
    accent_color,
    eyebrow="Grade",
    title_html=None,
    title_emoji=None,
    bg_data_url=None,
):
    """Grade-first picker card for View Students category grids."""
    safe_eyebrow = html_module.escape(str(eyebrow).upper())
    safe_label = html_module.escape(str(count_label))
    try:
        count_display = int(count)
    except (TypeError, ValueError):
        count_display = 0

    if title_html:
        title_block = title_html
    else:
        safe_title = html_module.escape(str(title))
        if title_emoji:
            title_block = (
                f'<span class="picker-card__title">'
                f'<span class="picker-card__title-emoji">{title_emoji}</span> {safe_title}</span>'
            )
        else:
            title_block = f'<span class="picker-card__title">{safe_title}</span>'

    has_bg = bool(bg_data_url)
    bg_class = " picker-card--has-bg" if has_bg else ""
    card_style = f"--picker-accent: {accent_color};"

    lines = [f'<div class="picker-card{bg_class}" style="{card_style}">']
    if has_bg:
        safe_bg_url = str(bg_data_url).replace("'", "%27")
        lines.append(
            f'<div class="picker-card__bg" style="background-image: url(\'{safe_bg_url}\');"></div>'
        )
    lines.extend(
        [
            '<div class="picker-card__content">',
            f'<div class="picker-card__eyebrow">{safe_eyebrow}</div>',
            title_block,
            '<div class="picker-card__divider"></div>',
            '<div class="picker-card__stat-row">',
            f'<span class="picker-card__count">{count_display}</span>',
            f'<span class="picker-card__count-label">{safe_label}</span>',
            "</div>",
            "</div>",
            "</div>",
        ]
    )
    return "\n".join(lines)


def _require_bank_statement_upload_password():
    """Return True if upload tab may show the PDF uploader (sign-in password; user1/2/4/5 only)."""
    if st.session_state.get("bank_statement_upload_authorized"):
        return True
    if not gate_user_can_access_hidden_tabs():
        st.markdown('<h3 class="section-header">Upload bank statement</h3>', unsafe_allow_html=True)
        st.markdown(
            '<div class="warning-message">Your account cannot unlock bank statement upload.</div>',
            unsafe_allow_html=True,
        )
        return False
    st.markdown('<h3 class="section-header">Upload bank statement</h3>', unsafe_allow_html=True)
    st.markdown('<div class="form-container">', unsafe_allow_html=True)
    st.caption(
        "Enter your **sign-in password** (the same password you use at the gate) to show the PDF uploader on this tab."
    )
    pw = st.text_input(
        "Sign-in password",
        type="password",
        key="bank_stmt_upload_gate_pw",
        placeholder="Your account password",
    )
    if st.button("Continue", type="primary", key="bank_stmt_upload_gate_btn"):
        if verify_current_gate_login_password(pw):
            st.session_state.bank_statement_upload_authorized = True
            _clear_password_field_keys("bank_stmt_upload_gate_pw")
            st.rerun()
        else:
            _invalidate_admin_password_fields(
                "Incorrect password.",
                "bank_stmt_upload_gate_pw",
            )
    st.markdown('</div>', unsafe_allow_html=True)
    return False


def _render_bank_statement_upload(conn):
    if not _require_bank_statement_upload_password():
        return
    st.markdown('<div class="form-container">', unsafe_allow_html=True)
    _gate_cols = st.columns([3, 1])
    with _gate_cols[0]:
        st.markdown(
            '<h3 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Upload bank statement</h3>',
            unsafe_allow_html=True,
        )
    with _gate_cols[1]:
        if st.button(
            "Lock upload",
            key="bank_stmt_upload_lock",
            help="Require your sign-in password again before the next upload",
        ):
            st.session_state.bank_statement_upload_authorized = False
            st.rerun()
    file = st.file_uploader(
        "Select bank statement (PDF, CSV, or Excel)",
        type=["pdf", "csv", "xlsx", "xls"],
        help="PDF: text or table layout. CSV/Excel: columns should include Transaction details (or similar), Credit (money in), and Debit (money out). "
        "Outgoing rows are skipped when Debit has an amount. M-Pesa **U…** codes are read from the narration for matching.",
    )
    st.markdown('</div>', unsafe_allow_html=True)

    if not file:
        return

    students = pd.read_sql("SELECT * FROM students", conn)
    transactions = []
    with st.spinner("Processing bank statement..."):
        try:
            transactions = extract_transactions(file)
            if transactions:
                st.markdown(
                    f'<div class="success-message">Successfully processed <strong>{len(transactions)}</strong> transactions from the statement</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div class="warning-message">No transactions found in the uploaded statement. Please check the file format.</div>',
                    unsafe_allow_html=True,
                )
        except Exception as e:
            st.markdown(
                f'<div class="warning-message">Error processing statement: {str(e)}</div>',
                unsafe_allow_html=True,
            )
            transactions = []

    if not transactions:
        return

    pay_hints = load_payment_transaction_id_hints(conn)
    results = []
    for tx in transactions:
        match, score = match_payment(tx, students, pay_hints)
        results.append(
            {
                "description": tx["description"],
                "amount": tx["amount"],
                "student_id": match["id"] if match is not None else None,
                "student_name": match["name"] if match is not None else "No Match",
                "score": score,
                "confidence": "High" if score >= 70 else "Medium" if score >= 40 else "Low",
                "mpesa_u_code": (tx.get("mpesa_u_code") or "").strip(),
            }
        )

    df = pd.DataFrame(results)

    st.markdown('<h3 class="section-header">Transaction Matching Results</h3>', unsafe_allow_html=True)
    st.markdown('<div class="form-container">', unsafe_allow_html=True)
    st.markdown(
        '<h4 style="color: var(--text); margin-bottom: 0.5rem; font-weight: 600;">Filter & Process Options</h4>',
        unsafe_allow_html=True,
    )

    min_confidence = st.selectbox(
        "Minimum Confidence Level",
        options=["All", "High", "Medium", "Low"],
        index=0,
        help="Filter transactions by matching confidence",
    )

    st.caption("An admin password is required before any statement rows are written to the database.")
    stmt_commit_pw = st.text_input(
        "Admin password for statement import",
        type="password",
        key="stmt_commit_pw_global",
        label_visibility="collapsed",
        placeholder="Admin password",
    )

    if st.button(
        "Process Selected Payments",
        type="primary",
        use_container_width=True,
        key="stmt_process_bulk_matches",
        help="Update student balances and create payment records for all matched rows in the confidence filter",
    ):
        if (e := evaluate_admin_password_input(stmt_commit_pw)) is not None:
            _invalidate_admin_password_fields(
                e,
                "stmt_commit_pw_global",
            )
        else:
            with st.spinner("Processing payments..."):
                try:
                    if min_confidence != "All":
                        confidence_threshold = {"High": 70, "Medium": 40, "Low": 0}[min_confidence]
                        df_filtered = df[df["score"] >= confidence_threshold]
                    else:
                        df_filtered = df

                    processed_count = 0
                    affected_ids = set()
                    bank_ipids = []
                    for _, row in df_filtered.iterrows():
                        if row["student_id"] is not None:
                            sid = int(row["student_id"])
                            affected_ids.add(sid)
                            _ip = new_internal_payment_id()
                            bank_ipids.append(_ip)
                            _bank_tid = (
                                (str(row.get("mpesa_u_code") or "").strip())
                                or (str(row.get("description") or ""))
                            )[:200]
                            conn.execute(
                                """
                                INSERT INTO payments (student_id, amount, transaction_id, description, matched, internal_payment_id)
                                VALUES (?, ?, ?, ?, 1, ?)
                                """,
                                (sid, row["amount"], _bank_tid, row["description"], _ip),
                            )
                            conn.execute(
                                """
                                UPDATE students 
                                SET total_paid = total_paid + ?,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                                """,
                                (row["amount"], sid),
                            )
                            processed_count += 1

                    conn.commit()
                    affected_list = list(affected_ids)
                    for i, sid in enumerate(affected_list):
                        sync_student_fees_from_db(conn, sid, do_commit=(i == len(affected_list) - 1))
                    invalidate_student_cache()
                    if processed_count > 0:
                        _audit_log(
                            conn,
                            "Payment",
                            f"Bank statement (filtered import): recorded {processed_count} payment(s).",
                            save_mode="immediate",
                            detail=json.dumps(
                                {"internal_payment_ids": bank_ipids[:500], "count": processed_count},
                                default=str,
                            )[:8000],
                        )

                    if processed_count > 0:
                        st.session_state["_payment_flash_msg"] = (
                            f"Recorded {processed_count} statement payment(s). Balances updated."
                        )
                        st.markdown(
                            f'<div class="success-message">Successfully processed <strong>{processed_count}</strong> payments and updated student records!</div>',
                            unsafe_allow_html=True,
                        )
                        st.markdown('<div class="dataframe-container">', unsafe_allow_html=True)
                        summary_data = []
                        for _, row in df_filtered.iterrows():
                            if row["student_id"] is not None:
                                student = students[students["id"] == row["student_id"]].iloc[0]
                                summary_data.append(
                                    [
                                        student["name"],
                                        _student_code_display_cell(student.get("student_code")),
                                        f"KSH {row['amount']:,.0f}",
                                        row["confidence"],
                                        row["description"][:50] + "..."
                                        if len(row["description"]) > 50
                                        else row["description"],
                                    ]
                                )
                        if summary_data:
                            summary_df = pd.DataFrame(
                                summary_data,
                                columns=["Student", "Code", "Amount", "Confidence", "Description"],
                            )
                            st.dataframe(summary_df, use_container_width=True)
                        st.markdown('</div>', unsafe_allow_html=True)
                        _clear_password_field_keys("stmt_commit_pw_global")
                        st.rerun()
                    else:
                        st.markdown(
                            '<div class="warning-message">No payments met the confidence criteria for processing.</div>',
                            unsafe_allow_html=True,
                        )
                except Exception as e:
                    conn.rollback()
                    _invalidate_admin_password_fields(
                        f"Error processing payments: {str(e)}",
                        "stmt_commit_pw_global",
                    )

    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="form-container">', unsafe_allow_html=True)
    st.markdown(
        '<h4 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Display Options</h4>',
        unsafe_allow_html=True,
    )

    col1, col2 = st.columns(2)
    with col1:
        show_unmatched = st.checkbox(
            "Show Unmatched Only",
            value=False,
            help="Only show transactions that couldn't be matched to students",
        )
    with col2:
        min_confidence2 = st.selectbox(
            "Minimum Confidence Level",
            ["All", "Low", "Medium", "High"],
            index=0,
            help="Filter transactions by matching confidence",
            key="stmt_display_min_conf",
        )

    st.markdown('</div>', unsafe_allow_html=True)

    filtered_df = df.copy()
    if show_unmatched:
        filtered_df = filtered_df[filtered_df["student_name"] == "No Match"]
    if min_confidence2 != "All":
        confidence_map = {"Low": 0, "Medium": 40, "High": 70}
        min_score = confidence_map[min_confidence2]
        filtered_df = filtered_df[filtered_df["score"] >= min_score]

    if not filtered_df.empty:
        matched_count = len(filtered_df[filtered_df["student_name"] != "No Match"])
        total_amount = filtered_df["amount"].sum()

        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown(
                f"""
                <div class="metric-card" style="border-left-color: var(--primary);">
                    <div style="font-size: 1.25rem; font-weight: 600; color: var(--primary);">{len(filtered_df)}</div>
                    <div class="vine-help-text">Filtered Transactions</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        with col2:
            st.markdown(
                f"""
                <div class="metric-card" style="border-left-color: var(--success);">
                    <div style="font-size: 1.25rem; font-weight: 600; color: var(--success);">{matched_count}</div>
                    <div class="vine-help-text">Matched Payments</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        with col3:
            st.markdown(
                f"""
                <div class="metric-card" style="border-left-color: var(--info);">
                    <div style="font-size: 1.25rem; font-weight: 600; color: var(--info);">KSH {total_amount:,.0f}</div>
                    <div class="vine-help-text">Total Amount</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        display_df = filtered_df.copy()
        if "mpesa_u_code" not in display_df.columns:
            display_df["mpesa_u_code"] = ""
        display_df = display_df[
            ["description", "mpesa_u_code", "amount", "student_id", "student_name", "score", "confidence"]
        ]
        display_df["amount"] = display_df["amount"].apply(lambda x: f"KSH {x:,.0f}")

        def get_confidence_badge(confidence):
            colors = {"High": "#059669", "Medium": "#d97706", "Low": "#dc2626"}
            return f'<span class="status-badge" style="background: {colors[confidence]}20; color: {colors[confidence]}">{confidence}</span>'

        display_df["confidence"] = display_df["confidence"].apply(get_confidence_badge)

        display_df.columns = [
            "Transaction Description",
            "M-Pesa U code",
            "Amount",
            "Student ID",
            "Student Name",
            "Match Score",
            "Confidence",
        ]

        st.markdown('<div class="dataframe-container">', unsafe_allow_html=True)
        st.dataframe(display_df, use_container_width=True)
        st.markdown('</div>', unsafe_allow_html=True)

        st.markdown('<h3 class="section-header">Process Payments</h3>', unsafe_allow_html=True)

        matched_df = filtered_df[filtered_df["student_name"] != "No Match"]

        if not matched_df.empty:
            st.markdown('<div class="form-container">', unsafe_allow_html=True)
            st.caption("Use the **same admin password** as in Filter & Process Options above to confirm.")
            selected_transactions = st.multiselect(
                "Select transactions to process",
                options=matched_df.index.tolist(),
                format_func=lambda x: (
                    f"KSH {matched_df.iloc[x]['amount']:,.0f} - {matched_df.iloc[x]['student_name']} ({matched_df.iloc[x]['confidence']})"
                ),
                help="Select the matched transactions you want to process and update student balances",
            )

            if selected_transactions:
                total_selected = matched_df.iloc[selected_transactions]["amount"].sum()
                st.markdown(
                    f'<div class="info-message">Selected <strong>{len(selected_transactions)}</strong> transactions totaling <strong>KSH {total_selected:,.0f}</strong></div>',
                    unsafe_allow_html=True,
                )

                if st.button(
                    "Process selected transactions",
                    type="primary",
                    key="stmt_process_multiselect",
                    help="This will update student balances and record payments",
                ):
                    if (e := evaluate_admin_password_input(stmt_commit_pw)) is not None:
                        _invalidate_admin_password_fields(
                            e,
                            "stmt_commit_pw_global",
                        )
                    else:
                        processed_count = 0
                        affected_ids = set()
                        bank_ipids = []
                        for idx in selected_transactions:
                            row = matched_df.iloc[idx]
                            if row["student_id"] is not None:
                                sid = int(row["student_id"])
                                affected_ids.add(sid)
                                _ip = new_internal_payment_id()
                                bank_ipids.append(_ip)
                                _bank_tid = (
                                    (str(row.get("mpesa_u_code") or "").strip())
                                    or (str(row.get("description") or ""))
                                )[:200]
                                conn.execute(
                                    """
                                    INSERT INTO payments (student_id, amount, transaction_id, description, matched, internal_payment_id)
                                    VALUES (?, ?, ?, ?, ?, ?)
                                    """,
                                    (sid, row["amount"], _bank_tid, row["description"], True, _ip),
                                )
                                conn.execute(
                                    """
                                    UPDATE students 
                                    SET total_paid = total_paid + ?,
                                        updated_at = CURRENT_TIMESTAMP
                                    WHERE id = ?
                                    """,
                                    (row["amount"], sid),
                                )
                                processed_count += 1

                        conn.commit()
                        affected_list = list(affected_ids)
                        for i, sid in enumerate(affected_list):
                            sync_student_fees_from_db(conn, sid, do_commit=(i == len(affected_list) - 1))
                        invalidate_student_cache()
                        if processed_count > 0:
                            _audit_log(
                                conn,
                                "Payment",
                                f"Bank statement (selected rows): recorded {processed_count} payment(s).",
                                save_mode="immediate",
                                detail=json.dumps(
                                    {"internal_payment_ids": bank_ipids[:500], "count": processed_count},
                                    default=str,
                                )[:8000],
                            )
                        st.session_state["_payment_flash_msg"] = (
                            f"Recorded {processed_count} selected statement payment(s)."
                        )
                        st.markdown(
                            f'<div class="success-message">Successfully processed <strong>{processed_count}</strong> payments!</div>',
                            unsafe_allow_html=True,
                        )
                        _clear_password_field_keys("stmt_commit_pw_global")
                        st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.markdown(
                '<div class="info-message">No matched transactions available for processing.</div>',
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            '<div class="info-message">No transactions match the selected criteria.</div>',
            unsafe_allow_html=True,
        )


def _render_manual_payment_tab(conn):
    """Cash and other ad-hoc payments; drafts and Save now mirror Add Expense."""
    """Cash and other ad-hoc payments; pending drafts are on the Pending Reviews tab."""

    students_df = pd.read_sql(
        "SELECT id, name, student_code, grade, parent_name, parent_phone, parent2_name, parent2_phone FROM students ORDER BY name",
        conn,
    )
    if students_df.empty:
        st.info("Add students under **Add Student** before recording manual payments.")
        return

    def _fmt_student(sid):
        r = students_df.loc[students_df["id"] == sid].iloc[0]
        return f"{r['name']} — {r['grade']} ({_student_code_display_cell(r.get('student_code'))})"

    st.markdown(
        '<h3 style="color: var(--text); margin-bottom: 0.5rem; font-weight: 600;">Add payment</h3>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Records a payment linked to a student so balances update and **Generate receipts → From payments** can use it."
    )

    st.markdown(
        '<p class="vine-help-text vine-help-strong" style="margin: 1rem 0 0.35rem 0;">Find a student</p>',
        unsafe_allow_html=True,
    )
    _uniq_grades_pay = sorted(
        students_df["grade"].dropna().astype(str).unique().tolist(),
        key=lambda g: (REAL_GRADES.index(g) if g in REAL_GRADES else 999, g),
    )
    _grade_opts_pay = ["All grades"] + _uniq_grades_pay
    _sf1, _sf2 = st.columns((2.2, 1.1))
    with _sf1:
        _search_pay = st.text_input(
            "Search students",
            value="",
            key="manual_pay_student_search",
            placeholder="Name, student code, grade, parent/guardian or phone…",
            help="Matches if every word you type appears somewhere in the student row (name, code, grade, guardians, phones).",
        )
    with _sf2:
        _grade_f_pay = st.selectbox(
            "Grade filter",
            options=_grade_opts_pay,
            key="manual_pay_grade_filter",
            help="Limit the list to one grade, or all grades.",
        )

    _filtered_pay = students_df.copy()
    if _grade_f_pay != "All grades":
        _filtered_pay = _filtered_pay[_filtered_pay["grade"].astype(str) == str(_grade_f_pay)]
    _qpay = (_search_pay or "").strip().lower()
    if _qpay:
        _toks = [t for t in _qpay.split() if t]

        def _manual_pay_row_match(r):
            _scd = _student_code_display_cell(r.get("student_code"))
            _hay = " ".join(
                [
                    str(r.get("name") or ""),
                    str(r.get("student_code") or ""),
                    _scd,
                    str(r.get("grade") or ""),
                    str(r.get("parent_name") or ""),
                    str(r.get("parent_phone") or "").replace(" ", ""),
                    str(r.get("parent2_name") or ""),
                    str(r.get("parent2_phone") or "").replace(" ", ""),
                ]
            ).lower()
            return all(t in _hay for t in _toks)

        _filtered_pay = _filtered_pay[_filtered_pay.apply(_manual_pay_row_match, axis=1)]

    st.caption(f"**{len(_filtered_pay)}** learner(s) match your search and grade filter.")

    if _filtered_pay.empty:
        st.warning("No students match these filters. Clear the search box or choose **All grades**.")
        return

    _purpose_opts = get_payment_purpose_options(conn)

    with st.form("manual_payment_form"):
        student_id = st.selectbox(
            "Student *",
            options=_filtered_pay["id"].tolist(),
            format_func=_fmt_student,
            help="Learner receiving credit for this payment. Use search and grade filters above to narrow this list.",
        )
        row = students_df.loc[students_df["id"] == student_id].iloc[0]
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.markdown(f"**Student**  \n{row['name']}")
        with c2:
            st.markdown(f"**Grade**  \n{row['grade']}")
        with c3:
            st.markdown(f"**Parent/Guardian 1**  \n{row['parent_name'] or '—'}")
            st.markdown(f"**Phone**  \n{row['parent_phone'] or '—'}")
        with c4:
            st.markdown(f"**Parent/Guardian 2**  \n{row.get('parent2_name') or '—'}")
            st.markdown(f"**Phone**  \n{row.get('parent2_phone') or '—'}")
        st.caption("To change name, grade, or parent/guardian details, use **Manage Students**.")

        col_a, col_b = st.columns(2)
        with col_a:
            amount = st.number_input("Amount *", min_value=0, step=1, format="%d", help="KSH received")
            payment_date = st.date_input("Payment date *", help="When the payment was received")
            payment_method = st.selectbox(
                "Payment method *",
                list(MANUAL_PAYMENT_METHOD_OPTIONS),
            )
        with col_b:
            tx_help = (
                "Optional for Cash (a 9-letter/digit code is assigned if left blank). "
                "For M-Pesa paybill lines, enter the **U…** code from the bank SMS or statement so "
                "**Upload bank statement** can match this learner. Required for other methods."
            )
            transaction_code = st.text_input(
                "Transaction / reference code",
                placeholder="e.g. UEK6251GV9 (M-Pesa) or bank ref",
                help=tx_help,
            )
            purpose = st.selectbox(
                "Purpose *",
                _purpose_opts,
                help="Stored on the payment row and shown in payment history and receipts. "
                "Planned **School Activity** entries appear here when you add them under **School Activity**.",
            )

        st.markdown(
            '<p class="vine-help-text vine-help-strong" style="margin: 0.75rem 0 0.35rem 0;">Actual payer (optional)</p>',
            unsafe_allow_html=True,
        )
        _opc1, _opc2 = st.columns(2)
        with _opc1:
            other_payer_name = st.text_input(
                "Other payer or guardian name",
                placeholder="If different from guardians below",
                help="If **Guardian 2** is empty on the learner record, this name (and phone) are saved there; "
                "otherwise a short note is added to the payment row only.",
            )
        with _opc2:
            other_payer_phone = st.text_input(
                "Other payer phone",
                placeholder="e.g. 0712… or 2547…",
                help="Optional. When saved as guardian 2, the number is normalized like other phones.",
            )

        other_details = st.text_area(
            "Other details",
            placeholder="Optional notes (receipt remarks, payer name if different, etc.)",
            help="Optional longer description stored on the payment row.",
        )

        st.caption("Admin password is required only when you click **Save now**.")
        admin_pw = st.text_input(
            "Admin password",
            type="password",
            key="manual_pay_admin_pw",
            label_visibility="collapsed",
            placeholder="Admin password (Save now only)",
        )

        b1, b2, b3 = st.columns(3)
        with b1:
            save_now = st.form_submit_button("Save now", type="primary", use_container_width=True)
        with b2:
            save_later = st.form_submit_button("Save for later", use_container_width=True)
        with b3:
            if st.form_submit_button("Clear form", use_container_width=True):
                st.rerun()

        pay_date_iso = payment_date.strftime("%Y-%m-%d")
        _payload = {
            "student_id": int(student_id),
            "amount": float(amount),
            "payment_date": pay_date_iso,
            "payment_method": payment_method,
            "purpose": purpose,
            "transaction_id": transaction_code,
            "description": other_details,
            "other_payer_name": other_payer_name,
            "other_payer_phone": other_payer_phone,
        }

        if save_now:
            ok, err = _validate_manual_payment_entry(
                student_id=student_id,
                amount=amount,
                purpose=purpose,
                payment_method=payment_method,
                transaction_id=transaction_code,
            )
            if not ok:
                _invalidate_admin_password_fields(err, "manual_pay_admin_pw", level="warn")
            elif (e := evaluate_admin_password_input(admin_pw)) is not None:
                _invalidate_admin_password_fields(
                    e,
                    "manual_pay_admin_pw",
                )
            else:
                try:
                    res_pay = _insert_manual_payment_row(
                        conn,
                        student_id=_payload["student_id"],
                        amount=_payload["amount"],
                        payment_date_iso=_payload["payment_date"],
                        payment_method=_payload["payment_method"],
                        purpose=_payload["purpose"],
                        transaction_id=_payload["transaction_id"],
                        description_notes=_payload["description"],
                    )
                    apply_optional_other_payer_for_payment(
                        conn,
                        int(student_id),
                        res_pay["payment_id"],
                        _payload.get("other_payer_name"),
                        _payload.get("other_payer_phone"),
                    )
                    invalidate_student_cache()
                    _scd = _student_code_display_cell(row.get("student_code"))
                    _audit_log(
                        conn,
                        "Payment",
                        f"Manual payment {res_pay['internal_payment_id']} for student {_scd} ({row['name']}): "
                        f"KSH {float(amount):,.0f}; purpose: {(purpose or '').strip() or '—'}.",
                        save_mode="immediate",
                        internal_payment_id=res_pay["internal_payment_id"],
                        detail=json.dumps(
                            {"payment_row_id": res_pay["payment_id"], "method": payment_method},
                            default=str,
                        ),
                        entity_type="student",
                        entity_id=int(student_id),
                        entity_code=_scd,
                    )
                    st.session_state["_payment_flash_msg"] = (
                        f"Payment of KSH {float(amount):,.0f} recorded for {_fmt_student(student_id)}."
                    )
                    _clear_password_field_keys("manual_pay_admin_pw")
                    st.rerun()
                except Exception as e:
                    _invalidate_admin_password_fields(
                        f"Error: {e}",
                        "manual_pay_admin_pw",
                    )

        if save_later:
            ok, err = _validate_manual_payment_entry(
                student_id=student_id,
                amount=amount,
                purpose=purpose,
                payment_method=payment_method,
                transaction_id=transaction_code,
            )
            if not ok:
                st.markdown(
                    f'<div class="warning-message">{err}</div>',
                    unsafe_allow_html=True,
                )
            else:
                _draft = {**_payload, "id": str(uuid.uuid4()), "queued_by_gate_user": _gate_audit_user()}
                queue_pending_manual_payment(conn, _draft)
                _scd = _student_code_display_cell(row.get("student_code"))
                _audit_log(
                    conn,
                    "Payment",
                    f"Manual payment draft {_draft['id']} for student {_scd} ({row['name']}): "
                    f"KSH {float(amount):,.0f} — saved for pending review.",
                    save_mode="pending_review",
                    detail=json.dumps(
                        {"draft_id": _draft["id"], "student_id": int(student_id)},
                        default=str,
                    ),
                    entity_type="student",
                    entity_id=int(student_id),
                    entity_code=_scd,
                )
                st.session_state["_payment_flash_msg"] = (
                    "Payment saved for later. Open **Payment Management → Pending Reviews** to review and apply."
                )
                st.rerun()


# Theme: default + sidebar widget must run before CSS so injected styles match the user's choice.
if "theme_selector" not in st.session_state:
    st.session_state.theme_selector = "Dark"
st.sidebar.markdown("### Theme Settings")
st.sidebar.selectbox("Choose Theme", ["Light", "Dark"], key="theme_selector")
theme = st.session_state.theme_selector

# Custom CSS with enhanced modern design and theme support
if theme == "Dark":
    css_theme = """
    /* Dark theme - VineLedger Modern Design */
    :root {
        --primary: #818cf8;
        --primary-dark: #6366f1;
        --primary-light: #a5b4fc;
        --secondary: #a78bfa;
        --accent: #f472b6;
        --success: #34d399;
        --warning: #fbbf24;
        --danger: #f87171;
        --info: #60a5fa;
        --dark: #f8fafc;
        --light: #1e293b;
        --gray: #94a3b8;
        --border: #334155;
        --background: #0f172a;
        --surface: #1e293b;
        --surface-alt: #334155;
        --text: #f1f5f9;
        --text-secondary: #cbd5e1;
        --shadow: rgba(0, 0, 0, 0.3);
    }
    body {
        background: linear-gradient(135deg, var(--background) 0%, #1a1f3a 100%);
        color: var(--text);
        margin: 0;
        padding: 0;
        min-height: 100vh;
    }
    .stApp {
        background: transparent;
    }
    /* Dark mode full screen background */
    .stApp > div > div > div > div > div,
    .main,
    .main .block-container {
        background: #0f172a !important;
        min-height: 100vh !important;
        width: 100% !important;
        margin: 0 !important;
        padding: 2rem !important;
    }
    /* Dark mode sidebar — root + Streamlit wrappers (testids stable across OS; class names are not) */
    section[data-testid="stSidebar"],
    div[data-testid="stSidebar"],
    .stSidebar,
    [data-testid="stSidebar"] {
        background-color: #1e293b !important;
        background-image: none !important;
        border-right: 2px solid #334155 !important;
        color-scheme: dark;
    }
    [data-testid="stSidebar"] > div,
    [data-testid="stSidebar"] > div > div,
    [data-testid="stSidebarContent"],
    [data-testid="stSidebarContent"] > div,
    [data-testid="stSidebarNav"],
    [data-testid="stSidebarNav"] > div,
    .stSidebar > div {
        background-color: #1e293b !important;
        background-image: none !important;
    }
    .stSidebar [data-testid="stMarkdownContainer"],
    .stSidebar .stSelectbox,
    .stSidebar button,
    .stSidebar p,
    .stSidebar h1,
    .stSidebar h2,
    .stSidebar h3,
    .stSidebar h4,
    .stSidebar h5,
    .stSidebar h6,
    .stSidebar div,
    .stSidebar label {
        color: #f1f5f9 !important;
        font-weight: 500 !important;
        font-style: italic !important;
        font-family: 'Georgia', 'Times New Roman', serif !important;
    }
    /* Sidebar navigation: keep button labels upright (not explanatory body copy) */
    .stSidebar button,
    .stSidebar button *,
    [data-testid="stSidebar"] button,
    [data-testid="stSidebar"] button * {
        font-style: normal !important;
        font-weight: 600 !important;
    }
    """
else:
    css_theme = """
    /* Light theme - VineLedger Modern Design */
    :root {
        --primary: #6366f1;
        --primary-dark: #4f46e5;
        --primary-light: #818cf8;
        --secondary: #8b5cf6;
        --accent: #ec4899;
        --success: #10b981;
        --warning: #f59e0b;
        --danger: #ef4444;
        --info: #3b82f6;
        --dark: #1e293b;
        --light: #f8fafc;
        --gray: #64748b;
        --border: #e2e8f0;
        --background: #ffffff;
        --surface: #ffffff;
        --surface-alt: #f8fafc;
        --text: #1e293b;
        --text-secondary: #64748b;
        --shadow: rgba(0, 0, 0, 0.1);
    }
    body {
        background: linear-gradient(135deg, #ffffff 0%, #f8fafc 50%, #f1f5f9 100%);
        color: var(--text);
    }
    .stApp {
        background: transparent;
    }
    /* Light mode specific sidebar - Force light background */
    .stSidebar,
    .css-1lcbmhc,
    .css-1d391kg,
    [data-testid="stSidebar"] {
        background: #ffffff !important;
        border-right: 3px solid #1e293b !important;
        box-shadow: 2px 0 12px rgba(0, 0, 0, 0.2) !important;
    }
    
    /* Force all sidebar text to be dark with maximum specificity */
    .stSidebar *,
    .stSidebar [data-testid="stMarkdownContainer"] *,
    .stSidebar .stSelectbox *,
    .stSidebar p *,
    .stSidebar h1 *,
    .stSidebar h2 *,
    .stSidebar h3 *,
    .stSidebar h4 *,
    .stSidebar h5 *,
    .stSidebar h6 *,
    .stSidebar div *,
    .stSidebar label *,
    .stSidebar span *,
    .stSidebar .css-1lcbmhc *,
    .stSidebar .css-1d391kg *,
    [data-testid="stSidebar"] *,
    .stSidebar [data-testid="stMarkdownContainer"],
    .stSidebar .stSelectbox,
    .stSidebar p,
    .stSidebar h1,
    .stSidebar h2,
    .stSidebar h3,
    .stSidebar h4,
    .stSidebar h5,
    .stSidebar h6,
    .stSidebar div,
    .stSidebar label,
    .stSidebar span,
    .stSidebar .css-1lcbmhc,
    .stSidebar .css-1d391kg,
    [data-testid="stSidebar"] {
        color: #1e293b !important;
        font-weight: 600 !important;
        font-style: italic !important;
        font-family: 'Georgia', 'Times New Roman', serif !important;
    }
    
    /* Ensure button text remains white with higher specificity */
    .stSidebar button,
    .stSidebar button *,
    [data-testid="stSidebar"] button,
    [data-testid="stSidebar"] button * {
        color: white !important;
        font-style: normal !important;
        font-weight: 600 !important;
    }
    
    /* Force selectbox text to be dark */
    .stSidebar select,
    .stSidebar select option,
    [data-testid="stSidebar"] select,
    [data-testid="stSidebar"] select option {
        color: #1e293b !important;
    }
    
    /* Fix dropdown menu visibility for light mode */
    .stSelectbox select,
    .stSelectbox select option,
    .stMultiSelect select,
    .stMultiSelect select option,
    [data-testid="stSelectbox"] select,
    [data-testid="stSelectbox"] select option,
    [data-testid="stMultiSelect"] select,
    [data-testid="stMultiSelect"] select option,
    .css-1pahdxg select,
    .css-1pahdxg select option,
    .css-1lcbmhc select,
    .css-1lcbmhc select option,
    .css-1d391kg select,
    .css-1d391kg select option {
        color: #000000 !important;
        background: #ffffff !important;
        font-weight: 600 !important;
    }
    
    /* Dark mode dropdown fixes */
    body[data-theme="dark"] .stSelectbox select,
    body[data-theme="dark"] .stSelectbox select option,
    body[data-theme="dark"] .stMultiSelect select,
    body[data-theme="dark"] .stMultiSelect select option,
    body[data-theme="dark"] [data-testid="stSelectbox"] select,
    body[data-theme="dark"] [data-testid="stSelectbox"] select option,
    body[data-theme="dark"] [data-testid="stMultiSelect"] select,
    body[data-theme="dark"] [data-testid="stMultiSelect"] select option,
    body[data-theme="dark"] .css-1pahdxg select,
    body[data-theme="dark"] .css-1pahdxg select option,
    body[data-theme="dark"] .css-1lcbmhc select,
    body[data-theme="dark"] .css-1lcbmhc select option,
    body[data-theme="dark"] .css-1d391kg select,
    body[data-theme="dark"] .css-1d391kg select option {
        color: #f1f5f9 !important;
        background: #1e293b !important;
        font-weight: 600 !important;
    }
    
    /* Darken sidebar collapse/expand button in light mode */
    .stSidebar button[title="Collapse sidebar"],
    .stSidebar button[aria-label="Collapse sidebar"],
    .stSidebar button[data-testid="stSidebarToggle"],
    .stSidebar .css-17ziqus,
    .stSidebar .css-1lcbmhc button,
    .stSidebar .css-1d391kg button,
    [data-testid="stSidebar"] button[title*="sidebar"],
    [data-testid="stSidebar"] button[aria-label*="sidebar"],
    .stSidebar button:not([data-testid]),
    .stSidebar button[kind="header"],
    .stSidebar button[aria-expanded],
    .stSidebar button[aria-controls] {
        background: #000000 !important;
        border: 2px solid #000000 !important;
        color: white !important;
        border-radius: 4px !important;
        opacity: 1 !important;
        visibility: visible !important;
        display: block !important;
        min-width: 20px !important;
        min-height: 20px !important;
        width: 20px !important;
        height: 20px !important;
    }
    
    /* Target all buttons in sidebar header area */
    .stSidebar > div:first-child button,
    .stSidebar > div > div > button,
    .stSidebar [data-testid="element-container"] button {
        background: #000000 !important;
        border: 2px solid #000000 !important;
        color: white !important;
        border-radius: 4px !important;
        opacity: 1 !important;
        visibility: visible !important;
    }
    
    /* Dark mode sidebar toggle button */
    body[data-theme="dark"] .stSidebar button[title="Collapse sidebar"],
    body[data-theme="dark"] .stSidebar button[aria-label="Collapse sidebar"],
    body[data-theme="dark"] .stSidebar button[data-testid="stSidebarToggle"],
    body[data-theme="dark"] .stSidebar .css-17ziqus,
    body[data-theme="dark"] .stSidebar .css-1lcbmhc button,
    body[data-theme="dark"] .stSidebar .css-1d391kg button {
        background: #475569 !important;
        border: 1px solid #475569 !important;
        color: white !important;
    }
    """

PENDING_REVIEW_BADGE_CSS = """
    .yt-bell-widget__hint {
        margin: 0.15rem 0 0.85rem 0;
        padding: 0 0.25rem;
        font-size: 0.78rem;
        line-height: 1.35;
        text-align: right;
        color: var(--text-secondary);
        font-style: italic;
    }
    .nav-pending-float {
        position: relative;
        height: 0;
        overflow: visible;
        z-index: 30;
        pointer-events: none;
        margin: 0;
        padding: 0;
    }
    .nav-pending-float--after {
        margin-top: -2.85rem;
        margin-bottom: 0.55rem;
        text-align: right;
        padding-right: 0.65rem;
    }
    .nav-pending-float--header {
        margin: 0.25rem 0 0.15rem 0;
        text-align: right;
        padding-right: 0.5rem;
    }
    .nav-pending-float__badge {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-width: 1.35rem;
        height: 1.35rem;
        padding: 0 0.35rem;
        border-radius: 50%;
        background: #ff0000;
        color: #ffffff;
        font-size: 0.68rem;
        font-weight: 800;
        line-height: 1;
        text-align: center;
        border: 2px solid #ffffff;
        box-shadow: 0 2px 6px rgba(220, 38, 38, 0.55);
        letter-spacing: -0.02em;
        box-sizing: border-box;
    }
    .nav-pending-float__badge--header {
        min-width: 1.5rem;
        height: 1.5rem;
        font-size: 0.72rem;
    }
    .balance-with-bf {
        text-align: right;
        line-height: 1.2;
        white-space: nowrap;
    }
    .co-sup-link {
        color: var(--primary);
        text-decoration: none;
        font-weight: 700;
        font-size: 0.72rem;
    }
    .co-sup-link:hover {
        text-decoration: underline;
    }
    .student-records-table-wrap {
        overflow-x: auto;
        margin: 0.5rem 0 1rem 0;
    }
    table.student-records-table {
        width: 100%;
        border-collapse: collapse;
        font-size: 0.875rem;
    }
    table.student-records-table th,
    table.student-records-table td {
        border: 1px solid var(--border);
        padding: 0.45rem 0.6rem;
        text-align: left;
        vertical-align: top;
    }
    table.student-records-table th {
        background: var(--surface);
        font-weight: 600;
        color: var(--text);
    }
    table.student-records-table tr:nth-child(even) td {
        background: rgba(0, 0, 0, 0.02);
    }
"""

css_content = """<style>
""" + css_theme + PENDING_REVIEW_BADGE_CSS + """
    
    /* Main header - VineLedger Branding */
    .main-header {
        font-size: 3.5rem;
        font-weight: 800;
        color: var(--text);
        text-align: center;
        padding: 3rem 0 2rem 0;
        margin-bottom: 2rem;
        background: linear-gradient(135deg, var(--primary) 0%, var(--accent) 50%, var(--secondary) 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
        text-shadow: 0 0 40px rgba(99, 102, 241, 0.3);
        letter-spacing: -0.02em;
        position: relative;
    }
    
    .main-header::before {
        content: '🌿';
        position: absolute;
        left: -60px;
        top: 50%;
        transform: translateY(-50%);
        font-size: 2rem;
        opacity: 0.7;
    }
    
    .main-header::after {
        content: 'Dashboard';
        position: absolute;
        right: -60px;
        top: 50%;
        transform: translateY(-50%);
        font-size: 2rem;
        opacity: 0.7;
    }
    
    /* Sub-header */
    .sub-header {
        text-align: center;
        color: var(--text-secondary);
        font-size: 1.1rem;
        margin-bottom: 2rem;
        font-weight: 500;
    }
    
    /* Section headers */
    .section-header {
        font-size: 1.875rem;
        font-weight: 700;
        color: var(--text);
        margin: 2.5rem 0 1.5rem 0;
        padding-bottom: 1rem;
        border-bottom: 2px solid var(--border);
        position: relative;
        display: flex;
        align-items: center;
        gap: 0.75rem;
    }
    
    .section-header::before {
        content: '';
        width: 4px;
        height: 24px;
        background: linear-gradient(135deg, var(--primary) 0%, var(--accent) 100%);
        border-radius: 2px;
    }
    
    .section-header::after {
        content: '';
        position: absolute;
        bottom: -2px;
        left: 0;
        width: 80px;
        height: 2px;
        background: linear-gradient(90deg, var(--primary) 0%, var(--accent) 100%);
        border-radius: 1px;
    }
    
    /* Enhanced metric cards */
    .metric-card {
        background: linear-gradient(135deg, var(--surface) 0%, var(--surface-alt) 100%);
        padding: 2rem;
        border-radius: 1.25rem;
        border: 1px solid var(--border);
        margin: 1rem 0;
        box-shadow: 0 10px 25px -5px var(--shadow), 0 4px 6px -2px var(--shadow);
        transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
        border-left: 4px solid var(--primary);
        position: relative;
        overflow: hidden;
    }
    
    .metric-card::before {
        content: '';
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        height: 1px;
        background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.1), transparent);
    }
    
    .metric-card:hover {
        transform: translateY(-4px) scale(1.02);
        box-shadow: 0 20px 40px -10px var(--shadow), 0 8px 16px -4px var(--shadow);
        border-left-color: var(--accent);
    }

    a.metric-card-link {
        display: block;
        text-decoration: none;
        color: inherit;
        cursor: pointer;
    }
    a.metric-card-link .metric-card {
        margin: 1rem 0;
    }
    a.metric-card-link:hover .metric-card {
        transform: translateY(-4px) scale(1.02);
        box-shadow: 0 20px 40px -10px var(--shadow), 0 8px 16px -4px var(--shadow);
        border-left-color: var(--accent);
    }

    /* View Students: grade / club category picker cards */
    .picker-card {
        --picker-accent: var(--primary);
        padding: 1.5rem 1.25rem 1.25rem;
        margin: 0.5rem 0;
        background: linear-gradient(135deg, var(--surface) 0%, var(--surface-alt) 100%);
        border-radius: 1rem;
        border: 1px solid var(--border);
        border-left: 4px solid var(--picker-accent);
        transition: transform 0.3s ease, box-shadow 0.3s ease;
    }

    .picker-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
    }

    .picker-card__eyebrow {
        font-size: 0.7rem;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--text-secondary);
        margin-bottom: 0.35rem;
    }

    .picker-card__title {
        font-size: 1.4rem;
        font-weight: 800;
        line-height: 1.25;
        color: var(--picker-accent);
        margin-bottom: 0.75rem;
        word-break: break-word;
        display: block;
    }

    .picker-card__title--split {
        display: flex;
        align-items: baseline;
        flex-wrap: wrap;
        gap: 0.15rem 0.35rem;
    }

    .picker-card__title-prefix {
        font-size: 1rem;
        font-weight: 600;
        color: var(--text-secondary);
        letter-spacing: 0.02em;
    }

    .picker-card__title-num {
        font-size: 2.25rem;
        font-weight: 800;
        color: var(--picker-accent);
        line-height: 1;
    }

    .picker-card__title-emoji {
        font-size: 1.35rem;
        margin-right: 0.15rem;
    }

    .picker-card--has-bg {
        position: relative;
        overflow: hidden;
    }

    .picker-card__content {
        position: relative;
        z-index: 1;
    }

    .picker-card__bg {
        position: absolute;
        top: 0;
        right: 0;
        bottom: 0;
        width: 58%;
        opacity: 0.42;
        background-size: cover;
        background-position: center;
        background-repeat: no-repeat;
        pointer-events: none;
        z-index: 0;
    }

    .picker-card__bg::after {
        content: "";
        position: absolute;
        inset: 0;
        background: linear-gradient(to right, var(--surface) 0%, transparent 70%);
        pointer-events: none;
    }

    .picker-card__divider {
        height: 1px;
        background: var(--border);
        margin-bottom: 0.75rem;
    }

    .picker-card__stat-row {
        display: flex;
        align-items: baseline;
        gap: 0.5rem;
        flex-wrap: wrap;
    }

    .picker-card__count {
        font-size: 1.5rem;
        font-weight: 700;
        color: var(--text);
        line-height: 1;
    }

    .picker-card__count-label {
        font-size: 0.8rem;
        font-weight: 500;
        color: var(--text-secondary);
    }

    /* Enhanced message containers */
    .success-message {
        background: linear-gradient(135deg, rgba(16, 185, 129, 0.15) 0%, rgba(16, 185, 129, 0.05) 100%);
        border: 1px solid rgba(16, 185, 129, 0.3);
        padding: 1.25rem 2rem 1.25rem 3.5rem;
        border-radius: 1rem;
        color: var(--success);
        font-weight: 600;
        box-shadow: 0 4px 12px rgba(16, 185, 129, 0.15);
        position: relative;
        overflow: hidden;
    }

    .success-message::before {
        content: '✓';
        position: absolute;
        left: 1rem;
        top: 50%;
        transform: translateY(-50%);
        width: 24px;
        height: 24px;
        background: var(--success);
        color: white;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 14px;
        font-weight: bold;
    }
    
    .info-message {
        background: linear-gradient(135deg, rgba(59, 130, 246, 0.15) 0%, rgba(59, 130, 246, 0.05) 100%);
        border: 1px solid rgba(59, 130, 246, 0.3);
        padding: 1.25rem 2rem 1.25rem 3.5rem;
        border-radius: 1rem;
        color: var(--info);
        font-weight: 600;
        box-shadow: 0 4px 12px rgba(59, 130, 246, 0.15);
        position: relative;
        overflow: hidden;
    }
    
    .info-message::before {
        content: 'ℹ';
        position: absolute;
        left: 1rem;
        top: 50%;
        transform: translateY(-50%);
        width: 24px;
        height: 24px;
        background: var(--info);
        color: white;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 14px;
        font-weight: bold;
        content: '⚠';
        position: absolute;
        left: 1rem;
        top: 50%;
        transform: translateY(-50%);
        width: 24px;
        height: 24px;
        background: var(--warning);
        color: white;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 14px;
        font-weight: bold;
    }
    
    /* Enhanced form and data containers */
    .form-container {
        background: linear-gradient(135deg, var(--surface) 0%, var(--surface-alt) 100%);
        padding: 2.5rem;
        border-radius: 1.5rem;
        border: 1px solid var(--border);
        margin: 2rem 0;
        box-shadow: 0 10px 25px -5px var(--shadow), 0 4px 6px -2px var(--shadow);
        backdrop-filter: blur(10px);
        position: relative;
    }
    
    .form-container::before {
        content: '';
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        height: 1px;
        background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.1), transparent);
    }
    
    .dataframe-container {
        background: linear-gradient(135deg, var(--surface) 0%, var(--surface-alt) 100%);
        padding: 2rem;
        border-radius: 1.5rem;
        border: 1px solid var(--border);
        margin: 2rem 0;
        box-shadow: 0 10px 25px -5px var(--shadow), 0 4px 6px -2px var(--shadow);
        backdrop-filter: blur(10px);
        overflow: hidden;
    }
    
    /* Enhanced Streamlit component overrides */
    .stButton > button {
        background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
        color: white !important;
        border: none;
        padding: 0.75rem 2rem;
        font-weight: 700;
        border-radius: 0.75rem;
        transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
        box-shadow: 0 4px 12px rgba(99, 102, 241, 0.2);
        font-size: 0.95rem;
        letter-spacing: 0.025em;
        position: relative;
        overflow: hidden;
    }
    
    /* Enhanced text visibility for all labels and headers */
    h1, h2, h3, h4, h5, h6 {
        color: var(--text) !important;
        font-weight: 600;
    }
    
    /* Better contrast for form labels */
    .stSelectbox > div > div > label,
    .stTextInput > div > div > label,
    .stNumberInput > div > div > label,
    .stTextArea > div > div > label,
    .stCheckbox > div > div > label {
        color: var(--text) !important;
        font-weight: 500 !important;
    }
    
    /* Enhanced text input visibility */
    .stTextInput > div > div > input,
    .stNumberInput > div > div > input,
    .stTextArea > div > div > textarea,
    .stSelectbox > div > div > div {
        color: var(--text) !important;
        background: var(--surface) !important;
        border: 1px solid var(--border) !important;
        padding: 0.5rem !important;
        border-radius: 0.5rem !important;
    }
    
    /* Fix barely visible input field labels - Streamlit specific */
    .stTextInput > div[data-testid="stBaseTextInput"] > div > div > label,
    .stNumberInput > div[data-testid="stBaseNumberInput"] > div > div > label,
    .stSelectbox > div[data-testid="stBaseSelect"] > div > div > label,
    .stTextArea > div[data-testid="stBaseTextArea"] > div > div > label,
    .stCheckbox > div[data-testid="stBaseCheckbox"] > div > div > label {
        color: var(--text) !important;
        font-weight: 600 !important;
        opacity: 1 !important;
        font-size: 0.875rem !important;
        margin-bottom: 0.5rem !important;
    }
    
    /* Additional label targeting for maximum coverage */
    .stTextInput label,
    .stNumberInput label,
    .stSelectbox label,
    .stTextArea label,
    .stCheckbox label,
    .stFileUploader label,
    .stDateInput label {
        color: var(--text) !important;
        font-weight: 600 !important;
        opacity: 1 !important;
    }
    
    /* Target Streamlit's internal label containers */
    div[data-baseweb="select"] > div > div > label,
    div[data-baseweb="input"] > div > div > label {
        color: var(--text) !important;
        font-weight: 600 !important;
    }
    
    /* Maximum coverage for body text (captions use informative styling below) */
    .stText, .stMarkdown {
        color: var(--text) !important;
    }
    
    /* Fix any remaining light gray text */
    span, p, div:not([class*="metric"]):not([class*="status"]) {
        color: var(--text) !important;
    }
    
    /* Widget help tooltips + native captions (captions also get end-of-sheet rules for size/italic) */
    .stHelpText, .stTooltipText {
        color: var(--text-secondary) !important;
        font-size: 0.78rem !important;
        font-style: italic !important;
        opacity: 0.95 !important;
    }
    
    /* Force override any gray text */
    [style*="color: rgb"] {
        color: var(--text) !important;
    }
    
    [style*="color: #"] {
        color: var(--text) !important;
    }
    
    .stButton > button::before {
        content: '';
        position: absolute;
        top: 0;
        left: -100%;
        width: 100%;
        height: 100%;
        background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.2), transparent);
        transition: left 0.5s;
    }
    
    .stButton > button:hover::before {
        left: 100%;
    }
    
    .stButton > button:hover {
        background: linear-gradient(135deg, var(--primary-dark) 0%, var(--accent) 100%);
        transform: translateY(-2px);
        box-shadow: 0 8px 20px rgba(99, 102, 241, 0.3);
    }
    
    .stButton > button:active {
        transform: translateY(0);
        box-shadow: 0 4px 12px rgba(99, 102, 241, 0.2);
    }
    
    .stSelectbox > div > div {
        background: var(--surface) !important;
        color: var(--text) !important;
        border-radius: 0.5rem;
        border: 1px solid var(--border);
    }
    
    .stSelectbox > div > div > div {
        color: var(--text) !important;
    }
    
    /* FIXED TEXT INPUT VISIBILITY */
    .stTextInput > div > div > input,
    .stNumberInput > div > div > input,
    .stTextArea > div > div > textarea {
        border-radius: 0.5rem;
        border: 1px solid var(--border);
        background: var(--surface) !important;
        color: var(--text) !important;
        padding: 0.5rem !important;
    }
    
    .stTextInput > div > div > input::placeholder,
    .stNumberInput > div > div > input::placeholder,
    .stTextArea > div > div > textarea::placeholder {
        color: var(--gray) !important;
        opacity: 0.7;
    }
    
    /* Data table styling */
    .dataframe {
        background: var(--surface) !important;
        color: var(--text) !important;
    }
    
    .dataframe th {
        background: var(--surface) !important;
        color: var(--text) !important;
        font-weight: 600;
    }
    
    .dataframe td {
        background: var(--surface) !important;
        color: var(--text) !important;
    }
    
    .dataframe tr:hover {
        background: var(--light) !important;
    }
    
    /* Main content area background fixes */
    .main .block-container {
        background: var(--background) !important;
        min-height: 100vh;
    }
    
    /* Chart styling */
    .stPlotlyChart {
        background: var(--surface) !important;
    }
"""
if theme == "Light":
    css_content += """
    /* Light theme (append): BaseWeb dropdowns after global rules so list options stay readable */
    div[data-baseweb="popover"],
    div[data-baseweb="popover"] ul,
    ul[role="listbox"] {
        background-color: #ffffff !important;
        background-image: none !important;
        color: #0f172a !important;
        border: 1px solid #cbd5e1 !important;
        box-shadow: 0 8px 24px rgba(15, 23, 42, 0.14) !important;
    }
    div[data-baseweb="popover"] li,
    li[role="option"] {
        color: #0f172a !important;
        background-color: #ffffff !important;
        background-image: none !important;
    }
    div[data-baseweb="popover"] li *,
    li[role="option"] * {
        color: #0f172a !important;
        -webkit-text-fill-color: #0f172a !important;
    }
    li[role="option"]:hover,
    li[role="option"][aria-selected="true"],
    li[role="option"][data-highlighted="true"] {
        background-color: #e2e8f0 !important;
        color: #0f172a !important;
    }
    div[data-baseweb="select"] > div,
    div[data-baseweb="select"] > div > div {
        background-color: #ffffff !important;
        color: #0f172a !important;
    }
    div[data-baseweb="select"] input,
    div[data-baseweb="select"] span,
    div[data-baseweb="select"] p {
        color: #0f172a !important;
        -webkit-text-fill-color: #0f172a !important;
        caret-color: #0f172a !important;
    }
    div[data-baseweb="select"] svg {
        fill: #0f172a !important;
    }
    div[data-baseweb="menu"],
    ul[data-baseweb="menu"] {
        background-color: #ffffff !important;
        color: #0f172a !important;
    }
    /* File uploader (light): force light dropzone + readable text (fixes dark strip in light theme) */
    [data-testid="stFileUploader"] section,
    [data-testid="stFileUploader"] [data-testid="stFileDropzone"],
    [data-testid="stFileUploader"] div[data-testid="stFileDropzone"],
    [data-testid="stFileUploader"] [data-baseweb="file-uploader"],
    [data-testid="stFileUploader"] [role="presentation"] {
        background-color: #f8fafc !important;
        background-image: none !important;
        border: 2px dashed #94a3b8 !important;
        color: #0f172a !important;
    }
    [data-testid="stFileUploader"] section *,
    [data-testid="stFileUploader"] [data-testid="stFileDropzone"] *,
    [data-testid="stFileUploader"] div[data-testid="stFileDropzone"] * {
        color: #0f172a !important;
        -webkit-text-fill-color: #0f172a !important;
    }
    [data-testid="stFileUploader"] small,
    [data-testid="stFileUploader"] p,
    [data-testid="stFileUploader"] span {
        color: #334155 !important;
        -webkit-text-fill-color: #334155 !important;
        opacity: 1 !important;
    }
    [data-testid="stFileUploader"] svg,
    [data-testid="stFileUploader"] path {
        fill: #475569 !important;
        stroke: #475569 !important;
        opacity: 1 !important;
    }
    [data-testid="stFileUploader"] button {
        background-color: #ffffff !important;
        color: #0f172a !important;
        border: 1px solid #cbd5e1 !important;
    }
    [data-testid="stFileUploader"] button:hover {
        background-color: #f1f5f9 !important;
        border-color: #94a3b8 !important;
    }
    /* Light: dark chrome (dataframe toolbar, upload actions) — white text/icons, not primary blue */
    section.main [data-testid="stElementToolbar"] button,
    section.main [data-testid="stElementToolbar"] [role="button"],
    section.main [data-testid="stDataFrame"] [data-testid="stElementToolbar"] button,
    section.main [data-testid="stDataFrame"] [data-testid="stElementToolbar"] [role="button"] {
        color: #f8fafc !important;
        -webkit-text-fill-color: #f8fafc !important;
        background-color: #0f172a !important;
        background-image: none !important;
        border-color: #334155 !important;
    }
    section.main [data-testid="stElementToolbar"] button:hover,
    section.main [data-testid="stDataFrame"] [data-testid="stElementToolbar"] button:hover {
        background-color: #1e293b !important;
        color: #ffffff !important;
        -webkit-text-fill-color: #ffffff !important;
    }
    section.main [data-testid="stElementToolbar"] svg,
    section.main [data-testid="stElementToolbar"] path,
    section.main [data-testid="stDataFrame"] [data-testid="stElementToolbar"] svg,
    section.main [data-testid="stDataFrame"] [data-testid="stElementToolbar"] path {
        fill: #f8fafc !important;
        stroke: #f8fafc !important;
        color: #f8fafc !important;
    }
    /* File uploader: dark “Browse / Replace” row — force light foreground */
    section.main [data-testid="stFileUploader"] [data-baseweb="button"],
    section.main [data-testid="stFileUploader"] button[kind="secondary"] {
        background-color: #0f172a !important;
        background-image: none !important;
        color: #ffffff !important;
        -webkit-text-fill-color: #ffffff !important;
        border-color: #334155 !important;
    }
    section.main [data-testid="stFileUploader"] [data-baseweb="button"] *,
    section.main [data-testid="stFileUploader"] button[kind="secondary"] * {
        color: #ffffff !important;
        -webkit-text-fill-color: #ffffff !important;
    }
    section.main [data-testid="stFileUploader"] [data-baseweb="button"] svg,
    section.main [data-testid="stFileUploader"] button[kind="secondary"] svg,
    section.main [data-testid="stFileUploader"] [data-baseweb="button"] path {
        fill: #ffffff !important;
        stroke: #ffffff !important;
    }
    section.main [data-testid="stFileUploader"] [data-baseweb="button"]:hover,
    section.main [data-testid="stFileUploader"] button[kind="secondary"]:hover {
        background-color: #1e293b !important;
        color: #ffffff !important;
    }
    /* Main primary actions: keep label/icon readable on Streamlit primary fill */
    section.main .stButton > button[kind="primary"],
    section.main .stButton > button[kind="primary"] * {
        color: #ffffff !important;
        -webkit-text-fill-color: #ffffff !important;
    }
    section.main .stButton > button[kind="primary"] svg,
    section.main .stButton > button[kind="primary"] path {
        fill: #ffffff !important;
        stroke: #ffffff !important;
    }
"""
css_content += """
    /* In-page tab bars (st.tabs): outlined segment controls so they read as navigation, not body copy */
    [data-testid="stTabs"] {
        margin: 0.5rem 0 1.25rem 0;
    }
    [data-testid="stTabs"] [role="tablist"],
    [data-testid="stTabs"] div[data-baseweb="tab-list"] {
        display: inline-flex !important;
        flex-wrap: wrap !important;
        align-items: center !important;
        gap: 0.5rem !important;
        padding: 0.35rem !important;
        background: var(--surface-alt) !important;
        border: 1px solid var(--border) !important;
        border-radius: 0.65rem !important;
        box-shadow: 0 1px 3px var(--shadow) !important;
    }
    [data-testid="stTabs"] [role="tab"],
    [data-testid="stTabs"] button[data-baseweb="tab"] {
        font-size: 0.95rem !important;
        font-weight: 600 !important;
        letter-spacing: 0.01em !important;
        padding: 0.45rem 0.95rem !important;
        margin: 0 !important;
        border-radius: 0.5rem !important;
        border: 2px solid var(--border) !important;
        background: var(--surface) !important;
        color: var(--text) !important;
        min-height: 2.45rem !important;
        line-height: 1.25 !important;
        box-shadow: 0 1px 2px var(--shadow) !important;
        transition: border-color 0.15s ease, background 0.15s ease, box-shadow 0.15s ease !important;
    }
    [data-testid="stTabs"] [role="tab"]:hover,
    [data-testid="stTabs"] button[data-baseweb="tab"]:hover {
        border-color: var(--primary-light) !important;
        background: var(--surface-alt) !important;
    }
    [data-testid="stTabs"] [role="tab"][aria-selected="true"],
    [data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {
        border-color: var(--primary) !important;
        background: linear-gradient(
            135deg,
            rgba(129, 140, 248, 0.28) 0%,
            rgba(244, 114, 182, 0.14) 100%
        ) !important;
        color: var(--text) !important;
        box-shadow: 0 0 0 2px rgba(129, 140, 248, 0.45), 0 2px 8px var(--shadow) !important;
    }
    /* Older Streamlit tab wrapper (fallback) */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0.5rem !important;
        padding: 0.35rem !important;
        border: 1px solid var(--border) !important;
        border-radius: 0.65rem !important;
        background: var(--surface-alt) !important;
    }
    .stTabs [data-baseweb="tab"] {
        font-size: 0.95rem !important;
        font-weight: 600 !important;
        border: 2px solid var(--border) !important;
        border-radius: 0.5rem !important;
        padding: 0.45rem 0.95rem !important;
        background: var(--surface) !important;
    }
    .stTabs [data-baseweb="tab"][aria-selected="true"] {
        border-color: var(--primary) !important;
        background: linear-gradient(
            135deg,
            rgba(129, 140, 248, 0.28) 0%,
            rgba(244, 114, 182, 0.14) 100%
        ) !important;
        box-shadow: 0 0 0 2px rgba(129, 140, 248, 0.45), 0 2px 8px var(--shadow) !important;
    }

    /* --- Informative / explanatory copy (last so it wins over generic text rules) --- */
    /* Status lines: billing period, calendar "today" — NOT small/italic (operational headings) */
    p.vine-status-line,
    .vine-status-line {
        font-size: 0.95rem !important;
        font-style: normal !important;
        font-weight: 500 !important;
        color: var(--text-secondary) !important;
        line-height: 1.45 !important;
        margin: 0 0 0.45rem 0 !important;
    }
    .vine-status-line strong {
        font-weight: 700 !important;
        color: var(--text) !important;
    }

    p.vine-help-text,
    div.vine-help-text,
    .vine-help-text {
        font-size: 0.8125rem !important;
        font-style: italic !important;
        color: var(--text-secondary) !important;
        line-height: 1.45 !important;
    }
    .vine-help-text code {
        font-style: normal;
        font-size: 0.78rem;
    }
    .vine-help-text strong {
        font-weight: 600 !important;
    }
    .vine-help-text.vine-help-strong {
        font-weight: 600 !important;
    }
    /* Manage clubs → Assign members: tighter explanatory copy under section headings */
    p.vine-assign-hint,
    .vine-assign-hint {
        font-size: 0.72rem !important;
        font-style: italic !important;
        color: var(--text-secondary) !important;
        line-height: 1.4 !important;
        margin: 0 0 0.45rem 0 !important;
    }
    .vine-assign-hint strong {
        font-weight: 600 !important;
        font-style: italic !important;
    }
    [data-testid="stCaption"],
    [data-testid="stCaption"] p,
    [data-testid="stCaption"] span,
    [data-testid="stCaption"] label,
    [data-testid="stCaption"] small {
        font-size: 0.8125rem !important;
        font-style: italic !important;
        color: var(--text-secondary) !important;
        font-weight: 400 !important;
        opacity: 1 !important;
    }

    /* Streamlit st.info / st.warning / st.success — instructive & explanatory copy */
    [data-testid="stAlert"],
    [data-testid="stAlert"] p,
    [data-testid="stAlert"] div[data-testid="stMarkdownContainer"],
    [data-testid="stAlert"] div[data-testid="stMarkdownContainer"] p {
        font-size: 0.88rem !important;
        font-style: italic !important;
        font-weight: 400 !important;
    }

    /* Custom inline alert banners (markdown) */
    .info-message,
    .info-message p,
    .success-message,
    .success-message p,
    .warning-message,
    .warning-message p {
        font-size: 0.9rem !important;
        font-style: italic !important;
        font-weight: 500 !important;
    }

    /* In-page tab labels: navigation, not help text */
    [data-testid="stTabs"] [role="tab"],
    [data-testid="stTabs"] button[data-baseweb="tab"],
    .stTabs [data-baseweb="tab"] {
        font-style: normal !important;
    }

    /* Primary actions in main area — not italic */
    section.main .stButton > button,
    section.main .stButton > button * {
        font-style: normal !important;
    }

    /* Field labels above inputs (required entry prompts) — upright */
    label[data-testid="stWidgetLabel"],
    label[data-testid="stWidgetLabel"] p,
    .stTextInput > div[data-testid="stBaseTextInput"] > div > div > label,
    .stNumberInput > div[data-testid="stBaseNumberInput"] > div > div > label,
    .stSelectbox > div[data-testid="stBaseSelect"] > div > div > label,
    .stTextArea > div[data-testid="stBaseTextArea"] > div > div > label,
    .stCheckbox > div[data-testid="stBaseCheckbox"] > div > div > label,
    .stFileUploader label,
    .stDateInput label,
    .stMultiSelect > label,
    .stSlider label {
        font-style: normal !important;
    }
"""
if theme == "Dark":
    css_content += """
    /* Dark: sidebar repaint last (overrides Streamlit/BaseWeb inner layers on Windows / remote viewers) */
    section[data-testid="stSidebar"],
    div[data-testid="stSidebar"],
    [data-testid="stSidebar"],
    [data-testid="stSidebarContent"],
    [data-testid="stSidebarNav"] {
        background-color: #1e293b !important;
        background-image: none !important;
        color-scheme: dark;
    }
    [data-testid="stSidebar"] > div,
    [data-testid="stSidebar"] > div > div {
        background-color: #1e293b !important;
        background-image: none !important;
    }
    """
css_content += "</style>"

st.markdown(css_content, unsafe_allow_html=True)

conn = init_db()
render_global_gate(conn)
ensure_pending_reviews_loaded(conn)
invalidate_student_cache()

# Display logo and header
col_logo, col_title = st.columns([1, 8])
with col_logo:
    _header_logo = LOGO_WEBP if theme == "Dark" else LOGO_HEADER_LIGHT
    if _header_logo.is_file():
        st.image(str(_header_logo), width=80)
with col_title:
    st.markdown('<h1 class="main-header" style="text-align: left; padding: 0; margin: 0;">VineLedger</h1>', unsafe_allow_html=True)
    st.markdown('<p class="sub-header" style="text-align: left; margin: 0;">Comprehensive School Expense & Revenue Management System</p>', unsafe_allow_html=True)

# Theme switcher in sidebar
if st.session_state.get("gate_user"):
    st.sidebar.markdown("### Session")
    st.sidebar.caption(f"Signed in as **{st.session_state.gate_user}**")
    if st.sidebar.button("Sign out", key="gate_sign_out_btn"):
        log_gate_event(conn, str(st.session_state.gate_user), "logout", None)
        clear_gate_session()
        st.rerun()

# Permanent sidebar navigation with buttons
st.sidebar.markdown("### Navigation")

# Store current page in session state
if 'current_page' not in st.session_state:
    st.session_state.current_page = 'Dashboard'

render_sidebar_pending_notification()

# Navigation buttons
if st.sidebar.button("Dashboard", key="nav_dashboard", use_container_width=True):
    st.session_state.current_page = 'Dashboard'
    st.rerun()

st.sidebar.markdown("**Revenue Management**")
_n_pending_pay = len(st.session_state.get("pending_manual_payment_drafts") or [])
if st.sidebar.button("Payment Management", key="nav_upload", use_container_width=True):
    st.session_state.current_page = "Payment Management"
    st.rerun()
render_sidebar_nav_pending_badge(_n_pending_pay)

if st.sidebar.button("Generate Receipts", key="nav_receipts", use_container_width=True):
    st.session_state.current_page = 'Generate Receipts'
    st.rerun()

if st.sidebar.button("Payment History", key="nav_history", use_container_width=True):
    st.session_state.current_page = 'Payment History'
    st.rerun()

st.sidebar.markdown("**Manage Personnel**")
if st.sidebar.button("Add Student", key="nav_add_student", use_container_width=True):
    st.session_state.current_page = 'Add Student'
    st.rerun()

if st.sidebar.button("View Students", key="nav_view_students", use_container_width=True):
    st.session_state.current_page = 'View Students'
    st.session_state.view_students_category = None
    st.session_state.selected_grade = None
    st.session_state.selected_cc_club = None
    st.session_state.pop("carry_on_student_id", None)
    st.rerun()

_n_pending_manage = manage_students_pending_draft_count()
if st.sidebar.button("Manage Students", key="nav_manage_students", use_container_width=True):
    st.session_state.current_page = 'Manage Students'
    st.rerun()
render_sidebar_nav_pending_badge(_n_pending_manage)

if st.session_state.get("gate_user") and not gate_user_can_access_hidden_tabs():
    st.session_state.sidebar_staff_revealed = False

st.sidebar.markdown("**Other**")
if st.session_state.get("sidebar_staff_revealed"):
    if st.sidebar.button("Hide other tabs", key="nav_staff_hide", use_container_width=True):
        st.session_state.sidebar_staff_revealed = False
        if st.session_state.current_page in _STAFF_PAGES_MENU:
            st.session_state.current_page = "Dashboard"
        st.rerun()
    if st.sidebar.button("Add Staff", key="nav_add_staff", use_container_width=True):
        st.session_state.current_page = 'Add Staff'
        st.rerun()
    if st.sidebar.button("View Staff", key="nav_view_staff", use_container_width=True):
        st.session_state.current_page = 'View Staff'
        st.rerun()
    if st.sidebar.button("Manage Staff", key="nav_manage_staff", use_container_width=True):
        st.session_state.current_page = 'Manage Staff'
        st.rerun()
else:
    st.sidebar.caption(
        "Other tools are hidden. Enter the **password for your user account** (the same password you use to sign in) to show them."
    )
    _unlock_pw = st.sidebar.text_input(
        "User password",
        type="password",
        key="sidebar_staff_unlock_pw",
        label_visibility="collapsed",
        placeholder="Your sign-in password",
    )
    if st.sidebar.button("Show other tabs", key="nav_staff_show", use_container_width=True):
        if not gate_user_can_access_hidden_tabs():
            _invalidate_admin_password_fields(
                "You do not have permission to reveal these tabs.",
                "sidebar_staff_unlock_pw",
            )
        elif verify_current_gate_login_password(_unlock_pw):
            st.session_state.sidebar_staff_revealed = True
            _clear_password_field_keys("sidebar_staff_unlock_pw")
            st.rerun()
        else:
            _invalidate_admin_password_fields(
                "Incorrect password.",
                "sidebar_staff_unlock_pw",
            )

st.sidebar.markdown("**Expense Management**")
_n_pending_exp = len(st.session_state.get("pending_expense_drafts") or [])
if st.sidebar.button("Add Expense", key="nav_add_expense", use_container_width=True):
    st.session_state.current_page = 'Add Expense'
    st.rerun()
render_sidebar_nav_pending_badge(_n_pending_exp)

if st.sidebar.button("Expense Categories & Reports", key="nav_expense_insights", use_container_width=True):
    st.session_state.current_page = 'Expense Categories & Reports'
    st.rerun()

st.sidebar.markdown("**Configuration**")
if st.sidebar.button("Fee Structure", key="nav_fee_structure", use_container_width=True):
    st.session_state.current_page = 'Fee Structure'
    st.rerun()

if st.sidebar.button("Database backup", key="nav_db_backup", use_container_width=True):
    st.session_state.current_page = "Database backup"
    st.rerun()

if st.sidebar.button("School Calendar", key="nav_school_calendar", use_container_width=True):
    st.session_state.current_page = "School Calendar"
    st.rerun()

if st.sidebar.button("School Activity", key="nav_school_activity", use_container_width=True):
    st.session_state.current_page = "School Activity"
    st.rerun()

# Add visual separator
st.sidebar.markdown("---")

# Get current page from session state
menu = st.session_state.current_page

# Deep link from (co) superscript in student tables → View Students / Carry on
_carry_q = st.query_params.get("carry_student")
if _carry_q is not None and str(_carry_q).strip():
    try:
        st.session_state.current_page = "View Students"
        st.session_state.view_students_category = "carry_on"
        st.session_state.carry_on_student_id = int(_carry_q)
        st.session_state.selected_grade = None
        st.session_state.selected_cc_club = None
        menu = "View Students"
        try:
            del st.query_params["carry_student"]
        except Exception:
            pass
    except (ValueError, TypeError):
        pass

_vs_cat_q = st.query_params.get("view_students")
if _vs_cat_q is not None and str(_vs_cat_q).strip():
    _vs_key = str(_vs_cat_q).strip()
    if _vs_key in VIEW_STUDENTS_INSIGHT_CATEGORIES:
        st.session_state.current_page = "View Students"
        st.session_state.view_students_category = _vs_key
        st.session_state.selected_grade = None
        st.session_state.selected_cc_club = None
        menu = "View Students"
        try:
            del st.query_params["view_students"]
        except Exception:
            pass

if menu in ("Expense Categories", "Expense Reports"):
    st.session_state.current_page = "Expense Categories & Reports"
    menu = st.session_state.current_page

if menu == "Upload Statement":
    st.session_state.current_page = "Payment Management"
    menu = st.session_state.current_page

if menu in _STAFF_PAGES_MENU and (
    not st.session_state.get("sidebar_staff_revealed", False)
    or not gate_user_can_access_hidden_tabs()
):
    st.session_state.current_page = "Dashboard"
    menu = "Dashboard"

_vine_flash_err = st.session_state.pop("_vine_app_flash_error", None)
if _vine_flash_err:
    st.error(_vine_flash_err)
_vine_flash_warn = st.session_state.pop("_vine_app_flash_warn", None)
if _vine_flash_warn:
    st.warning(_vine_flash_warn)

if menu == "Dashboard":
    st.markdown('<h2 class="section-header">Financial Overview</h2>', unsafe_allow_html=True)

    _auto_msgs = run_calendar_automation_if_due(conn)
    if _auto_msgs:
        for _am in _auto_msgs:
            st.markdown(f'<div class="success-message">{_am}</div>', unsafe_allow_html=True)

    for _alert in get_dashboard_calendar_alerts(conn):
        _cls = {
            "warning": "warning-message",
            "info": "info-message",
            "success": "success-message",
        }.get(_alert["level"], "info-message")
        st.markdown(f'<div class="{_cls}">{_alert["message"]}</div>', unsafe_allow_html=True)

    _cur_term = get_current_term(conn)
    if _cur_term:
        st.markdown(
            f'<p class="vine-status-line">Current billing period: <strong>{html_module.escape(str(_cur_term["label"]))}</strong></p>',
            unsafe_allow_html=True,
        )

    # Get key metrics (operational counts use Active enrolment only)
    students_df = pd.read_sql("SELECT * FROM students", conn)
    active_students_df = (
        students_df.loc[active_students_mask(students_df)].copy()
        if not students_df.empty
        else students_df
    )

    # Key metrics with better styling (fee totals moved to Expense Categories & Reports)
    col1, col2, col3 = st.columns(3)
    col4, col5, col6 = st.columns(3)

    transport_users = (
        int(active_students_df["has_transport"].sum())
        if not active_students_df.empty and "has_transport" in active_students_df.columns
        else 0
    )
    meal_program_students = (
        sum(student_row_bool(r, "has_meal", False) for _, r in active_students_df.iterrows())
        if not active_students_df.empty
        else 0
    )
    _adm_term = count_new_admissions_this_term(conn)
    _adm_year = count_new_admissions_this_year(conn)
    _exit_term = count_student_exits_this_term(conn)
    _exit_year = count_student_exits_this_year(conn)
    _graduated_n = count_graduated_students(conn)

    with col1:
        _enrol_sub = (
            f"{_graduated_n} graduated on file (archived, not enrolled)"
            if _graduated_n
            else "Current enrolment only"
        )
        st.markdown(f"""
        <div class="metric-card" style="border-left-color: var(--primary);">
            <div style="font-size: 1.5rem; font-weight: 700; color: var(--primary);">{len(active_students_df)}</div>
            <div class="vine-help-text" style="margin-top: 0.25rem;">Active students</div>
            <div class="vine-help-text" style="margin-top: 0.15rem;">{_enrol_sub}</div>
        </div>
        """, unsafe_allow_html=True)

    with col2:
        st.markdown(
            _metric_card_nav_link(
                "transport",
                f"""
        <div class="metric-card" style="border-left-color: var(--info);">
            <div style="font-size: 1.5rem; font-weight: 700; color: var(--info);">{transport_users}</div>
            <div class="vine-help-text" style="margin-top: 0.25rem;">Transport Users</div>
        </div>
        """,
            ),
            unsafe_allow_html=True,
        )

    with col3:
        st.markdown(
            _metric_card_nav_link(
                "meal_program",
                f"""
        <div class="metric-card" style="border-left-color: var(--success);">
            <div style="font-size: 1.5rem; font-weight: 700; color: var(--success);">{meal_program_students}</div>
            <div class="vine-help-text" style="margin-top: 0.25rem;">Meal program students</div>
        </div>
        """,
            ),
            unsafe_allow_html=True,
        )

    with col4:
        st.markdown(
            _metric_card_nav_link(
                "new_admissions_term",
                f"""
        <div class="metric-card" style="border-left-color: #8b5cf6;">
            <div style="font-size: 1.5rem; font-weight: 700; color: #8b5cf6;">{_adm_term}</div>
            <div class="vine-help-text" style="margin-top: 0.25rem;">New admissions (this term)</div>
        </div>
        """,
            ),
            unsafe_allow_html=True,
        )

    with col5:
        st.markdown(
            _metric_card_nav_link(
                "new_admissions_year",
                f"""
        <div class="metric-card" style="border-left-color: #06b6d4;">
            <div style="font-size: 1.5rem; font-weight: 700; color: #06b6d4;">{_adm_year}</div>
            <div class="vine-help-text" style="margin-top: 0.25rem;">New admissions (this year)</div>
        </div>
        """,
            ),
            unsafe_allow_html=True,
        )

    with col6:
        st.markdown(
            _metric_card_nav_link(
                "student_exits_term",
                f"""
        <div class="metric-card" style="border-left-color: #f59e0b;">
            <div style="font-size: 1.5rem; font-weight: 700; color: #f59e0b;">{_exit_term}</div>
            <div class="vine-help-text" style="margin-top: 0.25rem;">Student exits (this term)</div>
            <div class="vine-help-text" style="margin-top: 0.15rem;">{_exit_year} this school year · transfer or scheduled deletion</div>
        </div>
        """,
            ),
            unsafe_allow_html=True,
        )

    st.markdown('<h3 class="section-header">Grade Distribution</h3>', unsafe_allow_html=True)
    if not active_students_df.empty:
        grade_counts = active_students_df["grade"].value_counts()
        _grade_order = sort_grade_labels(list(grade_counts.index))
        grade_series = grade_counts.reindex([g for g in _grade_order if g in grade_counts.index])
        st.bar_chart(grade_series, use_container_width=True)
        with st.expander("Class progression (Playgroup → Grade 9)"):
            st.caption(GRADE_PROGRESSION_ARROWS)
    else:
        st.markdown('<div class="info-message">No student data available</div>', unsafe_allow_html=True)
    
    # Students with outstanding balances
    st.markdown('<h3 class="section-header">Outstanding Balances</h3>', unsafe_allow_html=True)
    _out_search = st.text_input(
        "Search outstanding balances",
        placeholder="Search by name, grade, parent/guardian, or student code…",
        key="dashboard_outstanding_search",
    )
    outstanding_students = active_students_df[
        active_students_df.apply(lambda r: student_balance_is_outstanding(r), axis=1)
    ].copy()
    if not outstanding_students.empty:
        _co_map_all = get_carry_on_opening_map(
            conn, outstanding_students["id"].astype(int).tolist()
        )
        outstanding_students["_has_co"] = outstanding_students["id"].astype(int).map(
            lambda sid: _co_map_all.get(int(sid), 0) > 0.01
        )
        outstanding_students = outstanding_students.sort_values(
            ["_has_co", "balance"], ascending=[False, False]
        )

    if _out_search:
        outstanding_students = _filter_students_search_df(outstanding_students, _out_search)

    if not outstanding_students.empty:
        _top_out = outstanding_students.head(10)
        st.markdown('<div class="dataframe-container">', unsafe_allow_html=True)
        _oh = (
            "<table class='student-records-table'><thead><tr>"
            "<th>Name</th><th>Grade</th><th>Parent/Guardian</th><th>Balance</th>"
            "</tr></thead><tbody>"
        )
        for _, _or in _top_out.iterrows():
            _sid = int(_or["id"])
            _oh += (
                f"<tr><td>{html_module.escape(str(_or['name']))}</td>"
                f"<td>{html_module.escape(str(_or['grade']))}</td>"
                f"<td>{html_module.escape(str(_or['parent_name']))}</td>"
                f"<td>{format_balance_cell_html(_or.get('balance'), _sid, _co_map_all.get(_sid, 0), balance_status=_or.get('balance_status'), balance_set=_or.get('balance_set'))}</td></tr>"
            )
        _oh += "</tbody></table>"
        if any(v > 0.01 for v in _co_map_all.values()):
            st.caption(f"Click **{CO_SUPERSCRIPT}** above a balance for carry-on details. Students with carry-on appear first.")
        st.markdown(_oh, unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

        # Show summary
        total_outstanding = sum_outstanding_balance_rows(outstanding_students)
        st.markdown(f'<div class="warning-message"><strong>{len(outstanding_students)} students</strong> with outstanding balances totaling <strong>KSH {total_outstanding:,.0f}</strong></div>', unsafe_allow_html=True)
    elif _out_search and active_students_df.apply(
        lambda r: student_balance_is_outstanding(r), axis=1
    ).any():
        st.info("No outstanding balances match your search.")
    else:
        st.markdown('<div class="success-message">All students have paid their fees!</div>', unsafe_allow_html=True)

elif menu == "Manage Students":
    _student_flash = st.session_state.pop("_student_flash_msg", None)
    if _student_flash:
        st.success(_student_flash)

    st.markdown('<h2 class="section-header">Student Management</h2>', unsafe_allow_html=True)

    _n_stu_pend = manage_students_pending_draft_count()
    _tab_find_lbl = "Find & edit students"
    _tab_meal_lbl = "Manage meal program"
    _tab_transport_lbl = "Manage transport users"
    _tab_clubs_lbl = "Manage clubs"
    _tab_grade_lbl = "Manage grade"
    _tab_balance_lbl = "Manage balance"
    _tab_carry_lbl = "Carry on"
    _tab_pend_lbl = pending_review_tab_label(_n_stu_pend)
    (
        tab_find,
        tab_meal,
        tab_transport,
        tab_clubs,
        tab_grade,
        tab_balance,
        tab_carry,
        tab_pending,
    ) = st.tabs(
        [
            _tab_find_lbl,
            _tab_meal_lbl,
            _tab_transport_lbl,
            _tab_clubs_lbl,
            _tab_grade_lbl,
            _tab_balance_lbl,
            _tab_carry_lbl,
            _tab_pend_lbl,
        ]
    )

    with tab_pending:
        _render_pending_reviews_tab(conn)

    with tab_meal:
        _render_manage_meal_tab(conn)

    with tab_transport:
        _render_manage_transport_tab(conn)

    with tab_clubs:
        _render_manage_clubs_tab(conn)

    with tab_grade:
        _render_manage_grade_tab(conn)

    with tab_balance:
        _render_manage_balance_tab(conn)

    with tab_carry:
        _render_carry_on_tab(conn)

    with tab_find:
        # Get all students
        students_df = pd.read_sql("SELECT * FROM students ORDER BY name", conn)

        if not students_df.empty:
            st.markdown('<div class="form-container">', unsafe_allow_html=True)
            st.markdown('<h3 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Find a student</h3>', unsafe_allow_html=True)

            _uniq_grades = sorted(
                students_df["grade"].dropna().astype(str).unique().tolist(),
                key=lambda g: (GRADE_CHOICES_EDIT.index(g) if g in GRADE_CHOICES_EDIT else 999, g),
            )
            _grade_opts = ["All grades"] + _uniq_grades
            _status_opts = ["All statuses"] + sorted(
                students_df.get("status", pd.Series(dtype=str)).fillna("Active").astype(str).unique().tolist()
            )

            c_f1, c_f2, c_f3 = st.columns((2.2, 1.1, 1.1))
            with c_f1:
                st.text_input(
                    "Search",
                    placeholder="Name, student code, parent/guardian name, or phone…",
                    key="manage_students_search",
                )
            with c_f2:
                st.selectbox("Grade", _grade_opts, key="manage_students_grade_filter")
            with c_f3:
                st.selectbox("Status", _status_opts, key="manage_students_status_filter")

            _q = (st.session_state.get("manage_students_search") or "").strip().lower()
            _gf = st.session_state.get("manage_students_grade_filter", "All grades")
            _sf = st.session_state.get("manage_students_status_filter", "All statuses")

            filtered_ms = students_df
            if _gf != "All grades":
                filtered_ms = filtered_ms[filtered_ms["grade"].astype(str) == _gf]
            if _sf != "All statuses" and "status" in filtered_ms.columns:
                filtered_ms = filtered_ms[filtered_ms["status"].fillna("Active").astype(str) == _sf]
            if _q:
                _code_disp = filtered_ms["student_code"].map(_student_code_display_cell).fillna("").astype(str).str.lower()
                _m = (
                    filtered_ms["name"].fillna("").astype(str).str.lower().str.contains(_q, regex=False)
                    | filtered_ms["student_code"].fillna("").astype(str).str.lower().str.contains(_q, regex=False)
                    | _code_disp.str.contains(_q, regex=False)
                    | filtered_ms["parent_name"].fillna("").astype(str).str.lower().str.contains(_q, regex=False)
                    | filtered_ms["parent_phone"].fillna("").astype(str).str.lower().str.contains(_q, regex=False)
                    | filtered_ms["parent2_name"].fillna("").astype(str).str.lower().str.contains(_q, regex=False)
                    | filtered_ms["parent2_phone"].fillna("").astype(str).str.lower().str.contains(_q, regex=False)
                )
                _qn = normalize_kenya_msisdn(st.session_state.get("manage_students_search") or "")
                if _qn and len(_qn) >= 12:
                    for _pcol in ("parent_phone", "parent2_phone"):
                        _pnorm = filtered_ms[_pcol].fillna("").map(normalize_kenya_msisdn)
                        _m = _m | (_pnorm == _qn)
                elif _qn and len(_qn) >= 9:
                    for _pcol in ("parent_phone", "parent2_phone"):
                        _pnorm = filtered_ms[_pcol].fillna("").map(normalize_kenya_msisdn)
                        _m = _m | _pnorm.str.contains(_qn, regex=False)
                filtered_ms = filtered_ms[_m]

            st.caption(f"Showing **{len(filtered_ms)}** of **{len(students_df)}** students — pick one below.")

            selected_id = None
            student = None
            if filtered_ms.empty:
                st.info("No students match these filters. Clear the search or set grade/status to **All**.")
            else:
                _opts = filtered_ms["id"].astype(int).tolist()
                _lab = {
                    int(r["id"]): f"{r['name']} — {_student_code_display_cell(r.get('student_code'))} — {r['grade']} — {r.get('status', 'Active')}"
                    for _, r in filtered_ms.iterrows()
                }
                selected_id = st.selectbox(
                    "Choose a student to manage",
                    options=_opts,
                    format_func=lambda sid: _lab[int(sid)],
                    help="Narrow the list with search and filters above.",
                    key="manage_student_target_id",
                )
                student = students_df.loc[students_df["id"] == selected_id].iloc[0]

            st.markdown('</div>', unsafe_allow_html=True)

            if student is not None:

                # Student details
                _stu_co = get_current_term_carry_on(conn, int(student["id"]))
                _bal_html = format_balance_cell_html(
                    student.get("balance"),
                    int(student["id"]),
                    _stu_co,
                    balance_status=student.get("balance_status"),
                    balance_set=student.get("balance_set"),
                )
                st.markdown('<div class="dataframe-container">', unsafe_allow_html=True)
                st.markdown(
                    f'''
                <h4 style="color: var(--text); margin-bottom: 0.5rem; font-weight: 600;">Student Information</h4>
                <div style="line-height: 1.6;">
                    <strong>Name:</strong> {student['name']}<br/>
                    <strong>Student Code:</strong> {html_module.escape(_student_code_display_cell(student.get('student_code')))}<br/>
                    <strong>Grade:</strong> {student['grade']}<br/>
                    <strong>Progression:</strong> {html_module.escape(grade_progression_through(student.get('grade')) or '—')}<br/>
                    <strong>Joined:</strong> {(str(student.get('joined_date') or '')[:10] or '—')}<br/>
                    <strong>Status:</strong> <span style="color: {"#059669" if student.get('status', 'Active') == 'Active' else "#dc2626"};">{student.get('status', 'Active')}</span><br/>
                    <strong>Sponsored:</strong> {"Yes" if student_is_sponsored(student) else "No"}<br/>
                    <strong>Parent/Guardian 1:</strong> {student['parent_name']}<br/>
                    <strong>Phone:</strong> {student['parent_phone']}<br/>
                    <strong>Parent/Guardian 2:</strong> {student.get('parent2_name') or '—'}<br/>
                    <strong>Phone:</strong> {student.get('parent2_phone') or '—'}<br/>
                    <strong>Outstanding balance:</strong> {_bal_html}
                </div>
                ''',
                    unsafe_allow_html=True,
                )
                st.markdown('</div>', unsafe_allow_html=True)

                render_student_term_ledger(conn, int(student["id"]))

                # Management operations
                _stu_status = student_status_label(student)
                _editable = student_record_is_editable(student)
                if not _editable and st.session_state.get("edit_student_mode"):
                    st.session_state.edit_student_mode = False

                st.markdown('<div class="form-container">', unsafe_allow_html=True)
                st.markdown('<h3 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Management Operations</h3>', unsafe_allow_html=True)

                if _editable:
                    _pxfer = (st.session_state.get("pending_student_transfers") or {}).get(
                        int(student["id"])
                    )
                    if _pxfer:
                        st.warning(
                            "A **transfer** is queued under **Pending Reviews** (not applied yet). "
                            "Discard it there or apply with the admin password."
                        )
                    _pdel = (st.session_state.get("pending_student_deletions") or {}).get(
                        int(student["id"])
                    )
                    if _pdel:
                        st.warning(
                            "A **deletion** is queued under **Pending Reviews** (not applied yet). "
                            "Discard it there or apply with the admin password."
                        )

                if not _editable:
                    if _stu_status == "Graduated":
                        st.info(
                            "This learner has **Graduated** (completed Grade 9) and **cannot be edited**. "
                            "The record is kept for history and remains visible under **View Students**."
                        )
                        _ex = student.get("exited_at")
                        if _ex is not None and not (isinstance(_ex, float) and pd.isna(_ex)):
                            st.caption(f"Graduation date: **{str(_ex)[:10]}**")
                    else:
                        st.info(
                            f"This learner is **{_stu_status}** and **cannot be edited** here. "
                            "The record remains visible under **View Students** and in this summary until it is "
                            "permanently removed after the grace period."
                        )
                    if _stu_status == "Transferred" and (student.get("transfer_reason") or "").strip():
                        st.caption(f"Transfer note: {student.get('transfer_reason')}")
                    if (student.get("deletion_reason") or "").strip():
                        st.caption(f"Deletion reason: {student.get('deletion_reason')}")
                    _sched = student.get("deletion_scheduled")
                    if _sched is not None and not (isinstance(_sched, float) and pd.isna(_sched)):
                        try:
                            _due = pd.to_datetime(_sched)
                            st.caption(f"Scheduled permanent removal: **{_due.strftime('%Y-%m-%d')}**")
                        except (TypeError, ValueError):
                            st.caption(f"Scheduled permanent removal: **{_sched}**")

                # Edit Student Record Section
                if _editable and st.button("Edit Student Record", type="primary", use_container_width=True):
                    st.session_state.edit_student_mode = True
                    st.session_state.edit_student_reload_sid = int(student["id"])
                    clear_student_edit_widget_state(student["id"])

                if _editable and st.session_state.get("edit_student_mode", False):
                    st.markdown(
                        '<h4 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Edit Student Information</h4>',
                        unsafe_allow_html=True,
                    )
                    pdraft = (st.session_state.get("pending_student_edits") or {}).get(int(student["id"]))
                    if pdraft:
                        st.info(
                            "This student has a **saved-for-later** draft loaded into the form. Change anything you need, then "
                            "use **Save now** (password) or **Save for later** again."
                        )

                    import json


                    with st.form("edit_student_form"):
                        col1, col2 = st.columns(2)

                        with col1:
                            name = st.text_input(
                                "Student Name *",
                                value=pdraft["name"] if pdraft else student["name"],
                                help="Enter student's full name",
                            )
                            _g = student["grade"]
                            if pdraft and pdraft.get("grade"):
                                _gp = str(pdraft["grade"]).strip()
                                if _gp in GRADE_CHOICES_EDIT:
                                    _g = _gp
                            if _g is None or (isinstance(_g, float) and pd.isna(_g)) or str(_g).strip() == "":
                                _g = INCOMPLETE_GRADE_LABEL
                            else:
                                _g = str(_g).strip()
                                if _g not in GRADE_CHOICES_EDIT:
                                    _g = INCOMPLETE_GRADE_LABEL
                            _grade_idx = GRADE_CHOICES_EDIT.index(_g)
                            grade = st.selectbox(
                                "Grade *",
                                GRADE_CHOICES_EDIT,
                                index=_grade_idx,
                            )
                            parent_name = st.text_input(
                                "Parent/Guardian 1 name (optional)",
                                value=pdraft["parent_name"] if pdraft else student["parent_name"],
                                help="Optional. Leave blank if unknown.",
                            )
                            parent_phone = st.text_input(
                                "Parent/Guardian 1 phone (optional)",
                                value=pdraft["parent_phone"] if pdraft else student["parent_phone"],
                                help="Optional. Kenya: 01… / 07…, 254…, or +254…",
                            )
                            parent2_name = st.text_input(
                                "Parent/Guardian 2 name (optional)",
                                value=pdraft.get("parent2_name") if pdraft else (student.get("parent2_name") or ""),
                                help="Second parent or guardian, if applicable.",
                            )
                            parent2_phone = st.text_input(
                                "Parent/Guardian 2 phone (optional)",
                                value=pdraft.get("parent2_phone") if pdraft else (student.get("parent2_phone") or ""),
                                help="Optional second contact number.",
                            )
                            _dob_val = None
                            if pdraft and pdraft.get("date_of_birth"):
                                try:
                                    _dob_val = pd.to_datetime(pdraft["date_of_birth"]).date()
                                except (TypeError, ValueError):
                                    pass
                            elif student.get("date_of_birth") and not (
                                isinstance(student["date_of_birth"], float) and pd.isna(student["date_of_birth"])
                            ):
                                try:
                                    _dob_val = pd.to_datetime(student["date_of_birth"]).date()
                                except (TypeError, ValueError):
                                    pass
                            date_of_birth = st.date_input(
                                "Date of birth",
                                value=_dob_val,
                                min_value=date(1995, 1, 1),
                                max_value=date.today(),
                                help="Optional. Used to calculate age on student records.",
                            )
                            _age_now = student_age_from_dob(date_of_birth)
                            if _age_now is not None:
                                st.caption(f"Age: **{_age_now}** years")

                        with col2:
                            transport_items = conn.execute(
                                "SELECT id, fee_name, fee_amount FROM fee_structure WHERE fee_category='transport' ORDER BY fee_amount"
                            ).fetchall()
                            co_curricular_items = conn.execute(
                                "SELECT id, fee_name, fee_amount FROM fee_structure WHERE fee_category='co_curricular' ORDER BY fee_name"
                            ).fetchall()

                            transport_keys = ["__none__"] + [str(item[0]) for item in transport_items]
                            transport_labels = {
                                "__none__": "Does not use school transport",
                                **{str(t[0]): f"{t[1]} — KSH {t[2]:,.0f}" for t in transport_items},
                            }
                            default_tkey = "__none__"
                            if pdraft and pdraft.get("transport_choice") is not None:
                                _tk = str(pdraft["transport_choice"])
                                if _tk in transport_keys:
                                    default_tkey = _tk
                            elif bool(student.get("has_transport")) and student.get("transport_route_id") is not None:
                                try:
                                    rid = str(int(student["transport_route_id"]))
                                    if rid in transport_keys:
                                        default_tkey = rid
                                except (TypeError, ValueError):
                                    pass
                            t_index = transport_keys.index(default_tkey) if default_tkey in transport_keys else 0
                            transport_choice = st.selectbox(
                                "School transport",
                                options=transport_keys,
                                index=t_index,
                                format_func=lambda k: transport_labels.get(k, k),
                                help="Pick a route or no transport.",
                                key=f"edit_student_transport_{student['id']}",
                            )
                            has_transport = transport_choice != "__none__"
                            selected_transport_id = int(transport_choice) if has_transport else None

                            st.markdown(
                                '<p class="vine-help-text" style="margin-top: 0.5rem;">Co-Curricular Activities (KSH 3,000 each)</p>',
                                unsafe_allow_html=True,
                            )
                            existing_cc_ids = parse_co_curricular_ids(
                                pdraft.get("co_curricular_ids") if pdraft and pdraft.get("co_curricular_ids") is not None
                                else student.get("co_curricular_activities"),
                                conn=conn,
                                student_id=student["id"],
                            )
                            existing_cc_set = {int(x) for x in existing_cc_ids}
                            cc_columns = st.columns(3)
                            for idx, item in enumerate(co_curricular_items):
                                with cc_columns[idx % 3]:
                                    st.checkbox(
                                        item[1],
                                        value=int(item[0]) in existing_cc_set,
                                        key=f"edit_cc_{student['id']}_{item[0]}",
                                    )
                            _wk_meal = f"edit_meal_{student['id']}"
                            _hm = bool(pdraft["has_meal"]) if pdraft else bool(student["has_meal"])
                            if _wk_meal not in st.session_state:
                                st.session_state[_wk_meal] = _hm
                            st.checkbox(
                                meal_program_checkbox_label(conn),
                                key=_wk_meal,
                                help="Check if student takes meals at school",
                            )
                            _wk_adm = f"edit_admission_{student['id']}"
                            _ia = (
                                bool(pdraft.get("include_admission_fees"))
                                if pdraft
                                else student_row_bool(student, "include_admission_fees", False)
                            )
                            if _wk_adm not in st.session_state:
                                st.session_state[_wk_adm] = _ia
                            st.checkbox(
                                "Include one-time admission fee (KSH 1,000)",
                                key=_wk_adm,
                                help="Registration / admission fee only. Independent of interview fee.",
                            )
                            _wk_int = f"edit_interview_{student['id']}"
                            _ii = (
                                bool(pdraft.get("include_interview_fee"))
                                if pdraft
                                else student_row_bool(student, "include_interview_fee", False)
                            )
                            if _wk_int not in st.session_state:
                                st.session_state[_wk_int] = _ii
                            _iv = interview_fee_amount_for_grade(grade)
                            st.checkbox(
                                f"Include one-time interview fee (KSH {_iv:,.0f} for this class)",
                                key=_wk_int,
                                help="KSH 500 for Playgroup–Grade 6; KSH 700 for Grade 7 and above.",
                            )
                            _wk_sp = f"edit_sponsored_{student['id']}"
                            if _wk_sp not in st.session_state:
                                st.session_state[_wk_sp] = student_is_sponsored(student)
                            st.checkbox(
                                "Sponsored Student",
                                key=_wk_sp,
                                help="Fully sponsored learners are not required to pay fees; balance defaults to zero unless you change it below.",
                            )

                        _wk_bal = f"edit_balance_{student['id']}"
                        _wk_bmode = f"edit_balance_mode_{student['id']}"
                        _balance_mode_opts = [
                            BALANCE_DISPLAY_NOT_SET,
                            BALANCE_DISPLAY_CLEARED,
                            "Amount (KSH)",
                        ]
                        if _wk_bmode not in st.session_state:
                            if pdraft and pdraft.get("balance_status"):
                                _seed_st = pdraft["balance_status"]
                            else:
                                _seed_st = student_balance_status(student)
                            if _seed_st == BALANCE_STATUS_NOT_SET:
                                st.session_state[_wk_bmode] = BALANCE_DISPLAY_NOT_SET
                            elif _seed_st == BALANCE_STATUS_CLEARED:
                                st.session_state[_wk_bmode] = BALANCE_DISPLAY_CLEARED
                            else:
                                st.session_state[_wk_bmode] = "Amount (KSH)"
                        if _wk_bal not in st.session_state:
                            _bal = float(student.get("balance") or 0)
                            if pdraft and pdraft.get("balance") is not None:
                                _bal = float(pdraft["balance"])
                            st.session_state[_wk_bal] = _bal
                        st.radio(
                            "Outstanding balance",
                            options=_balance_mode_opts,
                            key=_wk_bmode,
                            horizontal=True,
                            help="**Not set** — not entered yet. **Cleared** — nothing owed. **Amount** — KSH outstanding.",
                        )
                        if st.session_state.get(_wk_bmode) == "Amount (KSH)":
                            st.number_input(
                                "Amount (KSH)",
                                min_value=0.0,
                                step=1.0,
                                format="%.0f",
                                key=_wk_bal,
                            )

                        st.caption("**Save now** writes to the database (password). **Save for later** queues under **Pending Reviews**.")
                        admin_pw = st.text_input(
                            "Admin password",
                            type="password",
                            key=f"edit_admin_pw_save_now_{student['id']}",
                            label_visibility="collapsed",
                            placeholder="Admin password (Save now only)",
                        )
                        bcol1, bcol2, bcol3 = st.columns(3)
                        with bcol1:
                            save_now = st.form_submit_button("Save now", type="primary", use_container_width=True)
                        with bcol2:
                            save_later = st.form_submit_button("Save for later", use_container_width=True)
                        with bcol3:
                            cancel_edit = st.form_submit_button("Cancel", use_container_width=True)

                        _edit_sid = int(student["id"])
                        _payload = None
                        if save_now or save_later:
                            _payload = {
                                "name": (name or "").strip(),
                                "grade": grade,
                                "parent_name": (parent_name or "").strip(),
                                "parent_phone": parent_phone or "",
                                "parent2_name": (parent2_name or "").strip(),
                                "parent2_phone": parent2_phone or "",
                                "date_of_birth": dob_to_storage(date_of_birth),
                                "transport_choice": transport_choice,
                                "has_transport": has_transport,
                                "selected_transport_id": selected_transport_id,
                                "co_curricular_ids": read_form_co_curricular_ids(
                                    "edit_cc", _edit_sid, co_curricular_items
                                ),
                                "has_meal": bool(st.session_state.get(f"edit_meal_{_edit_sid}", False)),
                                "include_admission_fees": int(
                                    bool(st.session_state.get(f"edit_admission_{_edit_sid}", False))
                                ),
                                "include_interview_fee": int(
                                    bool(st.session_state.get(f"edit_interview_{_edit_sid}", False))
                                ),
                                "is_sponsored": int(
                                    bool(st.session_state.get(f"edit_sponsored_{_edit_sid}", False))
                                ),
                                **_balance_fields_from_edit_form(_edit_sid),
                            }

                        if save_now:
                            if (e := evaluate_admin_password_input(admin_pw)) is not None:
                                _invalidate_admin_password_fields(
                                    e,
                                    f"edit_admin_pw_save_now_{student['id']}",
                                )
                            elif _payload is not None:
                                _merged = merge_student_edit_payload(conn, _edit_sid, _payload)
                                _val_errs = validate_student_edit_payload(_merged)
                                if _val_errs:
                                    _invalidate_admin_password_fields(
                                        " ".join(_val_errs),
                                        f"edit_admin_pw_save_now_{student['id']}",
                                        level="warn",
                                    )
                                else:
                                    try:
                                        persist_student_edit(conn, _edit_sid, _merged)
                                        invalidate_student_cache()
                                        _ok, _verify_errs = verify_student_edit_saved(conn, _edit_sid, _merged)
                                        remove_pending_student_edit(conn, _edit_sid)
                                        clear_student_edit_widget_state(_edit_sid)
                                        _rec_a = get_student_record(conn, _edit_sid)
                                        _rca = (_rec_a or {}).get("student_code")
                                        _sa = "" if _rca is None else str(_rca).strip()
                                        _code_a = display_student_code(_sa) if _sa else str(_edit_sid)
                                        _audit_log(
                                            conn,
                                            "Manage Student",
                                            f"Manage Student, {_code_a} ({_merged.get('name')}): saved now (direct edit).",
                                            save_mode="immediate",
                                            detail=_audit_student_payload_detail(_merged),
                                            entity_type="student",
                                            entity_id=int(_edit_sid),
                                            entity_code=str(_code_a),
                                        )
                                        _tlabel = transport_labels.get(_merged.get("transport_choice"), "No transport")
                                        _dob_disp = _merged.get("date_of_birth") or "—"
                                        if _ok:
                                            st.session_state["_student_flash_msg"] = (
                                                f"**{_merged['name']}** saved to the database. "
                                                f"Grade: {_merged['grade']} · Transport: {_tlabel} · "
                                                f"Meals: {'Yes' if _merged['has_meal'] else 'No'} · "
                                                f"Clubs: {len(_merged.get('co_curricular_ids') or [])} · "
                                                f"Sponsored: {'Yes' if _merged.get('is_sponsored') else 'No'} · "
                                                f"Balance: KSH {float(_merged['balance']):,.0f}."
                                            )
                                        else:
                                            st.session_state["_student_flash_msg"] = (
                                                f"Saved **{_merged['name']}** but verification found: "
                                                + "; ".join(_verify_errs)
                                            )
                                        st.session_state.edit_student_mode = False
                                        _clear_password_field_keys(f"edit_admin_pw_save_now_{student['id']}")
                                        st.rerun()
                                    except Exception as e:
                                        _invalidate_admin_password_fields(
                                            f"Error updating student: {str(e)}",
                                            f"edit_admin_pw_save_now_{student['id']}",
                                        )

                        if save_later and _payload is not None:
                            queue_pending_student_edit(
                                conn,
                                _edit_sid,
                                {**_payload, "queued_by_gate_user": _gate_audit_user()},
                            )
                            _rec0 = get_student_record(conn, _edit_sid)
                            _rc0 = (_rec0 or {}).get("student_code")
                            _s0 = "" if _rc0 is None else str(_rc0).strip()
                            _code0 = display_student_code(_s0) if _s0 else str(_edit_sid)
                            _audit_log(
                                conn,
                                "Manage Student",
                                f"Manage Student, {_code0} ({_payload.get('name', 'student')}): changes saved for pending review.",
                                save_mode="pending_review",
                                detail=_audit_student_payload_detail(_payload),
                                entity_type="student",
                                entity_id=int(_edit_sid),
                                entity_code=str(_code0),
                            )
                            st.session_state.edit_student_mode = False
                            st.session_state["_student_flash_msg"] = (
                                f"Draft saved for **{_payload['name']}** (not written to the database yet). "
                                "Open **Pending Reviews** to apply with the admin password. "
                                "View Students may show a preview until then."
                            )
                            st.rerun()

                        if cancel_edit:
                            clear_student_edit_widget_state(_edit_sid)
                            st.session_state.edit_student_mode = False
                            st.rerun()

                if _editable:
                    st.markdown('<hr style="margin: 2rem 0; border: none; border-top: 1px solid var(--border);">', unsafe_allow_html=True)

                    col1, col2 = st.columns(2)

                    with col1:
                        _xfer_key = f"transfer_confirm_{student['id']}"
                        if st.button("Mark as Transferred", type="secondary", use_container_width=True):
                            st.session_state[_xfer_key] = True

                        if st.session_state.get(_xfer_key, False):
                            st.caption("Transfer reason is optional — leave it blank if you do not need a note.")
                            transfer_pw = st.text_input(
                                "Admin password",
                                type="password",
                                key=f"transfer_pw_{student['id']}",
                            )
                            transfer_reason = st.text_input(
                                "Transfer reason (optional)",
                                key=f"transfer_reason_{student['id']}",
                                placeholder="Optional note only",
                            )
                            st.caption(
                                "**Save for later** queues under **Pending Reviews**. "
                                "**Confirm transfer** applies immediately (admin password)."
                            )
                            xc1, xc2, xc3 = st.columns(3)
                            with xc1:
                                if st.button("Confirm transfer", type="primary", key=f"transfer_do_{student['id']}"):
                                    if (e := evaluate_admin_password_input(transfer_pw)) is not None:
                                        _invalidate_admin_password_fields(
                                            e,
                                            f"transfer_pw_{student['id']}",
                                        )
                                    else:
                                        try:
                                            _reason = (transfer_reason or "").strip() or None
                                            rep = schedule_student_transfer(
                                                conn, int(student["id"]), _reason
                                            )
                                            invalidate_student_cache()
                                            remove_pending_student_transfer(conn, int(student["id"]))
                                            st.session_state.pop(_xfer_key, None)
                                            st.session_state["_student_flash_msg"] = (
                                                f"**{student['name']}** marked as transferred. "
                                                f"Records will be permanently removed after {STUDENT_DELETION_GRACE_DAYS} days "
                                                f"(on or after {rep['deletion_scheduled'][:10]}). "
                                                "**Transferred** shows as Yes in View Students."
                                            )
                                            _clear_password_field_keys(f"transfer_pw_{student['id']}")
                                            st.rerun()
                                        except Exception as e:
                                            conn.rollback()
                                            _invalidate_admin_password_fields(
                                                f"Error: {str(e)}",
                                                f"transfer_pw_{student['id']}",
                                            )
                            with xc2:
                                if st.button("Save for later", key=f"transfer_later_{student['id']}"):
                                    queue_pending_student_transfer(
                                        conn,
                                        int(student["id"]),
                                        {
                                            "name": student.get("name"),
                                            "transfer_reason": (transfer_reason or "").strip() or None,
                                            "queued_by_gate_user": _gate_audit_user(),
                                        },
                                    )
                                    st.session_state.pop(_xfer_key, None)
                                    st.session_state["_student_flash_msg"] = (
                                        f"Transfer for **{student['name']}** saved for later — "
                                        "review under **Pending Reviews**."
                                    )
                                    st.rerun()
                            with xc3:
                                if st.button("Cancel", key=f"transfer_cancel_{student['id']}"):
                                    st.session_state.pop(_xfer_key, None)
                                    st.rerun()

                    with col2:
                        _del_key = f"delete_confirm_{student['id']}"
                        _del_reason_ready_key = f"delete_reason_ready_{student['id']}"
                        _del_ack_key = f"delete_ack_{student['id']}"
                        _del_reason_store_key = f"delete_reason_text_{student['id']}"
                        if st.button("Delete Student Record", type="secondary", use_container_width=True):
                            st.session_state[_del_key] = True
                            st.session_state.pop(_del_reason_ready_key, None)
                            st.session_state.pop(_del_ack_key, None)
                            st.session_state.pop(_del_reason_store_key, None)

                        if st.session_state.get(_del_key, False):
                            with st.container(border=True):
                                if not st.session_state.get(_del_reason_ready_key, False):
                                    st.markdown("#### Reason for deletion")
                                    st.caption(
                                        "Choose why this record is being removed. This is saved for accountability."
                                    )
                                    _reason_preset = st.selectbox(
                                        "Reason",
                                        STUDENT_DELETION_REASON_OPTIONS,
                                        key=f"delete_reason_preset_{student['id']}",
                                    )
                                    _reason_custom = ""
                                    if str(_reason_preset).startswith("Other"):
                                        _reason_custom = st.text_input(
                                            "Please specify",
                                            placeholder="Enter the reason…",
                                            key=f"delete_reason_custom_{student['id']}",
                                        )
                                    dr1, dr2 = st.columns(2)
                                    with dr1:
                                        if st.button(
                                            "Continue",
                                            type="primary",
                                            key=f"delete_reason_continue_{student['id']}",
                                            use_container_width=True,
                                        ):
                                            _resolved = resolve_deletion_reason_text(
                                                _reason_preset, _reason_custom
                                            )
                                            if not _resolved:
                                                st.warning("Select a reason or enter a custom reason.")
                                            else:
                                                st.session_state[_del_reason_store_key] = _resolved
                                                st.session_state[_del_reason_ready_key] = True
                                                st.rerun()
                                    with dr2:
                                        if st.button(
                                            "Cancel",
                                            key=f"delete_cancel_{student['id']}",
                                            use_container_width=True,
                                        ):
                                            st.session_state.pop(_del_key, None)
                                            st.session_state.pop(_del_reason_ready_key, None)
                                            st.session_state.pop(_del_ack_key, None)
                                            st.session_state.pop(_del_reason_store_key, None)
                                            st.rerun()
                                elif not st.session_state.get(_del_ack_key, False):
                                    st.markdown("#### Confirm deletion")
                                    _saved_reason = st.session_state.get(_del_reason_store_key) or "—"
                                    st.info(f"**Reason:** {_saved_reason}")
                                    st.warning(
                                    f"**{student['name']}** ({_student_code_display_cell(student.get('student_code', ''))}) will be marked for removal. "
                                    f"The record stays in the system until the grace period ends, then it is **permanently deleted** "
                                    f"(including payments linked to this student row).\n\n"
                                    f"**Deleted student records are permanently removed after "
                                    f"{STUDENT_DELETION_GRACE_DAYS} days.**"
                                )
                                if not st.session_state.get(_del_ack_key, False):
                                    dc1, dc2 = st.columns(2)
                                    with dc1:
                                        if st.button(
                                            "Confirm deletion",
                                            type="primary",
                                            key=f"delete_ack_btn_{student['id']}",
                                            use_container_width=True,
                                        ):
                                            st.session_state[_del_ack_key] = True
                                            st.rerun()
                                    with dc2:
                                        if st.button(
                                            "Back",
                                            key=f"delete_reason_back_{student['id']}",
                                            use_container_width=True,
                                        ):
                                            st.session_state.pop(_del_reason_ready_key, None)
                                            st.session_state.pop(_del_ack_key, None)
                                            st.rerun()
                                else:
                                    st.caption(
                                        "**Save for later** queues under **Pending Reviews**. "
                                        "**Schedule deletion** applies immediately (admin password)."
                                    )
                                    delete_pw = st.text_input(
                                        "Admin password",
                                        type="password",
                                        key=f"delete_pw_{student['id']}",
                                    )
                                    _del_reason = st.session_state.get(_del_reason_store_key)
                                    ds1, ds2, ds3 = st.columns(3)
                                    with ds1:
                                        if st.button(
                                            "Schedule deletion",
                                            type="primary",
                                            key=f"delete_do_{student['id']}",
                                            use_container_width=True,
                                        ):
                                            if (e := evaluate_admin_password_input(delete_pw)) is not None:
                                                _invalidate_admin_password_fields(
                                                    e,
                                                    f"delete_pw_{student['id']}",
                                                )
                                            else:
                                                try:
                                                    rep = schedule_student_deletion(
                                                        conn,
                                                        int(student["id"]),
                                                        _del_reason,
                                                    )
                                                    invalidate_student_cache()
                                                    remove_pending_student_deletion(
                                                        conn, int(student["id"])
                                                    )
                                                    st.session_state.pop(_del_key, None)
                                                    st.session_state.pop(_del_reason_ready_key, None)
                                                    st.session_state.pop(_del_ack_key, None)
                                                    st.session_state.pop(_del_reason_store_key, None)
                                                    st.session_state["_student_flash_msg"] = (
                                                        f"**{student['name']}** is scheduled for permanent deletion in "
                                                        f"**{STUDENT_DELETION_GRACE_DAYS} days** "
                                                        f"(on or after {rep['deletion_scheduled'][:10]}). "
                                                        f"Reason: {_del_reason}"
                                                    )
                                                    _clear_password_field_keys(f"delete_pw_{student['id']}")
                                                    st.rerun()
                                                except Exception as e:
                                                    conn.rollback()
                                                    _invalidate_admin_password_fields(
                                                        f"Error: {str(e)}",
                                                        f"delete_pw_{student['id']}",
                                                    )
                                    with ds2:
                                        if st.button(
                                            "Save for later",
                                            key=f"delete_later_{student['id']}",
                                            use_container_width=True,
                                        ):
                                            queue_pending_student_deletion(
                                                conn,
                                                int(student["id"]),
                                                {
                                                    "name": student.get("name"),
                                                    "deletion_reason": _del_reason,
                                                    "queued_by_gate_user": _gate_audit_user(),
                                                },
                                            )
                                            st.session_state.pop(_del_key, None)
                                            st.session_state.pop(_del_reason_ready_key, None)
                                            st.session_state.pop(_del_ack_key, None)
                                            st.session_state.pop(_del_reason_store_key, None)
                                            st.session_state["_student_flash_msg"] = (
                                                f"Deletion for **{student['name']}** saved for later — "
                                                "review under **Pending Reviews**."
                                            )
                                            st.rerun()
                                    with ds3:
                                        if st.button(
                                            "Back",
                                            key=f"delete_back_{student['id']}",
                                            use_container_width=True,
                                        ):
                                            st.session_state.pop(_del_ack_key, None)
                                            st.rerun()
    

                st.markdown('</div>', unsafe_allow_html=True)

            with st.expander("Admin: merge duplicate names in a grade", expanded=False):
                st.caption(
                    "Same grade and same learner name (ignoring capitals and extra spaces): keeps the best record "
                    "(most payments / fee lines / recorded total paid), moves payments and optional fees onto it, "
                    "then deletes the duplicate rows."
                )
                _dg_list = list(dict.fromkeys(GRADE_CHOICES_EDIT))
                _dg_ix = _dg_list.index("Grade 5") if "Grade 5" in _dg_list else 0
                d_col1, d_col2, d_col3 = st.columns((1.4, 1, 1.2))
                with d_col1:
                    dup_grade_sel = st.selectbox("Grade", options=_dg_list, index=_dg_ix, key="dedupe_grade_pick")
                with d_col2:
                    dup_dry = st.checkbox("Preview only (no changes)", value=False, key="dedupe_dry")
                with d_col3:
                    dedupe_pw = st.text_input("Admin password", type="password", key="dedupe_admin_pw")
                if st.button("Run duplicate-name merge", key="dedupe_names_btn"):
                    if (e := evaluate_admin_password_input(dedupe_pw)) is not None:
                        _invalidate_admin_password_fields(
                            e,
                            "dedupe_admin_pw",
                        )
                    else:
                        rep = dedupe_students_in_grade_by_name(conn, dup_grade_sel, dry_run=dup_dry)
                        invalidate_student_cache()
                        st.markdown(
                            f'<div class="success-message">Duplicate name groups: <strong>{rep["groups_merged"]}</strong> · '
                            f'Rows removed (or that would be removed): <strong>{rep["rows_removed"]}</strong>'
                            f'{" — preview only, no database changes." if dup_dry else "."}</div>',
                            unsafe_allow_html=True,
                        )
                        with st.expander("Merge log", expanded=False):
                            for line in rep.get("log", []):
                                st.text(line)
                        if not dup_dry and rep.get("rows_removed", 0) > 0:
                            _clear_password_field_keys("dedupe_admin_pw")
                            st.rerun()
        else:
            st.markdown('<div class="info-message">No students found in the system.</div>', unsafe_allow_html=True)

elif menu == "Add Student":
    st.markdown('<h2 class="section-header">Student Registration</h2>', unsafe_allow_html=True)

    with st.expander("Bulk import from spreadsheet (CSV or Excel)", expanded=False):
        st.markdown(
            '<p class="vine-help-text">'
            "<strong>Minimum:</strong> one column you map to <strong>student name</strong> (wide exports from last term "
            "are fine — pick the name column below and ignore the rest). <strong>Class</strong> is taken from a "
            "<strong>Grade</strong> column when present; otherwise from <strong>column titles</strong> that name a class "
            "(e.g. <code>Grade 1</code>) or from the <strong>file name</strong> (e.g. <code>PP2_roster.csv</code>). "
            f"If nothing matches, learners stay <strong>{INCOMPLETE_GRADE_LABEL}</strong> until you edit them in "
            "<strong>Manage Students</strong>.<br/><br/>"
            "Optional columns (or manual mapping): <strong>parent_name</strong>, <strong>parent_phone</strong>, "
            "<strong>parent2_name</strong>, <strong>parent2_phone</strong>, "
            "<strong>grade</strong>, <strong>student_code</strong>, <strong>transport_route</strong>, "
            "<strong>has_meal</strong>, <strong>include_admission</strong>, <strong>include_interview</strong>, <strong>co_curricular</strong> (see template)."
            "</p>",
            unsafe_allow_html=True,
        )
        st.download_button(
            label="Download CSV template",
            data=STUDENT_IMPORT_TEMPLATE_CSV,
            file_name="student_import_template.csv",
            mime="text/csv",
            key="download_student_import_template",
        )
        bulk_file = st.file_uploader(
            "Choose spreadsheet",
            type=["csv", "xlsx"],
            key="bulk_student_upload",
            help="First row must be column headers. Row numbers in error messages match your sheet (row 1 = headers).",
        )
        if bulk_file is not None:
            try:
                df_bulk = read_student_spreadsheet(bulk_file)
                st.caption(f"Loaded **{len(df_bulk)}** data rows (empty lines dropped). Preview:")
                st.dataframe(df_bulk.head(30), use_container_width=True, hide_index=True)

                _bulk_cols = list(df_bulk.columns)
                _suggested_name = suggest_student_import_name_column(df_bulk)
                _name_default_idx = (
                    _bulk_cols.index(_suggested_name)
                    if _suggested_name and _suggested_name in _bulk_cols
                    else 0
                )
                st.markdown("**Column mapping**")
                bulk_name_column = st.selectbox(
                    "Student name column",
                    options=_bulk_cols,
                    index=min(_name_default_idx, len(_bulk_cols) - 1) if _bulk_cols else 0,
                    key="bulk_import_name_column",
                    help="Which column in this file has the learner’s full name? (Balances and totals from an old export are never imported.)",
                )
                bulk_names_only = st.checkbox(
                    "Only import names — ignore every other column in this file",
                    value=True,
                    key="bulk_import_names_only",
                    help="Recommended for prior-term spreadsheets: avoids matching wrong columns (e.g. “Phone”, “Balance”) to parent or fees.",
                )
                _pg = preview_sheet_grade_for_bulk_import(df_bulk, bulk_name_column, bulk_file.name)
                if _pg:
                    st.caption(
                        f"**Class hint:** rows without a mapped **Grade** column will be assigned **{_pg}** "
                        "(from column titles that name one class and/or the upload file name). "
                        "You can correct any learner under **Manage Students**."
                    )
                else:
                    st.caption(
                        f"No class inferred from this file’s name or column titles; without a **Grade** column, "
                        f"new students will be **{INCOMPLETE_GRADE_LABEL}** until you set their class in **Manage Students**."
                    )
                bulk_column_overrides = {}
                _auto = "— Auto-detect —"
                _skip = "— Skip —"
                _opt_labels = {
                    "parent_name": "Parent/Guardian 1 name",
                    "parent_phone": "Parent/Guardian 1 phone",
                    "parent2_name": "Parent/Guardian 2 name",
                    "parent2_phone": "Parent/Guardian 2 phone",
                    "grade": "Grade / class",
                    "student_code": "Student code (if in file)",
                    "transport_route": "Transport / route",
                    "has_meal": "Meals (yes/no column)",
                    "include_admission": "Include admission fee (one-time)",
                    "include_interview": "Include interview fee (one-time)",
                    "co_curricular": "Co-curricular / clubs",
                }
                if not bulk_names_only:
                    with st.expander("Optional: map other columns (overrides auto-detect)", expanded=False):
                        st.caption(
                            "Choose **Auto-detect** to guess from headers, **Skip** to leave empty, or pick a column."
                        )
                        for logical_key, _aliases in STUDENT_IMPORT_OPTIONAL_SPECS:
                            opts = [_auto, _skip] + _bulk_cols
                            choice = st.selectbox(
                                _opt_labels.get(logical_key, logical_key),
                                options=opts,
                                index=0,
                                key=f"bulk_map_{logical_key}",
                            )
                            if choice == _skip:
                                bulk_column_overrides[logical_key] = None
                            elif choice != _auto:
                                bulk_column_overrides[logical_key] = choice

                if st.button("Import all rows into database", type="primary", key="bulk_student_import_run"):
                    with st.spinner("Importing students…"):
                        result = bulk_import_students_from_dataframe(
                            df_bulk,
                            conn,
                            name_column=bulk_name_column,
                            ignore_other_columns=bulk_names_only,
                            column_overrides=bulk_column_overrides if not bulk_names_only else None,
                            source_filename=getattr(bulk_file, "name", None) or "",
                        )
                    invalidate_student_cache()
                    st.markdown(
                        f'<div class="success-message">Import finished: <strong>{result["imported"]}</strong> students added. '
                        f'Skipped or not imported: <strong>{result["skipped"]}</strong>.</div>',
                        unsafe_allow_html=True,
                    )
                    if result.get("errors"):
                        with st.expander("Row-level messages (includes skips)", expanded=True):
                            for row_no, msg in result["errors"][:200]:
                                st.text(f"Sheet row {row_no}: {msg}")
                            if len(result["errors"]) > 200:
                                st.caption(f"… and {len(result['errors']) - 200} more messages.")
                    st.rerun()
            except Exception as ex:
                st.markdown(f'<div class="warning-message">Could not read file: {ex}</div>', unsafe_allow_html=True)
    
    # Get the next auto-generated student code
    next_code = get_next_student_code(conn)
    
    # Get fee structure data for dropdowns
    transport_items = conn.execute("SELECT id, fee_name, fee_amount FROM fee_structure WHERE fee_category='transport' ORDER BY fee_amount").fetchall()
    co_curricular_items = conn.execute("SELECT id, fee_name, fee_amount FROM fee_structure WHERE fee_category='co_curricular' ORDER BY fee_name").fetchall()
    
    st.markdown('<div class="form-container">', unsafe_allow_html=True)
    with st.form("add_student_form"):
        st.markdown('<h3 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Basic Information</h3>', unsafe_allow_html=True)
        
        # Display auto-generated student code
        st.markdown(f"""
        <div style="padding: 1rem; margin-bottom: 1rem; background: linear-gradient(135deg, var(--surface) 0%, var(--surface-alt) 100%); 
                   border-radius: 0.75rem; border: 2px solid var(--primary); border-left: 4px solid var(--primary);">
            <div class="vine-help-text" style="margin-bottom: 0.25rem;">Student Code (Auto-Generated)</div>
            <div style="font-size: 1.5rem; font-weight: 800; color: var(--primary); font-family: monospace;">{next_code}</div>
        </div>
        """, unsafe_allow_html=True)
        
        col1, col2 = st.columns(2)
        
        with col1:
            name = st.text_input("Student Name *", placeholder="Enter full name")
            parent = st.text_input("Parent/Guardian 1 name *", placeholder="Parent or guardian name")
            phone = st.text_input(
                "Parent/Guardian 1 phone *",
                help="Kenya numbers: 0712… / 01…, 2547… / 2541…, or +254… — stored as 254… to match M-Pesa/bank lines that use 254…",
                placeholder="0712345678, 0135448776, or 254712345678",
            )
            parent2 = st.text_input("Parent/Guardian 2 name (optional)", placeholder="Second parent or guardian")
            phone2 = st.text_input(
                "Parent/Guardian 2 phone (optional)",
                placeholder="0712345678, 0135448776, or 254712345678",
            )
            grade = st.selectbox("Grade *", REAL_GRADES)
            date_of_birth = st.date_input(
                "Date of birth",
                value=None,
                min_value=date(1995, 1, 1),
                max_value=date.today(),
                help="Optional. Age is calculated from this date on student records.",
            )
            _age_add = student_age_from_dob(date_of_birth)
            if _age_add is not None:
                st.caption(f"Age: **{_age_add}** years")
            new_admission = st.checkbox(
                "New Admission?",
                value=True,
                help="When checked, today's date is saved as the date this learner joined the school.",
            )

        with col2:
            st.markdown('<h4 style="color: var(--text); margin: 1rem 0 0.5rem 0; font-weight: 600;">Additional Services (Optional)</h4>', unsafe_allow_html=True)
            
            # Transport: one control — pick a route or "does not use"
            transport_keys = ["__none__"] + [str(item[0]) for item in transport_items]
            transport_labels = {
                "__none__": "Does not use school transport",
                **{str(t[0]): f"{t[1]} — KSH {t[2]:,.0f}" for t in transport_items},
            }
            transport_choice = st.selectbox(
                "School transport",
                options=transport_keys,
                index=0,
                format_func=lambda k: transport_labels.get(k, k),
                help="Choose the route for this learner, or select no transport.",
                key="add_student_transport_choice",
            )
            has_transport = transport_choice != "__none__"
            transport_route_id = int(transport_choice) if has_transport else None
            
            # Co-curricular activities - checkboxes
            st.markdown('<p class="vine-help-text" style="margin-top: 0.5rem;">Co-Curricular Activities (KSH 3,000 each)</p>', unsafe_allow_html=True)
            cc_columns = st.columns(3)
            for idx, item in enumerate(co_curricular_items):
                with cc_columns[idx % 3]:
                    st.checkbox(item[1], key=f"add_cc_{item[0]}")
            
            # Meal program
            has_meal = st.checkbox(meal_program_checkbox_label(conn), help="Check if student takes meals at school")
            include_admission_fees = st.checkbox(
                "Include one-time admission fee (KSH 1,000)",
                value=True,
                help="Registration / admission fee. Uncheck if not applicable.",
            )
            _add_iv = interview_fee_amount_for_grade(grade)
            include_interview_fee = st.checkbox(
                f"Include one-time interview fee (KSH {_add_iv:,.0f} for this class)",
                value=True,
                help="KSH 500 for Playgroup through Grade 6; KSH 700 for Grade 7 and above. Independent of admission fee.",
            )
            is_sponsored = st.checkbox(
                "Sponsored Student",
                help="Fully sponsored learners are not required to pay fees; outstanding balance is set to zero.",
            )
            if is_sponsored:
                st.text_input(
                    "Admin password (required for sponsored students) *",
                    type="password",
                    key="add_student_sponsored_pw",
                )
            
            st.markdown('<br>', unsafe_allow_html=True)
        
        st.markdown('<br>', unsafe_allow_html=True)
        col_prev, col_save = st.columns(2)
        with col_prev:
            preview = st.form_submit_button("Preview fee total")
        with col_save:
            submit = st.form_submit_button("Save Student", type="primary")

        selected_cc_ids = [
            int(item[0])
            for item in co_curricular_items
            if st.session_state.get(f"add_cc_{item[0]}", False)
        ]

        if preview:
            if not all([name, parent, phone]):
                st.markdown('<div class="warning-message">Please fill in all required fields marked with * to preview fees.</div>', unsafe_allow_html=True)
            else:
                import json
                fee_result = calculate_student_fees(
                    conn,
                    grade,
                    transport_route_id,
                    selected_cc_ids if selected_cc_ids else None,
                    has_meal,
                    include_admission=bool(include_admission_fees),
                    include_interview=bool(include_interview_fee),
                )
                st.markdown('<div class="info-message"><strong>Fee preview (expected this term)</strong></div>', unsafe_allow_html=True)
                br = fee_result.get("fee_breakdown") or []
                if br:
                    st.dataframe(
                        pd.DataFrame(br, columns=["Item", "Amount (KSH)"]),
                        use_container_width=True,
                        hide_index=True,
                    )
                adm = fee_result.get("admission_total") or 0
                inv = fee_result.get("interview_total") or 0
                st.caption(
                    f"Tuition + mandatory (per term): KSH {fee_result['mandatory_total']:,.0f} · "
                    f"Optional services: KSH {fee_result['optional_total']:,.0f} · "
                    f"Admission (if selected): KSH {adm:,.0f} · Interview (if selected): KSH {inv:,.0f} · "
                    f"**Grand total: KSH {fee_result['grand_total']:,.0f}**"
                )

        if submit:
            if not all([name, parent, phone]):
                st.markdown('<div class="warning-message">Please fill in all required fields marked with *</div>', unsafe_allow_html=True)
            else:
                _spw_err = (
                    evaluate_admin_password_input(st.session_state.get("add_student_sponsored_pw"))
                    if is_sponsored
                    else None
                )
                if _spw_err is not None:
                    _invalidate_admin_password_fields(
                        _spw_err,
                        "add_student_sponsored_pw",
                    )
                else:
                    try:
                        phone_n = normalize_kenya_msisdn(phone)
                        phone2_n = normalize_kenya_msisdn(phone2) if (phone2 or "").strip() else ""
                        cc_json = co_curricular_ids_to_json(selected_cc_ids)
                        joined_date = date.today().isoformat() if new_admission else None
                        _is_sponsored = int(bool(is_sponsored))
    
                        cur = conn.execute(
                            """INSERT INTO students 
                            (student_code, name, parent_name, parent_phone, parent2_name, parent2_phone,
                             grade, date_of_birth, balance, total_paid, has_transport, transport_route_id,
                             has_meal, co_curricular_activities, extra_classes, include_admission_fees,
                             include_interview_fee, joined_date, is_sponsored) 
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                next_code,
                                name,
                                parent,
                                phone_n,
                                (parent2 or "").strip(),
                                phone2_n,
                                grade,
                                dob_to_storage(date_of_birth),
                                0.0,
                                0.0,
                                has_transport,
                                transport_route_id,
                                has_meal,
                                cc_json,
                                "",
                                int(include_admission_fees),
                                int(include_interview_fee),
                                joined_date,
                                _is_sponsored,
                            ),
                        )
                        new_id = cur.lastrowid
                        save_student_co_curricular(conn, new_id, selected_cc_ids, do_commit=False)
                        conn.commit()
                        sync_student_fees_from_db(conn, new_id)
                        if _is_sponsored:
                            conn.execute(
                                "UPDATE students SET balance=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                (new_id,),
                            )
                            conn.commit()
                        invalidate_student_cache()
                        _audit_log(
                            conn,
                            "Add Student",
                            f"Added student {next_code} ({name}), grade {grade}, database id {new_id}.",
                            save_mode="immediate",
                            entity_type="student",
                            entity_id=int(new_id),
                            entity_code=str(next_code),
                            detail=json.dumps(
                                {"sponsored": bool(_is_sponsored), "new_admission": bool(new_admission)},
                                default=str,
                            ),
                        )
                        
                        bal_row = conn.execute("SELECT balance FROM students WHERE id=?", (new_id,)).fetchone()
                        new_balance = bal_row[0] if bal_row else 0.0
                        fee_result = calculate_student_fees(
                            conn,
                            grade,
                            transport_route_id,
                            selected_cc_ids if selected_cc_ids else None,
                            has_meal,
                            include_admission=bool(include_admission_fees),
                            include_interview=bool(include_interview_fee),
                        )
                        st.markdown(
                            f'<div class="success-message">Student <strong>{name}</strong> has been successfully added with code <strong>{next_code}</strong>!<br/>'
                            f'{"Expected fees (reference only)" if _is_sponsored else "Expected fees (this term)"}: <strong>KSH {fee_result["grand_total"]:,.0f}</strong> · '
                            f'Outstanding balance: <strong>KSH {new_balance:,.0f}</strong>'
                            f'{" · <strong>Sponsored</strong>" if _is_sponsored else ""}</div>',
                            unsafe_allow_html=True,
                        )
                        _clear_password_field_keys("add_student_sponsored_pw")
                        st.rerun()
                    except Exception as e:
                        st.markdown(f'<div class="warning-message">Error adding student: {str(e)}</div>', unsafe_allow_html=True)
    
    st.markdown('</div>', unsafe_allow_html=True)

elif menu == "View Students":
    _view_flash = st.session_state.pop("_student_flash_msg", None)
    if _view_flash:
        st.success(_view_flash)

    _pending_for_view = st.session_state.get("pending_student_edits") or {}
    if _pending_for_view:
        st.warning(
            f"**{len(_pending_for_view)}** student edit(s) are **not saved to the database yet**. "
            "View Students shows a **preview** only (see **Save status** column). "
            "To make changes permanent, open **Manage Students → Pending Reviews**, "
            "review each draft, and apply with the admin password."
        )

    st.markdown('<h2 class="section-header">Student Records by Grade</h2>', unsafe_allow_html=True)
    
    # Define all grades
    all_grades = list(REAL_GRADES) + [INCOMPLETE_GRADE_LABEL]
    
    # Initialize selected grade / club in session state if not exists
    if "selected_grade" not in st.session_state:
        st.session_state.selected_grade = None
    if "selected_cc_club" not in st.session_state:
        st.session_state.selected_cc_club = None
    if "view_students_category" not in st.session_state:
        st.session_state.view_students_category = None

    _cc_catalog = pd.read_sql(
        "SELECT id, fee_name FROM fee_structure WHERE fee_category='co_curricular' ORDER BY fee_name",
        conn,
    )

    # Co-curricular club roster (name, age, grade only)
    if st.session_state.selected_cc_club is not None:
        _club_id = int(st.session_state.selected_cc_club)
        _club_name = _cc_catalog.loc[_cc_catalog["id"] == _club_id, "fee_name"]
        _club_title = _club_name.iloc[0] if not _club_name.empty else f"Club {_club_id}"

        col_b1, col_b2 = st.columns([1, 4])
        with col_b1:
            if st.button("← Back to categories", use_container_width=True):
                st.session_state.selected_cc_club = None
                st.session_state.view_students_category = None
                st.rerun()
        with col_b2:
            st.markdown(
                f'<h3 style="color: var(--text); margin: 0;">{_club_title}</h3>',
                unsafe_allow_html=True,
            )

        _all_for_clubs = pd.read_sql(
            "SELECT id, name, grade, date_of_birth, co_curricular_activities, status FROM students ORDER BY name",
            conn,
        )
        _all_for_clubs = _all_for_clubs.loc[active_students_mask(_all_for_clubs)]
        _all_for_clubs = apply_pending_student_edits_to_df(_all_for_clubs, _pending_for_view)
        _members = students_in_co_curricular_club(_all_for_clubs, _club_id, conn=conn)
        st.caption(f"**{len(_members)}** learner(s) in this club — name, age, and grade only.")

        if _members.empty:
            st.info("No students are enrolled in this club yet.")
        else:
            st.markdown('<div class="dataframe-container">', unsafe_allow_html=True)
            st.dataframe(
                club_roster_display_df(_members),
                use_container_width=True,
                hide_index=True,
            )
            st.markdown("</div>", unsafe_allow_html=True)

    elif st.session_state.get("view_students_category"):
        _cat = st.session_state.view_students_category
        _meta = VIEW_STUDENTS_CATEGORY_META.get(_cat, {})
        _cat_title = _meta.get("title", _cat.replace("_", " ").title())

        col_b1, col_b2 = st.columns([1, 4])
        with col_b1:
            if st.button("← Back to categories", key="vs_back_insight", use_container_width=True):
                st.session_state.view_students_category = None
                st.session_state.pop("carry_on_student_id", None)
                st.rerun()
        with col_b2:
            st.markdown(
                f'<h3 style="color: var(--text); margin: 0;">{html_module.escape(_cat_title)}</h3>',
                unsafe_allow_html=True,
            )
        if _meta.get("caption"):
            st.caption(_meta["caption"])

        if _cat == "carry_on":
            _render_carry_on_tab(conn)
        else:
            _cat_df = _load_view_students_category_df(conn, _cat)
            _include_exited_default = _cat == "student_exits_term"
            _show_exited_toggle = _cat in ("new_admissions_term", "new_admissions_year")
            _render_view_students_records_list(
                conn,
                _cat_df,
                list_title=_cat_title,
                search_placeholder="Search by name, parent/guardian, grade, or student code…",
                pending_edits=_pending_for_view,
                ledger_key_suffix=f"cat_{_cat}",
                include_exited=_include_exited_default,
                show_include_exited_checkbox=_show_exited_toggle,
            )

    # Grade or club category picker
    elif st.session_state.selected_grade is None:
        st.markdown('<h3 class="section-header">Select a Grade Category</h3>', unsafe_allow_html=True)
        
        # Active student counts by grade (graduated leavers are archived, not listed here)
        grade_counts = {}
        for grade in all_grades:
            count_df = pd.read_sql(
                """SELECT COUNT(*) as count FROM students
                   WHERE grade = ? AND COALESCE(status, 'Active') = 'Active'""",
                conn,
                params=(grade,),
            )
            grade_counts[grade] = count_df.iloc[0]["count"]
        
        # Create grade category cards in a grid
        cols = st.columns(4)
        grade_colors = [
            "var(--primary)", "var(--secondary)", "var(--accent)", "var(--success)",
            "var(--info)", "var(--warning)", "var(--danger)", "#8b5cf6",
            "#06b6d4", "#84cc16", "#f59e0b", "#ef4444"
        ]
        
        for i, grade in enumerate(all_grades):
            with cols[i % 4]:
                count = grade_counts.get(grade, 0)
                color = grade_colors[i % len(grade_colors)]
                
                emit_picker_card(
                    render_picker_card_html(
                        grade,
                        count,
                        "students",
                        color,
                        eyebrow="Grade",
                        title_html=format_grade_picker_title_html(grade),
                    )
                )
                
                if st.button(f"View {grade}", key=f"grade_btn_{grade}", use_container_width=True):
                    st.session_state.selected_grade = grade
                    st.session_state.selected_cc_club = None
                    st.session_state.view_students_category = None
                    st.rerun()
        st.markdown('<h3 class="section-header">Co-curricular clubs</h3>', unsafe_allow_html=True)
        _all_for_cc_counts = pd.read_sql(
            "SELECT id, co_curricular_activities, status FROM students", conn
        )
        _all_for_cc_counts = _all_for_cc_counts.loc[active_students_mask(_all_for_cc_counts)]
        _all_for_cc_counts = apply_pending_student_edits_to_df(_all_for_cc_counts, _pending_for_view)
        if _cc_catalog.empty:
            st.caption("No co-curricular activities are set up in the fee structure yet.")
        else:
            _cc_cols = st.columns(4)
            _cc_palette = [
                "var(--secondary)",
                "var(--accent)",
                "var(--success)",
                "var(--info)",
                "var(--warning)",
                "#8b5cf6",
                "#06b6d4",
            ]
            for _ci, _crow in enumerate(_cc_catalog.itertuples(index=False)):
                _cid = int(_crow.id)
                _cname = _crow.fee_name
                _ccount = len(students_in_co_curricular_club(_all_for_cc_counts, _cid, conn=conn))
                _ccol = _cc_palette[_ci % len(_cc_palette)]
                with _cc_cols[_ci % 4]:
                    _club_meta = club_display_meta(_cname)
                    _bg_url = club_bg_data_url(_club_meta["slug"])
                    emit_picker_card(
                        render_picker_card_html(
                            _cname,
                            _ccount,
                            "members",
                            _ccol,
                            eyebrow="Club",
                            title_emoji=_club_meta["emoji"],
                            bg_data_url=_bg_url,
                        )
                    )
                    if st.button(f"View {_cname}", key=f"cc_btn_{_cid}", use_container_width=True):
                        st.session_state.selected_cc_club = _cid
                        st.session_state.selected_grade = None
                        st.session_state.view_students_category = None
                        st.rerun()

        st.markdown('<h3 class="section-header">Insight categories</h3>', unsafe_allow_html=True)
        st.caption("Admissions, exits, transport, meals, balances, and carry-on from prior terms.")
        _insight_cols = st.columns(3)
        for _ii, _cat_key in enumerate(VIEW_STUDENTS_INSIGHT_CATEGORIES):
            _imeta = VIEW_STUDENTS_CATEGORY_META[_cat_key]
            _icount = _count_view_students_category(conn, _cat_key)
            with _insight_cols[_ii % 3]:
                emit_picker_card(
                    render_picker_card_html(
                        _imeta["title"],
                        _icount,
                        "students",
                        _imeta["color"],
                        eyebrow=_imeta["eyebrow"],
                    )
                )
                if st.button(
                    f"View {_imeta['title']}",
                    key=f"vs_insight_btn_{_cat_key}",
                    use_container_width=True,
                ):
                    st.session_state.view_students_category = _cat_key
                    st.session_state.selected_grade = None
                    st.session_state.selected_cc_club = None
                    if _cat_key != "carry_on":
                        st.session_state.pop("carry_on_student_id", None)
                    st.rerun()

        # Show all students option
        st.markdown('<br>', unsafe_allow_html=True)
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            if st.button("📋 View All Students", type="primary", use_container_width=True):
                st.session_state.selected_grade = "All"
                st.session_state.selected_cc_club = None
                st.session_state.view_students_category = None
                st.rerun()
    
    else:
        # Show students for selected grade
        selected_grade = st.session_state.selected_grade
        
        # Back button
        col1, col2 = st.columns([1, 4])
        with col1:
            if st.button("← Back to categories", use_container_width=True):
                st.session_state.selected_grade = None
                st.session_state.view_students_category = None
                st.rerun()
        
        with col2:
            st.markdown(f'<h3 style="color: var(--text); margin: 0;">{selected_grade} Students</h3>', unsafe_allow_html=True)
        
        # Search within grade
        st.markdown('<div class="form-container">', unsafe_allow_html=True)
        search_term = st.text_input(
            f"Search {selected_grade} students",
            placeholder="Search by name, parent/guardian, or student code…",
            help="Type any part of the name, parent/guardian name, phone, or student code",
        )
        include_exited = st.checkbox(
            "Include transferred / scheduled for deletion (graduates are archived, not listed here)",
            value=False,
            key="view_students_include_exited",
        )
        st.markdown('</div>', unsafe_allow_html=True)
        
        # Query based on selection
        if selected_grade == "All":
            if search_term:
                df = pd.read_sql(f'''
                    SELECT * FROM students 
                    WHERE (name LIKE '%{search_term}%' 
                    OR parent_name LIKE '%{search_term}%'
                    OR parent_phone LIKE '%{search_term}%'
                    OR parent2_name LIKE '%{search_term}%'
                    OR parent2_phone LIKE '%{search_term}%'
                    OR student_code LIKE '%{search_term}%')
                ''', conn)
            else:
                df = pd.read_sql("SELECT * FROM students", conn)
        else:
            if search_term:
                df = pd.read_sql(f'''
                    SELECT * FROM students 
                    WHERE grade = ? 
                    AND (name LIKE '%{search_term}%' 
                    OR parent_name LIKE '%{search_term}%'
                    OR parent_phone LIKE '%{search_term}%'
                    OR parent2_name LIKE '%{search_term}%'
                    OR parent2_phone LIKE '%{search_term}%'
                    OR student_code LIKE '%{search_term}%')
                ''', conn, params=(selected_grade,))
            else:
                df = pd.read_sql("SELECT * FROM students WHERE grade = ?", conn, params=(selected_grade,))

        if not df.empty:
            df = df.loc[view_students_display_mask(df, include_exited=include_exited)].copy()

        if not df.empty:
            _scope = "active" if not include_exited else "listed"
            st.markdown(
                f'<div class="info-message">Found <strong>{len(df)}</strong> '
                f'{(_scope + " ") if _scope else ""}students in {selected_grade}</div>',
                unsafe_allow_html=True,
            )
            
            # Format the data for better display
            df = apply_pending_student_edits_to_df(df, _pending_for_view)
            df = add_pending_draft_status_column(df, _pending_for_view)
            display_df = enrich_students_for_view(df, conn)
            if "student_code" in display_df.columns:
                display_df = display_df.copy()
                display_df["student_code"] = display_df["student_code"].map(_student_code_display_cell)

            def _yn(v):
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    return "No"
                if isinstance(v, bool):
                    return "Yes" if v else "No"
                try:
                    return "Yes" if int(v) else "No"
                except (TypeError, ValueError):
                    return "Yes" if v else "No"

            if "has_transport" in display_df.columns:
                display_df["has_transport"] = display_df["has_transport"].apply(_yn)
            if "has_meal" in display_df.columns:
                display_df["has_meal"] = display_df["has_meal"].apply(_yn)
            if "include_admission_fees" in display_df.columns:
                display_df["include_admission_fees"] = display_df["include_admission_fees"].apply(_yn)
            if "include_interview_fee" in display_df.columns:
                display_df["include_interview_fee"] = display_df["include_interview_fee"].apply(_yn)
            if "total_paid" in display_df.columns:
                display_df["total_paid"] = display_df["total_paid"].apply(lambda x: f"KSH {x:,.0f}")

            for _hc in VIEW_STUDENTS_HIDDEN_COLUMNS:
                if _hc in display_df.columns:
                    display_df = display_df.drop(columns=[_hc])

            display_df = display_df.rename(
                columns={
                    c: VIEW_STUDENTS_SQL_COLUMN_LABELS.get(c, c.replace("_", " ").title())
                    for c in display_df.columns
                }
            )

            _pref = [
                "Save status",
                "Student Code",
                "Name",
                "Grade",
                "Date of birth",
                "Age",
                "Joined",
                "Parent/Guardian 1 Name",
                "Parent/Guardian 1 Phone",
                "Parent/Guardian 2 Name",
                "Parent/Guardian 2 Phone",
                "School transport",
                "Transport route",
                "Meals program",
                "Co-curricular",
                "Balance",
                "Total Paid",
                "Most recent payment",
                "To be deleted",
                "Days till deleted",
                "Deletion reason",
                "Transfer reason",
                "Transferred",
                "Status",
                "Admission fees",
            ]
            _ordered = [c for c in _pref if c in display_df.columns]
            _rest = [c for c in display_df.columns if c not in _ordered]
            display_df = display_df[_ordered + _rest]

            # Display table with container
            st.markdown('<div class="dataframe-container">', unsafe_allow_html=True)
            render_students_table_with_bf(df, display_df, conn)
            st.markdown('</div>', unsafe_allow_html=True)

            _ledger_opts = df["id"].astype(int).tolist()
            _ledger_labels = {
                int(r["id"]): f"{r['name']} ({_student_code_display_cell(r.get('student_code'))})"
                for _, r in df.iterrows()
            }
            _ledger_sid = st.selectbox(
                "View term billing history for a student",
                options=_ledger_opts,
                format_func=lambda sid: _ledger_labels[int(sid)],
                key=f"view_students_ledger_{selected_grade}",
            )
            if _ledger_sid:
                render_student_term_ledger(conn, int(_ledger_sid))
            
            # Add statistics with better styling
            st.markdown('<h3 class="section-header">Summary Statistics</h3>', unsafe_allow_html=True)

            col1, col2, col3, col4, col5 = st.columns(5)

            _ages = [student_age_from_dob(r.get("date_of_birth")) for _, r in df.iterrows()]
            _ages = [a for a in _ages if a is not None]
            _avg_age = f"{sum(_ages) / len(_ages):.1f}" if _ages else "—"
            _lunch_takers = sum(
                student_row_bool(r, "has_meal", False) for _, r in df.iterrows()
            )
            _transport_users = sum(
                student_row_bool(r, "has_transport", False) for _, r in df.iterrows()
            )

            with col1:
                st.markdown(f"""
                <div class="metric-card" style="border-left-color: var(--primary);">
                    <div style="font-size: 1.25rem; font-weight: 700; color: var(--primary);">{len(df)}</div>
                    <div class="vine-help-text" style="margin-top: 0.25rem;">Total Students</div>
                </div>
                """, unsafe_allow_html=True)

            with col2:
                total_balance = sum_outstanding_balance_rows(df)
                st.markdown(f"""
                <div class="metric-card" style="border-left-color: var(--danger);">
                    <div style="font-size: 1.25rem; font-weight: 700; color: var(--danger);">KSH {total_balance:,.0f}</div>
                    <div class="vine-help-text" style="margin-top: 0.25rem;">Total Outstanding</div>
                </div>
                """, unsafe_allow_html=True)

            with col3:
                st.markdown(f"""
                <div class="metric-card" style="border-left-color: var(--accent);">
                    <div style="font-size: 1.25rem; font-weight: 700; color: var(--accent);">{_avg_age}</div>
                    <div class="vine-help-text" style="margin-top: 0.25rem;">Average age</div>
                </div>
                """, unsafe_allow_html=True)

            with col4:
                st.markdown(f"""
                <div class="metric-card" style="border-left-color: var(--success);">
                    <div style="font-size: 1.25rem; font-weight: 700; color: var(--success);">{_lunch_takers}</div>
                    <div class="vine-help-text" style="margin-top: 0.25rem;">Lunch takers</div>
                </div>
                """, unsafe_allow_html=True)

            with col5:
                st.markdown(f"""
                <div class="metric-card" style="border-left-color: var(--info);">
                    <div style="font-size: 1.25rem; font-weight: 700; color: var(--info);">{_transport_users}</div>
                    <div class="vine-help-text" style="margin-top: 0.25rem;">Transport users</div>
                </div>
                """, unsafe_allow_html=True)

            st.caption(
                "**Total outstanding** sums only **set** balances (same as each row’s balance display; **Not set** / **Cleared** are excluded)."
            )
        else:
            st.markdown(f'<div class="info-message">No students found in {selected_grade}.</div>', unsafe_allow_html=True)

elif menu == "Payment Management":
    _pay_flash = st.session_state.pop("_payment_flash_msg", None)
    if _pay_flash:
        st.success(_pay_flash)

    st.markdown('<h2 class="section-header">Payment Management</h2>', unsafe_allow_html=True)
    st.caption(
        "Upload and match bank statements or add cash and other payments manually. "
        "The **Upload bank statement** tab requires your **sign-in password** before PDF upload (restricted accounts cannot unlock it); "
        "**Add payment** does not."
    )

    _n_pay_pend = len(st.session_state.get("pending_manual_payment_drafts") or [])
    _pay_tabs = ["Upload bank statement", "Add payment"]
    _pay_tabs.append(pending_review_tab_label(_n_pay_pend))
    pay_tabs = st.tabs(_pay_tabs)
    with pay_tabs[0]:
        _render_bank_statement_upload(conn)
    with pay_tabs[1]:
        _render_manual_payment_tab(conn)
    with pay_tabs[2]:
        _render_pending_manual_payments_tab(conn)

elif menu == "Generate Receipts":
    st.markdown('<h2 class="section-header">Generate receipts</h2>', unsafe_allow_html=True)

    if "last_autofill_receipt" not in st.session_state:
        st.session_state.last_autofill_receipt = None
    if "sample_receipt_path" not in st.session_state:
        st.session_state.sample_receipt_path = None

    _students_all = pd.read_sql("SELECT * FROM students", conn)
    students_df = (
        _students_all[_students_all["total_paid"] > 0]
        .sort_values("updated_at", ascending=False)
        .reset_index(drop=True)
    )

    template_exists = os.path.exists("receipt_template.pdf") or os.path.exists("receipt_template.png")
    template_path_resolved = None
    if template_exists:
        template_path_resolved = (
            "receipt_template.pdf" if os.path.exists("receipt_template.pdf") else "receipt_template.png"
        )
    if not template_exists and st.session_state.get("gr_replace_confirm"):
        st.session_state.gr_replace_confirm = False

    _app_dir = os.path.dirname(os.path.abspath(__file__))
    _layout_path = os.path.join(_app_dir, "receipt_layout.json")
    _layout_example = os.path.join(_app_dir, "receipt_layout.example.json")
    layout_ok = os.path.isfile(_layout_path)

    tab_tpl, tab_pay, tab_sample = st.tabs(["Template", "From payments", "Sample"])

    with tab_tpl:
        st.markdown('<div class="form-container">', unsafe_allow_html=True)
        if template_exists:
            tpl_name = os.path.basename(template_path_resolved)
            c1, c2 = st.columns([3, 1])
            with c1:
                extra = " · `receipt_layout.json` is active (per-field positions)." if layout_ok else ""
                st.success(f"Template on file: `{tpl_name}`{extra}")
            with c2:
                if st.button("Replace", help="Remove the current template so you can upload a new one", key="gr_replace_tpl"):
                    st.session_state.gr_replace_confirm = True
            if st.session_state.get("gr_replace_confirm") and template_exists:
                st.markdown(
                    '<p class="vine-help-text">'
                    "Removing the template cannot be undone here. Enter the admin password to confirm.</p>",
                    unsafe_allow_html=True,
                )
                rp = st.text_input("Admin password", type="password", key="gr_replace_pw")
                rc1, rc2 = st.columns(2)
                with rc1:
                    if st.button("Confirm removal", type="primary", key="gr_replace_do"):
                        _rp_err = evaluate_admin_password_input(rp)
                        if _rp_err is not None:
                            _invalidate_admin_password_fields(
                                _rp_err,
                                "gr_replace_pw",
                            )
                        else:
                            if os.path.exists("receipt_template.pdf"):
                                os.remove("receipt_template.pdf")
                            if os.path.exists("receipt_template.png"):
                                os.remove("receipt_template.png")
                            st.session_state.gr_replace_confirm = False
                            st.success("Template removed. You can upload a new file.")
                            _clear_password_field_keys("gr_replace_pw")
                            st.rerun()
                with rc2:
                    if st.button("Cancel", key="gr_replace_cancel"):
                        st.session_state.gr_replace_confirm = False
                        st.rerun()
            low = template_path_resolved.lower()
            if low.endswith((".png", ".jpg", ".jpeg", ".webp")):
                st.image(template_path_resolved, caption="Current template", width=420)
            else:
                st.caption(
                    f"PDF on disk: `{tpl_name}`. Generate a sample or a real receipt in the other tabs to preview the print layout."
                )
        else:
            st.caption("Upload your school’s receipt artwork once (PNG from Canva is usually the sharpest).")
            template_file = st.file_uploader(
                "Receipt template (PDF / PNG / JPEG)",
                type=["pdf", "png", "jpg", "jpeg"],
                key="gr_tpl_upload",
            )
            if template_file:
                template_path = (
                    "receipt_template.png" if template_file.type.startswith("image") else "receipt_template.pdf"
                )
                with open(template_path, "wb") as f:
                    f.write(template_file.getbuffer())
                st.success("Template saved.")
                st.rerun()

        with st.expander("PNG vs PDF, PyMuPDF, and layout JSON"):
            st.markdown(
                "- **PNG / JPEG:** Artwork is scaled on the page; VineLedger draws payment text on top.\n"
                "- **PDF:** Page 1 is rasterized when **PyMuPDF** is installed (`requirements.txt`). "
                "If it is missing, the app falls back to a plain data-only PDF.\n"
                "- **Match your blank boxes:** copy `receipt_layout.example.json` to **`receipt_layout.json`** "
                f"in `{_app_dir}` and set each field’s `x` and `y` as fractions from **0 to 1** of the scaled image. "
                "Use root `coordinate_origin`: **`top`** (y measured from the top of the art, like many design tools) "
                "or **`bottom`**. Each field can set its own `origin`.\n"
                "- **Template fields:** `grade` (below parent), `student_code` (under date on the right), `total_paid_today`, `school_balance_remaining`. "
                "Signature/stamp area is left empty on the artwork."
            )
            if os.path.isfile(_layout_example):
                st.caption(f"Example file: `{_layout_example}`")

        st.markdown('</div>', unsafe_allow_html=True)

    with tab_pay:
        if not template_path_resolved:
            st.info("Upload a template in **Template** first.")
        elif students_df.empty:
            st.info(
                "No students with payment totals yet. Add learners and record payments (for example via **Payment Management**), then return here."
            )
        else:
            GR_RECEIPT_PURPOSE_OPTIONS = get_receipt_purpose_options(conn)
            st.caption(
                "Search payments, tick **Print** for each row, then generate **one PDF** "
                "(two receipts per letter page when possible). Purpose defaults to **School Fees** unless you change it below."
            )

            sid_list = students_df["id"].astype(int).tolist()
            _ph = ",".join("?" * len(sid_list))
            pay_merged = pd.read_sql(
                f"""SELECT p.*,
                    s.name AS gr_student_name,
                    s.student_code AS gr_student_code,
                    s.grade AS gr_grade,
                    s.balance AS gr_balance,
                    s.total_paid AS gr_total_paid,
                    s.parent_name AS gr_parent_name,
                    s.parent_phone AS gr_parent_phone,
                    COALESCE(s.parent2_name, '') AS gr_parent2_name,
                    COALESCE(s.parent2_phone, '') AS gr_parent2_phone,
                    s.has_transport AS gr_has_transport,
                    s.transport_route AS gr_transport_route,
                    s.extra_classes AS gr_extra_classes,
                    s.has_meal AS gr_has_meal
                FROM payments p
                JOIN students s ON p.student_id = s.id
                WHERE s.id IN ({_ph})
                ORDER BY p.payment_date DESC
                LIMIT 600""",
                conn,
                params=sid_list,
            )

            def _gr_student_row_for_receipt(p):
                return pd.Series(
                    {
                        "id": int(p["student_id"]),
                        "name": p["gr_student_name"],
                        "student_code": p["gr_student_code"],
                        "grade": p["gr_grade"],
                        "balance": float(p["gr_balance"] or 0) if pd.notna(p.get("gr_balance")) else 0.0,
                        "total_paid": float(p["gr_total_paid"] or 0) if pd.notna(p.get("gr_total_paid")) else 0.0,
                        "parent_name": p.get("gr_parent_name") or "",
                        "parent_phone": p.get("gr_parent_phone") or "",
                        "parent2_name": p.get("gr_parent2_name") or "",
                        "parent2_phone": p.get("gr_parent2_phone") or "",
                        "has_transport": int(p.get("gr_has_transport") or 0),
                        "transport_route": p.get("gr_transport_route") or "",
                        "extra_classes": p.get("gr_extra_classes") or "",
                        "has_meal": int(p.get("gr_has_meal") or 0),
                    }
                )

            def _gr_payment_row_only(p):
                return pd.Series({k: p[k] for k in p.index if not str(k).startswith("gr_")})

            pay_search = st.text_input(
                "Search payments",
                placeholder="Student name, code, parent phone, bank ref, internal ID, amount…",
                help="Filters the list below. Empty search shows the 250 most recent payments.",
                key="gr_pay_search_main",
            )
            _needle = (pay_search or "").strip().lower()
            if not _needle:
                filtered = pay_merged.head(250)
            else:

                def _pay_row_hit(r):
                    _raw_code = str(r.get("gr_student_code", "") or "")
                    _disp_code = display_student_code(_raw_code.strip())
                    blob = " ".join(
                        str(r.get(c, "") or "")
                        for c in (
                            "gr_student_name",
                            "gr_student_code",
                            "gr_parent_name",
                            "gr_parent_phone",
                            "gr_parent2_name",
                            "gr_parent2_phone",
                            "transaction_id",
                            "internal_payment_id",
                            "description",
                            "payment_method",
                            "purpose",
                        )
                    ).lower()
                    blob = f"{blob} {_disp_code.lower()}".strip()
                    amt_s = str(r.get("amount", ""))
                    pid_s = str(int(r.get("id", 0) or 0))
                    return _needle in blob or _needle in amt_s or _needle in pid_s

                filtered = pay_merged[pay_merged.apply(_pay_row_hit, axis=1)]

            if len(filtered) > 200:
                st.caption(f"Showing the **200** newest of **{len(filtered)}** matches — narrow your search.")
                filtered = filtered.head(200).copy()
            else:
                st.caption(f"**{len(filtered)}** payment(s) in this list.")

            if pay_merged.empty:
                st.warning("No payment rows found for students with recorded payments.")
            elif filtered.empty:
                st.info("No payments match your search.")
            else:
                _id_tuple = tuple(int(x) for x in filtered["id"].tolist())
                if st.session_state.get("gr_pay_ids_tuple") != _id_tuple:
                    st.session_state.gr_pay_ids_tuple = _id_tuple
                    st.session_state.gr_editor_df = pd.DataFrame(
                        {
                            "Print": False,
                            "Student": filtered["gr_student_name"].astype(str),
                            "Code": filtered["gr_student_code"].map(_student_code_display_cell),
                            "Amount": filtered["amount"].map(lambda x: f"KSH {float(x):,.0f}"),
                            "Date": pd.to_datetime(filtered["payment_date"]).dt.strftime("%Y-%m-%d"),
                            "Method": (
                                filtered["payment_method"].fillna("").astype(str)
                                if "payment_method" in filtered.columns
                                else ""
                            ),
                            "Payment ID": filtered["id"].astype(int),
                            "Internal ID": (
                                filtered["internal_payment_id"].fillna("").astype(str)
                                if "internal_payment_id" in filtered.columns
                                else ""
                            ),
                        }
                    )

                _sa1, _sa2, _sa3 = st.columns([2, 1, 1])
                with _sa1:
                    purpose = st.selectbox(
                        "Purpose on all selected receipts",
                        GR_RECEIPT_PURPOSE_OPTIONS,
                        index=0,
                        key="gr_bulk_purpose_sel",
                    )
                with _sa2:
                    if st.button("Select all shown", key="gr_sel_all_btn"):
                        st.session_state.gr_editor_df["Print"] = True
                        st.rerun()
                with _sa3:
                    if st.button("Clear ticks", key="gr_sel_clear_btn"):
                        st.session_state.gr_editor_df["Print"] = False
                        st.rerun()

                mime_tpl = (
                    "image/png"
                    if template_path_resolved.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                    else "application/pdf"
                )

                edited = st.data_editor(
                    st.session_state.gr_editor_df,
                    column_config={
                        "Print": st.column_config.CheckboxColumn(
                            "Print",
                            help="Include this payment in the PDF",
                            default=False,
                        ),
                        "Payment ID": st.column_config.NumberColumn("Payment ID", format="%d", disabled=True),
                    },
                    disabled=["Student", "Code", "Amount", "Date", "Method", "Internal ID"],
                    hide_index=True,
                    use_container_width=True,
                    num_rows="fixed",
                    key="gr_bulk_pay_editor_widget",
                )
                st.session_state.gr_editor_df = edited.copy()

                if st.button(
                    "Generate filled PDF for ticked rows",
                    type="primary",
                    use_container_width=True,
                    key="gr_bulk_filled_btn",
                ):
                    _sel_ids = edited.loc[
                        edited["Print"].fillna(False).astype(bool), "Payment ID"
                    ].astype(int).tolist()
                    if not _sel_ids:
                        st.warning("Tick **Print** for at least one payment.")
                    else:
                        _rows_sorted = filtered[filtered["id"].isin(_sel_ids)].sort_values(
                            "payment_date", ascending=False
                        )
                        _pairs = []
                        for _, pr in _rows_sorted.iterrows():
                            _stu = _gr_student_row_for_receipt(pr)
                            _pay = _gr_payment_row_only(pr).copy()
                            _pay["purpose"] = purpose
                            _pairs.append((_stu, _pay))
                        try:
                            _bulk_path = create_filled_templates_bulk(
                                template_path_resolved, mime_tpl, _pairs
                            )
                            st.success(
                                f"Created **{len(_pairs)}** receipt(s) in one document "
                                f"({'two per page' if len(_pairs) > 1 else 'one page'})."
                            )
                            _friendly = f"filled_receipts_{len(_pairs)}.pdf"
                            _render_receipt_pdf_actions(
                                _bulk_path,
                                download_label="Download PDF",
                                download_file_name=_friendly,
                                print_button_key="gr_bulk_print_btn",
                                download_button_key="gr_bulk_filled_dlb",
                                viewer_key="gr_bulk_filled_pdf_view",
                            )
                            _audit_log(
                                conn,
                                "Receipt",
                                f"Generated bulk filled-template PDF with {len(_pairs)} receipt(s) "
                                f"(purpose: {purpose}).",
                                save_mode="immediate",
                            )
                        except Exception as _e:
                            st.error(str(_e))

                with st.expander("Plain system receipts (no artwork)", expanded=False):
                    st.caption(
                        "Same ticked rows and purpose — built-in layout only, two receipts per page when possible."
                    )
                    if st.button("Generate plain PDF for ticked rows", key="gr_bulk_plain_btn"):
                        _sel_ids = edited.loc[
                        edited["Print"].fillna(False).astype(bool), "Payment ID"
                    ].astype(int).tolist()
                        if not _sel_ids:
                            st.warning("Tick **Print** for at least one payment.")
                        else:
                            try:
                                _rows_sorted = filtered[filtered["id"].isin(_sel_ids)].sort_values(
                                    "payment_date", ascending=False
                                )
                                _pairs = []
                                for _, pr in _rows_sorted.iterrows():
                                    _stu = _gr_student_row_for_receipt(pr)
                                    _pay = _gr_payment_row_only(pr).copy()
                                    _pay["purpose"] = purpose
                                    _pairs.append((_stu, _pay))
                                _plain_bulk = generate_bulk_plain_receipts_pdf(_pairs)
                                st.success("Plain combined PDF created.")
                                _friendly_plain = f"plain_receipts_{len(_pairs)}.pdf"
                                _render_receipt_pdf_actions(
                                    _plain_bulk,
                                    download_label="Download plain combined PDF",
                                    download_file_name=_friendly_plain,
                                    print_button_key="gr_bulk_plain_print_btn",
                                    download_button_key="gr_bulk_plain_dlb",
                                    viewer_key="gr_bulk_plain_pdf_view",
                                )
                                _audit_log(
                                    conn,
                                    "Receipt",
                                    f"Generated bulk plain PDF with {len(_pairs)} receipt(s).",
                                    save_mode="immediate",
                                )
                            except Exception as _e:
                                st.error(str(_e))

    with tab_sample:
        if not template_path_resolved:
            st.info("Upload a template in **Template** first.")
        else:
            st.caption("Fictional student and payment — nothing is saved to the database.")
            _sample_purpose_opts = get_receipt_purpose_options(conn)
            sample_purpose = st.selectbox(
                "Sample purpose",
                _sample_purpose_opts,
                key="gr_sample_purpose_sel",
            )
            if st.button("Build sample PDF", type="primary", key="gr_sample_generate_btn"):
                sample_student = pd.Series(
                    {
                        "id": -1,
                        "student_code": "0999",
                        "name": "Wanjiku Sample",
                        "parent_name": "Parent Mwangi",
                        "parent_phone": "254712345678",
                        "parent2_name": "Guardian Wanjiku",
                        "parent2_phone": "254798765432",
                        "grade": "Grade 3",
                        "balance": 42500.0,
                        "total_paid": 18500.0,
                        "has_transport": 1,
                        "transport_route": "Route A",
                        "extra_classes": "",
                        "has_meal": 0,
                    }
                )
                sample_payment = pd.Series(
                    {
                        "id": 900001,
                        "student_id": -1,
                        "amount": 8500.0,
                        "payment_date": "2026-05-01 09:15:00",
                        "transaction_id": "MPX-SAMPLE-001",
                        "description": "Sample fee payment",
                        "matched": 1,
                    }
                )
                sample_pay = sample_payment.copy()
                sample_pay["purpose"] = sample_purpose
                low = template_path_resolved.lower()
                mime_tpl = "image/png" if low.endswith((".png", ".jpg", ".jpeg", ".webp")) else "application/pdf"
                try:
                    out_path = create_filled_template(
                        template_path_resolved, sample_student, sample_pay, mime_tpl
                    )
                    st.session_state.sample_receipt_path = out_path
                    st.success("Sample PDF created.")
                except Exception as e:
                    st.session_state.sample_receipt_path = None
                    st.error(f"Could not build sample receipt: {e}")

            _srp = st.session_state.get("sample_receipt_path")
            if _srp and os.path.isfile(_srp):
                _render_receipt_pdf_actions(
                    _srp,
                    download_label="Download sample",
                    download_file_name="vinegrape_sample_receipt.pdf",
                    print_button_key="gr_sample_print_btn",
                    download_button_key="gr_sample_download_btn",
                    viewer_key="gr_sample_pdf_view",
                )
                if st.button("Delete sample file from disk", key="gr_sample_clear_btn"):
                    try:
                        if os.path.isfile(_srp):
                            os.remove(_srp)
                    except OSError:
                        pass
                    st.session_state.sample_receipt_path = None
                    st.rerun()

elif menu == "Payment History":
    st.markdown('<h2 class="section-header">Internal Receipt Records & Payment History</h2>', unsafe_allow_html=True)
    
    # Filters section
    st.markdown('<div class="form-container">', unsafe_allow_html=True)
    st.markdown('<h3 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Filter Payments</h3>', unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns(3)
    with col1:
        date_filter = st.selectbox("Date Range", ["All Time", "Last 30 Days", "Last 90 Days", "This Year"], 
                                  help="Filter payments by time period")
    with col2:
        grade_filter = st.selectbox("Grade Filter", ["All Grades"] + [f"Grade {i}" for i in range(1, 13)], 
                                  help="Filter by student grade")
    with col3:
        payment_status = st.selectbox("Payment Status", ["All", "Matched", "Unmatched"], 
                                    help="Filter by payment matching status")
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="form-container">', unsafe_allow_html=True)
    st.markdown('<h4 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Search Payments</h4>', unsafe_allow_html=True)
    search_col1, search_col2 = st.columns(2)
    with search_col1:
        search_term = st.text_input(
            "Search by Name/Code/Phone/Transaction ID",
            placeholder="Student name, code, phone, internal transaction ID, bank reference…",
            help="Matches student name/code/parent phones, internal payment ID, bank reference, description, method, or purpose (together with the filters above).",
            key="payment_history_search_term",
        )
    with search_col2:
        st.button("Search", type="primary", key="payment_history_search_btn")
    st.markdown('</div>', unsafe_allow_html=True)

    # Build one SQL query: date/grade/status filters + optional search (parameterized)
    query = """
        SELECT p.*, s.name as student_name, s.student_code, s.grade, s.parent_name
        FROM payments p
        JOIN students s ON p.student_id = s.id
        WHERE 1=1
    """
    params = []

    if date_filter == "Last 30 Days":
        query += " AND p.payment_date >= date('now', '-30 days')"
    elif date_filter == "Last 90 Days":
        query += " AND p.payment_date >= date('now', '-90 days')"
    elif date_filter == "This Year":
        query += " AND p.payment_date >= date('now', 'start of year')"

    if grade_filter != "All Grades":
        query += " AND s.grade = ?"
        params.append(grade_filter)

    if payment_status == "Matched":
        query += " AND p.matched = 1"
    elif payment_status == "Unmatched":
        query += " AND p.matched = 0"

    _st = (search_term or "").strip()
    if _st:
        _like = f"%{_st}%"
        query += (
            " AND (s.name LIKE ? OR s.student_code LIKE ? OR s.parent_name LIKE ? OR "
            "s.parent_phone LIKE ? OR s.parent2_name LIKE ? OR s.parent2_phone LIKE ? OR "
            "IFNULL(p.internal_payment_id, '') LIKE ? OR IFNULL(p.transaction_id, '') LIKE ? OR "
            "IFNULL(p.description, '') LIKE ? OR IFNULL(p.payment_method, '') LIKE ? OR IFNULL(p.purpose, '') LIKE ?)"
        )
        params.extend([_like, _like, _like, _like, _like, _like, _like, _like, _like, _like, _like])

    query += " ORDER BY p.payment_date DESC"

    payments_df = pd.read_sql(query, conn, params=params if params else None)
    
    if not payments_df.empty:
        # Summary statistics with better styling
        st.markdown('<h3 class="section-header">Payment Analytics</h3>', unsafe_allow_html=True)
        
        col1, col2, col3 = st.columns(3)

        with col1:
            total_payments = len(payments_df)
            st.markdown(f"""
            <div class="metric-card" style="border-left-color: var(--primary);">
                <div style="font-size: 1.25rem; font-weight: 700; color: var(--primary);">{total_payments}</div>
                <div class="vine-help-text" style="margin-top: 0.25rem;">Transactions</div>
            </div>
            """, unsafe_allow_html=True)

        with col2:
            matched_payments = payments_df['matched'].sum()
            match_rate = (matched_payments / total_payments * 100) if total_payments > 0 else 0
            st.markdown(f"""
            <div class="metric-card" style="border-left-color: var(--info);">
                <div style="font-size: 1.25rem; font-weight: 700; color: var(--info);">{match_rate:.1f}%</div>
                <div class="vine-help-text" style="margin-top: 0.25rem;">Match Rate</div>
            </div>
            """, unsafe_allow_html=True)

        with col3:
            avg_payment = payments_df['amount'].mean()
            st.markdown(f"""
            <div class="metric-card" style="border-left-color: var(--secondary);">
                <div style="font-size: 1.25rem; font-weight: 700; color: var(--secondary);">KSH {avg_payment:,.0f}</div>
                <div class="vine-help-text" style="margin-top: 0.25rem;">Average Payment</div>
            </div>
            """, unsafe_allow_html=True)
        
        # Payment trends chart (adaptive buckets: day → week → month → year by data span)
        if len(payments_df) > 1:
            st.markdown('<h3 class="section-header">Payment Trends</h3>', unsafe_allow_html=True)

            trend_df, trend_hint = _payment_trend_bucket_amounts(payments_df)
            if trend_df is not None and not trend_df.empty:
                if trend_hint:
                    st.caption(trend_hint)
                st.bar_chart(trend_df, use_container_width=True)
        
        # Detailed payment records with enhanced display
        st.markdown('<h3 class="section-header">Detailed Payment Records</h3>', unsafe_allow_html=True)

        def get_status_badge(matched):
            if matched:
                return '<span class="status-badge status-paid">Matched</span>'
            else:
                return '<span class="status-badge status-unmatched">Unmatched</span>'

        _pm = (
            payments_df["payment_method"].fillna("").astype(str).str.strip()
            if "payment_method" in payments_df.columns
            else pd.Series([""] * len(payments_df), index=payments_df.index)
        )
        _pur = (
            payments_df["purpose"].fillna("").astype(str).str.strip()
            if "purpose" in payments_df.columns
            else pd.Series([""] * len(payments_df), index=payments_df.index)
        )
        _internal_tid = (
            payments_df["internal_payment_id"].fillna("").astype(str).str.strip()
            if "internal_payment_id" in payments_df.columns
            else pd.Series([""] * len(payments_df), index=payments_df.index)
        )
        _internal_tid = _internal_tid.replace("", "—")
        _txn_ref = (
            payments_df["transaction_id"].fillna("").astype(str).str.strip()
            if "transaction_id" in payments_df.columns
            else pd.Series([""] * len(payments_df), index=payments_df.index)
        )
        _txn_ref = _txn_ref.replace("", "—")
        _desc = (
            payments_df["description"].fillna("").astype(str).str.strip()
            if "description" in payments_df.columns
            else pd.Series([""] * len(payments_df), index=payments_df.index)
        )
        _desc = _desc.replace("", "—")
        _stu_raw = (
            payments_df["student_code"].fillna("").astype(str).str.strip()
            if "student_code" in payments_df.columns
            else pd.Series([""] * len(payments_df), index=payments_df.index)
        )
        _stu_code = _stu_raw.map(_student_code_display_cell).replace("", "—")

        display_df = pd.DataFrame(
            {
                "Student Name": payments_df["student_name"].astype(str),
                "Student code": _stu_code,
                "Grade": payments_df["grade"].fillna("").astype(str).replace("", "—"),
                "Amount": payments_df["amount"].apply(lambda x: f"KSH {float(x):,.0f}"),
                "Date": pd.to_datetime(payments_df["payment_date"]).dt.strftime("%Y-%m-%d"),
                "Purpose": _pur.replace("", "—"),
                "Payment method": _pm.replace("", "—"),
                "Reference / transaction ID": _txn_ref,
                "Internal payment ID": _internal_tid,
                "Other details": _desc,
                "Status": payments_df["matched"].apply(get_status_badge),
            }
        )

        _table_cols = [
            "Student Name",
            "Student code",
            "Grade",
            "Amount",
            "Date",
            "Purpose",
            "Payment method",
            "Reference / transaction ID",
            "Internal payment ID",
            "Other details",
            "Status",
        ]

        st.markdown('<div class="dataframe-container">', unsafe_allow_html=True)
        st.dataframe(
            display_df[_table_cols],
            use_container_width=True,
        )
        st.markdown('</div>', unsafe_allow_html=True)

        # Export functionality
        st.markdown('<h3 class="section-header">Export Data</h3>', unsafe_allow_html=True)

        _csv_df = display_df[_table_cols].copy()
        _csv_df["Status"] = payments_df["matched"].map(lambda m: "Matched" if m else "Unmatched")
        csv_data = _csv_df.to_csv(index=False)
        st.download_button(
            label="Download Payment History (CSV)",
            data=csv_data,
            file_name=f"payment_history_{date_filter.replace(' ', '_').lower()}.csv",
            mime="text/csv",
            type="primary"
        )
        
    else:
        st.markdown('<div class="info-message">No payment records found matching the selected criteria.</div>', unsafe_allow_html=True)

elif menu == "Add Staff":
    if not check_password():
        st.stop()
    st.markdown('<h2 class="section-header">Staff Registration</h2>', unsafe_allow_html=True)
    
    # Get next staff ID
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(CAST(SUBSTR(staff_id, 4) AS INTEGER)) FROM staff WHERE staff_id LIKE 'STF%'")
    last_id = cursor.fetchone()[0] or 0
    next_staff_id = f"STF{last_id + 1:04d}"
    
    st.markdown('<div class="form-container">', unsafe_allow_html=True)
    _dept_opts_add = [
        "Administration",
        "Teaching",
        "Support Staff",
        "Finance",
        "IT",
        "Maintenance",
    ]
    st.caption(
        "Pick **Department** first — if you choose **Teaching**, subject and grade fields appear "
        "below immediately (they sit outside the form so the page updates before you click **Register Staff**)."
    )
    department = st.selectbox(
        "Department *",
        _dept_opts_add,
        key="add_staff_department_choice",
        help="Teaching shows subjects and grades below; other departments skip those fields.",
    )
    if department == "Teaching":
        st.markdown(
            '<h4 style="color: var(--text); margin: 1rem 0 0.5rem 0; font-weight: 600;">Teaching assignment</h4>'
            '<p class="vine-help-text">Required for Teaching department: list subjects and the grade levels they apply to.</p>',
            unsafe_allow_html=True,
        )
        tcol1, tcol2 = st.columns(2)
        with tcol1:
            teaching_subjects = st.text_area(
                "Subjects *",
                height=100,
                placeholder="e.g. Mathematics, Physics",
                help="Subjects this staff member will teach",
                key="add_staff_teaching_subjects",
            )
        with tcol2:
            teaching_grades = st.text_area(
                "Grades *",
                height=100,
                placeholder="e.g. Grade 7, Grade 8, Form 2",
                help="Grade levels or classes for those subjects",
                key="add_staff_teaching_grades",
            )
    else:
        teaching_subjects = ""
        teaching_grades = ""

    with st.form("add_staff_form"):
        st.markdown('<h3 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Staff Details</h3>', unsafe_allow_html=True)
        
        col1, col2 = st.columns(2)
        
        with col1:
            name = st.text_input("Full Name *", help="Enter staff member's full name")
            email = st.text_input("Email Address *", help="Enter staff member's email")
            phone = st.text_input(
                "Phone Number *",
                help="Kenya: 01… / 07…, 254…, or +254… — stored as 254… when recognized.",
            )
            position = st.text_input("Position *", help="Enter job position/title")
        
        with col2:
            salary = st.number_input(
                "Monthly Salary (KSH) *",
                min_value=0.0,
                step=1000.0,
                help="Enter monthly salary",
            )
            hire_date = st.date_input("Hire Date", help="Select hire date")

        st.markdown(
            '<h4 style="color: var(--text); margin: 1rem 0 0.5rem 0; font-weight: 600;">Bank details (optional)</h4>'
            '<p class="vine-help-text">Payroll or reimbursement details — leave blank if not applicable.</p>',
            unsafe_allow_html=True,
        )
        bcol1, bcol2 = st.columns(2)
        with bcol1:
            bank_name = st.text_input("Bank name", help="Name of the bank")
            bank_branch = st.text_input("Branch (optional)", help="Branch name or code")
        with bcol2:
            bank_account_number = st.text_input("Account number", help="Bank account number")
        bank_other_details = st.text_area(
            "Other bank / payment details (optional)",
            height=70,
            placeholder="e.g. account name as registered, M-Pesa till, SWIFT/BIC, notes",
            help="Any other payment-related information",
        )
        
        st.markdown('</div>', unsafe_allow_html=True)
        
        col1, col2 = st.columns(2)
        with col1:
            submitted = st.form_submit_button("Register Staff", type="primary", use_container_width=True)
        with col2:
            clear_form = st.form_submit_button("Clear Form", use_container_width=True)
        
        if clear_form:
            for _k in (
                "add_staff_department_choice",
                "add_staff_teaching_subjects",
                "add_staff_teaching_grades",
            ):
                st.session_state.pop(_k, None)
            st.rerun()
        
        if submitted:
            _ts = (teaching_subjects or "").strip()
            _tg = (teaching_grades or "").strip()
            if not all([name, email, phone, position]):
                st.markdown('<div class="warning-message">Please fill in all required fields!</div>', unsafe_allow_html=True)
            elif department == "Teaching" and (not _ts or not _tg):
                st.markdown(
                    '<div class="warning-message">For <strong>Teaching</strong>, please fill in both <strong>Subjects</strong> and <strong>Grades</strong>.</div>',
                    unsafe_allow_html=True,
                )
            else:
                try:
                    cursor.execute(
                        """
                        INSERT INTO staff (
                            staff_id, name, email, phone, position, department, salary, hire_date,
                            bank_name, bank_account_number, bank_branch, bank_other_details,
                            teaching_subjects, teaching_grades
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            next_staff_id,
                            name,
                            email,
                            normalize_kenya_msisdn(phone),
                            position,
                            department,
                            salary,
                            hire_date,
                            (bank_name or "").strip(),
                            (bank_account_number or "").strip(),
                            (bank_branch or "").strip(),
                            (bank_other_details or "").strip(),
                            _ts if department == "Teaching" else "",
                            _tg if department == "Teaching" else "",
                        ),
                    )
                    new_staff_row_id = int(cursor.lastrowid)
                    
                    conn.commit()
                    _audit_log(
                        conn,
                        "Add Staff",
                        f"Registered staff {next_staff_id} ({name}), {position}, {department}.",
                        save_mode="immediate",
                        entity_type="staff",
                        entity_id=new_staff_row_id,
                        entity_code=str(next_staff_id),
                        detail=json.dumps(
                            {
                                "email": email,
                                "department": department,
                                "teaching": department == "Teaching",
                                "has_bank_details": bool(
                                    (bank_name or "").strip()
                                    or (bank_account_number or "").strip()
                                    or (bank_branch or "").strip()
                                    or (bank_other_details or "").strip()
                                ),
                            },
                            default=str,
                        ),
                    )
                    st.markdown(f'<div class="success-message">Staff member <strong>{name}</strong> registered successfully with ID: <strong>{next_staff_id}</strong></div>', unsafe_allow_html=True)
                    st.rerun()
                except Exception as e:
                    st.markdown(f'<div class="warning-message">Error registering staff: {str(e)}</div>', unsafe_allow_html=True)

elif menu == "View Staff":
    if not check_password():
        st.stop()
    st.markdown('<h2 class="section-header">Staff Records</h2>', unsafe_allow_html=True)
    
    staff_df = pd.read_sql("SELECT * FROM staff ORDER BY name", conn)
    
    if not staff_df.empty:
        # Summary statistics
        st.markdown('<div class="form-container">', unsafe_allow_html=True)
        st.markdown('<h3 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Staff Overview</h3>', unsafe_allow_html=True)
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            total_staff = len(staff_df)
            st.markdown(f"""
            <div class="metric-card" style="border-left-color: var(--primary);">
                <div style="font-size: 1.25rem; font-weight: 700; color: var(--primary);">{total_staff}</div>
                <div class="vine-help-text" style="margin-top: 0.25rem;">Total Staff</div>
            </div>
            """, unsafe_allow_html=True)
        
        with col2:
            active_staff = len(staff_df[staff_df['status'] == 'Active'])
            st.markdown(f"""
            <div class="metric-card" style="border-left-color: var(--success);">
                <div style="font-size: 1.25rem; font-weight: 700; color: var(--success);">{active_staff}</div>
                <div class="vine-help-text" style="margin-top: 0.25rem;">Active Staff</div>
            </div>
            """, unsafe_allow_html=True)
        
        with col3:
            total_salary = staff_df['salary'].sum()
            st.markdown(f"""
            <div class="metric-card" style="border-left-color: var(--info);">
                <div style="font-size: 1.25rem; font-weight: 700; color: var(--info);">KSH {total_salary:,.0f}</div>
                <div class="vine-help-text" style="margin-top: 0.25rem;">Total Monthly Payroll</div>
            </div>
            """, unsafe_allow_html=True)
        
        st.markdown('</div>', unsafe_allow_html=True)
        
        # Staff list
        st.markdown('<h3 class="section-header">Staff Directory</h3>', unsafe_allow_html=True)
        
        # Format for display
        display_df = staff_df.copy()
        display_df['salary'] = display_df['salary'].apply(lambda x: f"KSH {x:,.0f}")
        display_df['hire_date'] = pd.to_datetime(display_df['hire_date']).dt.strftime('%Y-%m-%d')
        
        # Add status badges
        def get_status_badge(status):
            colors = {"Active": "#059669", "Transferred": "#d97706", "Scheduled for Deletion": "#dc2626"}
            color = colors.get(status, "#6b7280")
            return f'<span class="status-badge" style="background: {color}20; color: {color}">{status}</span>'
        
        display_df['status'] = display_df['status'].apply(get_status_badge)

        _dir_cols = [
            "staff_id",
            "name",
            "position",
            "department",
            "salary",
            "status",
        ]
        for _opt in ("teaching_subjects", "teaching_grades", "bank_name", "bank_account_number"):
            if _opt in display_df.columns and _opt not in _dir_cols:
                _dir_cols.append(_opt)

        st.markdown('<div class="dataframe-container">', unsafe_allow_html=True)
        st.dataframe(display_df[_dir_cols], use_container_width=True)
        st.markdown('</div>', unsafe_allow_html=True)
        
        # Export functionality
        st.markdown('<h3 class="section-header">Export Data</h3>', unsafe_allow_html=True)
        
        _export_cols = ["staff_id", "name", "email", "phone", "position", "department", "salary", "status", "hire_date"]
        for _opt in (
            "bank_name",
            "bank_account_number",
            "bank_branch",
            "bank_other_details",
            "teaching_subjects",
            "teaching_grades",
        ):
            if _opt in staff_df.columns:
                _export_cols.append(_opt)
        csv_data = staff_df[[c for c in _export_cols if c in staff_df.columns]].to_csv(index=False)
        st.download_button(
            label="Download Staff Records (CSV)",
            data=csv_data,
            file_name="staff_records.csv",
            mime="text/csv",
            type="primary"
        )
    else:
        st.markdown('<div class="info-message">No staff records found. Add staff members to get started.</div>', unsafe_allow_html=True)

elif menu == "Manage Staff":
    if not check_password():
        st.stop()
    st.markdown('<h2 class="section-header">Staff Management</h2>', unsafe_allow_html=True)
    
    # Get all staff
    staff_df = pd.read_sql("SELECT * FROM staff ORDER BY name", conn)
    
    if not staff_df.empty:
        st.markdown('<div class="form-container">', unsafe_allow_html=True)
        st.markdown('<h3 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Select Staff Member for Management</h3>', unsafe_allow_html=True)
        
        selected_staff = st.selectbox(
            "Choose a staff member to manage",
            options=staff_df.index.tolist(),
            format_func=lambda x: f"{staff_df.iloc[x]['name']} ({staff_df.iloc[x]['staff_id']}) - {staff_df.iloc[x]['position']} - Status: {staff_df.iloc[x]['status']}",
            help="Select a staff member for management operations"
        )
        
        st.markdown('</div>', unsafe_allow_html=True)
        
        if selected_staff is not None:
            staff = staff_df.iloc[selected_staff]
            
            # Staff details
            st.markdown('<div class="dataframe-container">', unsafe_allow_html=True)
            _bnk_name = _staff_series_text(staff, "bank_name")
            _bnk_acct = _staff_series_text(staff, "bank_account_number")
            _bnk_br = _staff_series_text(staff, "bank_branch")
            _bnk_other = _staff_series_text(staff, "bank_other_details")
            _t_subj = _staff_series_text(staff, "teaching_subjects")
            _t_gr = _staff_series_text(staff, "teaching_grades")
            _bank_lines = ""
            if any((_bnk_name, _bnk_acct, _bnk_br, _bnk_other)):
                _bank_lines = (
                    "<strong>Bank:</strong> "
                    + html_module.escape(_bnk_name or "—")
                    + " · <strong>Account:</strong> "
                    + html_module.escape(_bnk_acct or "—")
                    + "<br/>"
                )
                if _bnk_br:
                    _bank_lines += f"<strong>Branch:</strong> {html_module.escape(_bnk_br)}<br/>"
                if _bnk_other:
                    _bank_lines += f"<strong>Other payment details:</strong> {html_module.escape(_bnk_other)}<br/>"
            _teach_lines = ""
            if str(staff.get("department") or "") == "Teaching":
                _teach_lines = (
                    "<strong>Subjects:</strong> "
                    + html_module.escape(_t_subj or "—")
                    + "<br/><strong>Grades:</strong> "
                    + html_module.escape(_t_gr or "—")
                    + "<br/>"
                )
            st.markdown(f'''
            <h4 style="color: var(--text); margin-bottom: 0.5rem; font-weight: 600;">Staff Information</h4>
            <div style="line-height: 1.6;">
                <strong>Name:</strong> {staff['name']}<br/>
                <strong>Staff ID:</strong> {staff['staff_id']}<br/>
                <strong>Position:</strong> {staff['position']}<br/>
                <strong>Department:</strong> {staff['department']}<br/>
                <strong>Status:</strong> <span style="color: {"#059669" if staff['status'] == 'Active' else "#dc2626"};">{staff['status']}</span><br/>
                <strong>Email:</strong> {staff['email']}<br/>
                <strong>Phone:</strong> {staff['phone']}<br/>
                <strong>Salary:</strong> KSH {staff['salary']:,.0f}<br/>
                {_teach_lines}{_bank_lines}
            </div>
            ''', unsafe_allow_html=True)
            st.markdown('</div>', unsafe_allow_html=True)
            
            # Management operations
            st.markdown('<div class="form-container">', unsafe_allow_html=True)
            st.markdown('<h3 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Management Operations</h3>', unsafe_allow_html=True)
            
            # Edit Staff Record Section
            if st.button("Edit Staff Record", type="primary", use_container_width=True):
                st.session_state.edit_staff_mode = True
            
            if st.session_state.get('edit_staff_mode', False):
                st.markdown('<h4 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Edit Staff Information</h4>', unsafe_allow_html=True)
                
                # Password verification
                if not st.session_state.get('staff_edit_authenticated', False):
                    password = st.text_input("Enter admin password:", type="password", key="staff_edit_password")
                    if password:
                        _se_err = evaluate_admin_password_input(password)
                        if _se_err is None:
                            st.session_state.staff_edit_authenticated = True
                            _clear_password_field_keys("staff_edit_password")
                            st.rerun()
                        else:
                            _invalidate_admin_password_fields(
                                _se_err,
                                "staff_edit_password",
                            )
                else:
                    _edit_sid = int(staff["id"])
                    _dept_opts_edit = [
                        "Administration",
                        "Teaching",
                        "Support Staff",
                        "Finance",
                        "IT",
                        "Maintenance",
                    ]
                    _dept_ix_edit = (
                        _dept_opts_edit.index(staff["department"])
                        if staff["department"] in _dept_opts_edit
                        else 0
                    )
                    st.caption(
                        "Change **Department** here first — **Teaching** shows subjects and grades below "
                        "before you submit **Update Staff**."
                    )
                    department = st.selectbox(
                        "Department *",
                        _dept_opts_edit,
                        index=_dept_ix_edit,
                        key=f"edit_staff_department_{_edit_sid}",
                        help="Teaching reveals subject and grade fields outside the form so they update immediately.",
                    )
                    if department == "Teaching":
                        st.markdown(
                            '<h4 style="color: var(--text); margin: 1rem 0 0.5rem 0; font-weight: 600;">Teaching assignment</h4>'
                            '<p class="vine-help-text">Required while department is Teaching.</p>',
                            unsafe_allow_html=True,
                        )
                        _ec1, _ec2 = st.columns(2)
                        with _ec1:
                            ed_teaching_subjects = st.text_area(
                                "Subjects *",
                                value=_staff_series_text(staff, "teaching_subjects"),
                                height=100,
                                help="Subjects this staff member teaches",
                                key=f"edit_staff_subj_{_edit_sid}",
                            )
                        with _ec2:
                            ed_teaching_grades = st.text_area(
                                "Grades *",
                                value=_staff_series_text(staff, "teaching_grades"),
                                height=100,
                                help="Grade levels or classes",
                                key=f"edit_staff_gr_{_edit_sid}",
                            )
                    else:
                        ed_teaching_subjects = ""
                        ed_teaching_grades = ""

                    # Show edit form
                    with st.form("edit_staff_form"):
                        col1, col2 = st.columns(2)
                        
                        with col1:
                            name = st.text_input("Full Name *", value=staff['name'], help="Enter staff member's full name")
                            email = st.text_input("Email Address *", value=staff['email'], help="Enter staff member's email")
                            phone = st.text_input(
                                "Phone Number *",
                                value=staff['phone'],
                                help="Kenya: 01… / 07…, 254…, or +254… — stored as 254… when recognized.",
                            )
                            position = st.text_input("Position *", value=staff['position'], help="Enter job position/title")
                        
                        with col2:
                            salary = st.number_input("Monthly Salary (KSH) *", min_value=0.0, step=1000.0, 
                                                     value=float(staff['salary']), help="Enter monthly salary")
                            hire_date = st.date_input("Hire Date", value=pd.to_datetime(staff['hire_date']).date(), help="Select hire date")

                        st.markdown(
                            '<h4 style="color: var(--text); margin: 1rem 0 0.5rem 0; font-weight: 600;">Bank details (optional)</h4>',
                            unsafe_allow_html=True,
                        )
                        _bc1, _bc2 = st.columns(2)
                        with _bc1:
                            bank_name = st.text_input(
                                "Bank name",
                                value=_staff_series_text(staff, "bank_name"),
                            )
                            bank_branch = st.text_input(
                                "Branch (optional)",
                                value=_staff_series_text(staff, "bank_branch"),
                            )
                        with _bc2:
                            bank_account_number = st.text_input(
                                "Account number",
                                value=_staff_series_text(staff, "bank_account_number"),
                            )
                        bank_other_details = st.text_area(
                            "Other bank / payment details (optional)",
                            value=_staff_series_text(staff, "bank_other_details"),
                            height=70,
                        )
                        
                        col1, col2 = st.columns(2)
                        with col1:
                            submitted = st.form_submit_button("Update Staff", type="primary", use_container_width=True)
                        with col2:
                            cancel = st.form_submit_button("Cancel", use_container_width=True)
                        
                        if submitted:
                            _ed_ts = (ed_teaching_subjects or "").strip() if department == "Teaching" else ""
                            _ed_tg = (ed_teaching_grades or "").strip() if department == "Teaching" else ""
                            if department == "Teaching" and (not _ed_ts or not _ed_tg):
                                st.markdown(
                                    '<div class="warning-message">For <strong>Teaching</strong>, please fill in both <strong>Subjects</strong> and <strong>Grades</strong>.</div>',
                                    unsafe_allow_html=True,
                                )
                            else:
                                try:
                                    conn.execute(
                                        """
                                        UPDATE staff 
                                        SET name = ?, email = ?, phone = ?, position = ?, 
                                            department = ?, salary = ?, hire_date = ?,
                                            bank_name = ?, bank_account_number = ?, bank_branch = ?, bank_other_details = ?,
                                            teaching_subjects = ?, teaching_grades = ?,
                                            updated_at = CURRENT_TIMESTAMP
                                        WHERE id = ?
                                        """,
                                        (
                                            name,
                                            email,
                                            normalize_kenya_msisdn(phone),
                                            position,
                                            department,
                                            salary,
                                            hire_date,
                                            (bank_name or "").strip(),
                                            (bank_account_number or "").strip(),
                                            (bank_branch or "").strip(),
                                            (bank_other_details or "").strip(),
                                            _ed_ts,
                                            _ed_tg,
                                            staff["id"],
                                        ),
                                    )
                                    
                                    conn.commit()
                                    _audit_log(
                                        conn,
                                        "Manage Staff",
                                        f"Updated staff record {staff['staff_id']} ({name}): position {position}, department {department}, salary KSH {float(salary):,.0f}.",
                                        save_mode="immediate",
                                        entity_type="staff",
                                        entity_id=int(staff["id"]),
                                        entity_code=str(staff["staff_id"]),
                                    )
                                    st.markdown('<div class="success-message">Staff record updated successfully!</div>', unsafe_allow_html=True)
                                    # Reset edit mode
                                    st.session_state.edit_staff_mode = False
                                    st.session_state.staff_edit_authenticated = False
                                    st.rerun()
                                except Exception as e:
                                    st.markdown(f'<div class="warning-message">Error updating staff: {str(e)}</div>', unsafe_allow_html=True)
                        
                        if cancel:
                            # Reset edit mode
                            st.session_state.edit_staff_mode = False
                            st.session_state.staff_edit_authenticated = False
                            st.rerun()
            
            st.markdown('<hr style="margin: 2rem 0; border: none; border-top: 1px solid var(--border);">', unsafe_allow_html=True)
            
            col1, col2 = st.columns(2)
            
            with col1:
                if st.button("Mark as Transferred", type="secondary", use_container_width=True):
                    # Password verification
                    password = st.text_input("Enter admin password:", type="password", key="staff_transfer_password")
                    if password:
                        _st_err = evaluate_admin_password_input(password)
                        if _st_err is None:
                            try:
                                # Mark as transferred
                                transfer_reason = st.text_input("Transfer reason (optional):", key="staff_transfer_reason")
                                deletion_date = pd.Timestamp.now() + pd.Timedelta(days=7)
                                
                                conn.execute('''
                                    UPDATE staff 
                                    SET status = 'Transferred', 
                                        deletion_scheduled = ?,
                                        updated_at = CURRENT_TIMESTAMP
                                    WHERE id = ?
                                ''', (deletion_date, staff['id']))
                                
                                conn.commit()
                                st.markdown('<div class="success-message">Staff member marked as transferred. Records will be deleted in 7 days.</div>', unsafe_allow_html=True)
                                _clear_password_field_keys("staff_transfer_password")
                                st.rerun()
                            except Exception as e:
                                st.markdown(f'<div class="warning-message">Error: {str(e)}</div>', unsafe_allow_html=True)
                        else:
                            _invalidate_admin_password_fields(
                                _st_err,
                                "staff_transfer_password",
                            )
            
            with col2:
                if st.button("Delete Staff Record", type="secondary", use_container_width=True):
                    # Password verification
                    password = st.text_input("Enter admin password:", type="password", key="staff_delete_password")
                    if password:
                        _sd_err = evaluate_admin_password_input(password)
                        if _sd_err is None:
                            try:
                                # Schedule deletion
                                deletion_date = pd.Timestamp.now() + pd.Timedelta(days=7)
                                
                                conn.execute('''
                                    UPDATE staff 
                                    SET status = 'Scheduled for Deletion', 
                                        deletion_scheduled = ?,
                                        updated_at = CURRENT_TIMESTAMP
                                    WHERE id = ?
                                ''', (deletion_date, staff['id']))
                                
                                conn.commit()
                                st.markdown('<div class="success-message">Staff record scheduled for deletion in 7 days.</div>', unsafe_allow_html=True)
                                _clear_password_field_keys("staff_delete_password")
                                st.rerun()
                            except Exception as e:
                                st.markdown(f'<div class="warning-message">Error: {str(e)}</div>', unsafe_allow_html=True)
                        else:
                            _invalidate_admin_password_fields(
                                _sd_err,
                                "staff_delete_password",
                            )
            
            st.markdown('</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="info-message">No staff records found in the system.</div>', unsafe_allow_html=True)

elif menu == "Add Expense":
    _flash = st.session_state.pop("_expense_flash_msg", None)
    if _flash:
        st.success(_flash)

    st.markdown('<h2 class="section-header">Add New Expense</h2>', unsafe_allow_html=True)

    _n_exp_pend = len(st.session_state.get("pending_expense_drafts") or [])
    _exp_tab_record = "Record expense"
    _exp_tab_pending = pending_review_tab_label(_n_exp_pend)
    tab_exp_record, tab_exp_pending = st.tabs([_exp_tab_record, _exp_tab_pending])

    with tab_exp_pending:
        _render_pending_expenses_tab(conn)

    with tab_exp_record:
            st.markdown('<div class="form-container">', unsafe_allow_html=True)
            with st.form("add_expense_form"):
                st.markdown(
                    '<h3 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Expense Details</h3>',
                    unsafe_allow_html=True,
                )

                col1, col2 = st.columns(2)

                with col1:
                    categories_df = pd.read_sql("SELECT * FROM expense_categories ORDER BY name", conn)
                    category_options = sorted(
                        categories_df["name"].dropna().astype(str).str.strip().unique().tolist(),
                        key=lambda s: s.lower(),
                    )
                    category_options = [c for c in category_options if c.strip().lower() != "staff"]
                    cat_select_options = [_EXPENSE_CAT_NONE] + category_options

                    selected_category_ui = st.selectbox(
                        "Category suggestion",
                        options=cat_select_options,
                        help="Pick a catalogue bucket if it fits. Skip this if you use a custom label instead.",
                    )

                    custom_label = st.text_input(
                        "Custom label",
                        placeholder="e.g. Field trip transport — Grade 4",
                        help="Use when there is no good category suggestion, or to add extra detail. "
                        "At least one of **category suggestion** or **custom label** is required.",
                    )

                    amount = st.number_input(
                        "Amount *",
                        min_value=0,
                        step=1,
                        format="%d",
                        help="Enter expense amount",
                    )

                with col2:
                    expense_date = st.date_input(
                        "Expense Date *",
                        help="Date when expense was incurred",
                    )

                    payment_method = st.selectbox(
                        "Payment Method",
                        options=["Cash", "Bank Transfer", "Credit Card", "Check", "Other"],
                        help="How was this expense paid?",
                    )

                    vendor = st.text_input(
                        "Vendor/Payee",
                        placeholder="e.g., 'ABC Supplies Store'",
                        help="Who was this expense paid to?",
                    )

                    receipt_number = st.text_input(
                        "Receipt Number",
                        placeholder="e.g., 'RCP-2024-001'",
                        help="Receipt or invoice number",
                    )

                description = st.text_area(
                    "Description *",
                    placeholder="Enter detailed description of the expense...",
                    help="Required. Explain what this expense was for.",
                )

                st.caption("Admin password is required only when you click **Save now**.")
                admin_pw = st.text_input(
                    "Admin password",
                    type="password",
                    key="add_expense_admin_pw_save_now",
                    label_visibility="collapsed",
                    placeholder="Admin password (Save now only)",
                )

                bcol1, bcol2, bcol3 = st.columns(3)
                with bcol1:
                    save_now = st.form_submit_button("Save now", type="primary", use_container_width=True)
                with bcol2:
                    save_later = st.form_submit_button("Save for later", use_container_width=True)
                with bcol3:
                    if st.form_submit_button("Clear form", use_container_width=True):
                        st.rerun()

                category_for_db = (
                    None
                    if selected_category_ui == _EXPENSE_CAT_NONE
                    else selected_category_ui
                )

                _payload_base = {
                    "category": category_for_db,
                    "custom_label": custom_label,
                    "amount": float(amount),
                    "expense_date": expense_date.strftime("%Y-%m-%d"),
                    "description": description,
                    "payment_method": payment_method,
                    "vendor": vendor,
                    "receipt_number": receipt_number,
                }

                if save_now:
                    ok, err = _validate_expense_entry(
                        category_db=category_for_db,
                        custom_label=custom_label,
                        amount=amount,
                        description=description,
                    )
                    if not ok:
                        _invalidate_admin_password_fields(err, "add_expense_admin_pw_save_now", level="warn")
                    else:
                        _exp_adm_err = evaluate_admin_password_input(admin_pw)
                        if _exp_adm_err is not None:
                            _invalidate_admin_password_fields(
                                _exp_adm_err,
                                "add_expense_admin_pw_save_now",
                            )
                        else:
                            try:
                                eid = _insert_expense_row(
                                    conn,
                                    category=_payload_base["category"],
                                    custom_label=_payload_base["custom_label"],
                                    amount=_payload_base["amount"],
                                    expense_date_str=_payload_base["expense_date"],
                                    description=_payload_base["description"],
                                    payment_method=_payload_base["payment_method"],
                                    vendor=_payload_base["vendor"],
                                    receipt_number=_payload_base["receipt_number"],
                                )
                                _audit_log(
                                    conn,
                                    "Expense",
                                    f"Expense row {eid}: KSH {float(amount):,.0f} — {(description or '')[:120]!r}.",
                                    save_mode="immediate",
                                    entity_type="expense",
                                    entity_id=int(eid),
                                    detail=json.dumps(_payload_base, default=str)[:4000],
                                )
                                st.session_state["_expense_flash_msg"] = (
                                    f"Expense of KSH {float(amount):,.0f} recorded in the database."
                                )
                                _clear_password_field_keys("add_expense_admin_pw_save_now")
                                st.rerun()
                            except Exception as e:
                                _invalidate_admin_password_fields(
                                    f"Error adding expense: {str(e)}",
                                    "add_expense_admin_pw_save_now",
                                )

                if save_later:
                    ok, err = _validate_expense_entry(
                        category_db=category_for_db,
                        custom_label=custom_label,
                        amount=amount,
                        description=description,
                    )
                    if not ok:
                        st.markdown(
                            f'<div class="warning-message">{err}</div>',
                            unsafe_allow_html=True,
                        )
                    else:
                        _ex_draft = {"id": str(uuid.uuid4()), **_payload_base, "queued_by_gate_user": _gate_audit_user()}
                        queue_pending_expense(conn, _ex_draft)
                        _audit_log(
                            conn,
                            "Expense",
                            f"Expense draft {_ex_draft['id']}: KSH {float(amount):,.0f} — saved for pending review.",
                            save_mode="pending_review",
                            detail=json.dumps({"draft_id": _ex_draft["id"]}, default=str),
                        )
                        st.session_state["_expense_flash_msg"] = (
                            "Expense saved for later. Open **Add Expense → Pending Reviews** to review and apply."
                        )
                        st.rerun()

            st.markdown('</div>', unsafe_allow_html=True)

elif menu == "Expense Categories & Reports":
    if not check_password():
        st.stop()
    st.markdown('<h2 class="section-header">Expense Categories & Reports</h2>', unsafe_allow_html=True)

    students_fees = pd.read_sql(
        "SELECT balance, total_paid, balance_status, balance_set, status FROM students",
        conn,
    )
    if not students_fees.empty:
        total_outstanding = sum_outstanding_balance_rows(students_fees)
        total_collected = float(students_fees["total_paid"].sum())
    else:
        total_outstanding = 0.0
        total_collected = 0.0

    st.markdown('<h3 class="section-header">School fee summary (all students)</h3>', unsafe_allow_html=True)
    fee_col1, fee_col2 = st.columns(2)
    with fee_col1:
        st.markdown(f"""
        <div class="metric-card" style="border-left-color: var(--danger);">
            <div style="font-size: 1.5rem; font-weight: 700; color: var(--danger);">KSH {total_outstanding:,.0f}</div>
            <div class="vine-help-text" style="margin-top: 0.25rem;">Total Outstanding</div>
        </div>
        """, unsafe_allow_html=True)
    with fee_col2:
        st.markdown(f"""
        <div class="metric-card" style="border-left-color: var(--success);">
            <div style="font-size: 1.5rem; font-weight: 700; color: var(--success);">KSH {total_collected:,.0f}</div>
            <div class="vine-help-text" style="margin-top: 0.25rem;">Total Collected</div>
        </div>
        """, unsafe_allow_html=True)

    st.caption(
        "**Total outstanding** counts only students whose balance is **set** and greater than zero — "
        "the same rule as **View Students** and the Dashboard (excludes **Not set** and **Cleared**)."
    )

    expenses_df = pd.read_sql("SELECT * FROM expenses ORDER BY expense_date DESC", conn)

    if not expenses_df.empty:
        categories_df = pd.read_sql(
            "SELECT name, description FROM expense_categories ORDER BY name", conn
        )
        total_expenses = float(expenses_df["amount"].sum())
        expense_count = len(expenses_df)
        avg_expense = float(expenses_df["amount"].mean())

        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("Total recorded", f"KSH {total_expenses:,.0f}")
        with m2:
            st.metric("Transactions", f"{expense_count:,}")
        with m3:
            st.metric("Average per entry", f"KSH {avg_expense:,.0f}")

        spend = (
            expenses_df.groupby("category", dropna=False)["amount"]
            .agg(["sum", "count"])
            .reset_index()
            .rename(columns={"sum": "total_ksh", "count": "txns"})
        )
        if not categories_df.empty:
            cat_m = categories_df.rename(columns={"name": "category"})
            summary = spend.merge(cat_m[["category", "description"]], on="category", how="left")
            summary["description"] = summary["description"].fillna("")
        else:
            summary = spend.assign(description="")
        summary = summary.sort_values("total_ksh", ascending=False)

        st.markdown('<h3 class="section-header">Spend by category</h3>', unsafe_allow_html=True)
        st.caption("Rows appear only for categories used on recorded expenses (Add Expense).")
        show = summary.rename(
            columns={
                "category": "Category",
                "description": "Description",
                "total_ksh": "Total (KSH)",
                "txns": "Count",
            }
        )
        st.dataframe(
            show,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Total (KSH)": st.column_config.NumberColumn(format="%.0f"),
            },
        )

        st.markdown('<h3 class="section-header">Recent activity</h3>', unsafe_allow_html=True)
        recent = expenses_df.head(20).copy()
        recent["expense_date"] = pd.to_datetime(recent["expense_date"]).dt.strftime("%Y-%m-%d")
        label_col = recent["custom_label"].fillna("").astype(str).str.strip()
        if "description" in recent.columns:
            desc_col = recent["description"].fillna("").astype(str).str.strip()
        else:
            desc_col = pd.Series([""] * len(recent), index=recent.index)
        recent["Details"] = [
            (lab if lab else (des if des else "—")) for lab, des in zip(label_col, desc_col)
        ]
        recent_display = recent[
            ["expense_date", "category", "Details", "amount", "payment_method"]
        ].rename(
            columns={
                "expense_date": "Date",
                "category": "Category",
                "amount": "Amount (KSH)",
                "payment_method": "Payment",
            }
        )
        st.dataframe(
            recent_display,
            use_container_width=True,
            hide_index=True,
            column_config={"Amount (KSH)": st.column_config.NumberColumn(format="%.2f")},
        )
    else:
        st.markdown(
            '<div class="info-message">No expenses recorded yet. Use <strong>Add Expense</strong> to create entries — '
            "tables here stay empty until then.</div>",
            unsafe_allow_html=True,
        )

        st.markdown('<h3 class="section-header">Spend by category</h3>', unsafe_allow_html=True)
        st.dataframe(
            pd.DataFrame(columns=["Category", "Description", "Total (KSH)", "Count"]),
            use_container_width=True,
            hide_index=True,
        )

elif menu == "Fee Structure":
    st.markdown('<h2 class="section-header">Fee Structure Management</h2>', unsafe_allow_html=True)
    
    # Get all fee items grouped by category
    fee_categories = conn.execute("SELECT DISTINCT fee_category FROM fee_structure ORDER BY CASE fee_category WHEN 'tuition' THEN 1 WHEN 'mandatory' THEN 2 WHEN 'admission' THEN 3 WHEN 'transport' THEN 4 WHEN 'co_curricular' THEN 5 WHEN 'meal' THEN 6 ELSE 7 END").fetchall()
    
    st.markdown('<div class="form-container">', unsafe_allow_html=True)
    st.markdown('<h3 style="color: var(--text); margin-bottom: 1rem; font-weight: 600;">Current Fee Structure</h3>', unsafe_allow_html=True)
    st.markdown(
        '<p class="vine-help-text" style="margin-bottom: 1rem;">'
        "Amounts are editable below. When you save, you must enter the admin password; the system updates the catalogue, "
        "then recomputes <strong>every student</strong> outstanding balance from their grade, transport, clubs, meal, admission flag, and recorded payments."
        "</p>",
        unsafe_allow_html=True,
    )
    _fee_search = (st.text_input(
        "Search fees",
        placeholder="Fee name, grade, route, or category…",
        key="fee_structure_search",
        help="Filters the list below. Leave empty to show all fees.",
    ) or "").strip().lower()
    _fee_search_words = [w for w in _fee_search.split() if w]

    for cat_row in fee_categories:
        cat = cat_row[0]
        items = conn.execute("SELECT * FROM fee_structure WHERE fee_category=? ORDER BY fee_amount, fee_name", (cat,)).fetchall()
        
        if items:
            cat_labels = {
                "tuition": "Tuition Fees (Per Term)",
                "mandatory": "Mandatory Fees (Per Term)",
                "admission": "One-Time Admission Fees",
                "transport": "Transport Fees (Per Term, Optional)",
                "co_curricular": "Co-Curricular Activities (Per Term, Optional)",
                "meal": "Meal Program (Per Term, Optional)",
            }
            
            st.markdown(f'<h4 style="color: var(--primary); margin-top: 1.5rem; margin-bottom: 0.75rem;">{cat_labels.get(cat, cat)}</h4>', unsafe_allow_html=True)
            
            edcols = st.columns(2)
            _shown = 0
            for j, item in enumerate(items):
                item_id, fee_cat, fee_name, fee_amount, grade_applicable, transport_route, is_optional, is_one_time, created_at = item
                
                name_display = fee_name
                if grade_applicable:
                    name_display = f"{fee_name} ({grade_applicable})"
                if transport_route:
                    name_display = f"{transport_route}"
                
                opt_h = "Optional" if is_optional else "Mandatory"
                freq_h = "One-time" if is_one_time else "Per term"
                _hay = " ".join(
                    str(x or "")
                    for x in (fee_name, grade_applicable, transport_route, cat, name_display, opt_h)
                ).lower()
                if _fee_search_words and not all(w in _hay for w in _fee_search_words):
                    continue
                _shown += 1
                with edcols[j % 2]:
                    st.number_input(
                        f"{name_display} (KSH)",
                        min_value=0.0,
                        value=float(fee_amount),
                        step=50.0,
                        key=f"fee_edit_{item_id}",
                        help=f"Row id {item_id} · {opt_h} · {freq_h}",
                    )
            if _fee_search_words and _shown == 0:
                st.caption(f"No fees in **{cat_labels.get(cat, cat)}** match your search.")
    
    st.markdown('<hr style="margin: 2rem 0; border: none; border-top: 1px solid var(--border);">', unsafe_allow_html=True)
    st.markdown('<h4 style="color: var(--text); margin-bottom: 0.5rem;">Save fee amount changes</h4>', unsafe_allow_html=True)
    st.markdown(
        '<p class="vine-help-text" style="margin-bottom: 0.75rem;">'
        "Enter the admin password, then save. This writes all amount fields above and resyncs student balances."
        "</p>",
        unsafe_allow_html=True,
    )
    fee_pw = st.text_input("Admin password (required to save)", type="password", key="fee_save_password_field")
    if st.button("Save all fee amounts & resync student balances", type="primary", key="fee_save_all_btn"):
        _fee_err = evaluate_admin_password_input(fee_pw)
        if _fee_err is not None:
            _invalidate_admin_password_fields(
                _fee_err,
                "fee_save_password_field",
            )
        else:
            all_rows = conn.execute("SELECT id FROM fee_structure").fetchall()
            updated = 0
            for (fid,) in all_rows:
                k = f"fee_edit_{fid}"
                if k in st.session_state:
                    newv = float(st.session_state[k])
                    conn.execute("UPDATE fee_structure SET fee_amount=? WHERE id=?", (newv, fid))
                    updated += 1
            conn.commit()
            n = resync_all_student_balances(conn)
            invalidate_student_cache()
            st.markdown(
                f'<div class="success-message">Updated <strong>{updated}</strong> fee rows. '
                f'Recalculated balances for <strong>{n}</strong> students.</div>',
                unsafe_allow_html=True,
            )
            _clear_password_field_keys("fee_save_password_field")
            st.rerun()
    
    st.markdown('</div>', unsafe_allow_html=True)

elif menu == "Database backup":
    st.markdown('<h2 class="section-header">Database backup</h2>', unsafe_allow_html=True)
    if not gate_user_has_admin_privileges():
        st.markdown(
            '<div class="warning-message">Your account cannot download a full database backup.</div>',
            unsafe_allow_html=True,
        )
        if st.button("Back to Dashboard", type="primary", key="db_backup_denied_back"):
            st.session_state.current_page = "Dashboard"
            st.rerun()
        st.stop()

    st.markdown(
        '<p class="vine-help-text">Download a <strong>WAL-safe</strong> snapshot of the live SQLite file '
        "(same data as <code>school.db</code> / <code>VINELEDGER_SQLITE_PATH</code>). "
        "On <strong>Streamlit Community Cloud</strong>, store copies off-site (for example a <strong>private</strong> "
        "Google Drive folder); the hosted container is not a long-term archive.</p>",
        unsafe_allow_html=True,
    )
    try:
        _src_disp = resolve_sqlite_database_path(None)
        st.caption(f"Source file: `{_src_disp}`")
    except Exception:
        pass

    st.markdown('<div class="form-container">', unsafe_allow_html=True)
    _db_bpw = st.text_input(
        "Admin password (required to prepare download)",
        type="password",
        key="db_backup_admin_pw_field",
    )
    if st.button("Prepare download", type="primary", key="db_backup_prepare_btn"):
        if (e := evaluate_admin_password_input(_db_bpw)) is not None:
            _invalidate_admin_password_fields(e, "db_backup_admin_pw_field")
        else:
            try:
                _blob = snapshot_sqlite_database_bytes()
            except FileNotFoundError as ex:
                st.session_state.pop("_vine_db_backup_blob", None)
                st.session_state.pop("_vine_db_backup_filename", None)
                st.error(str(ex))
            else:
                _stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                st.session_state["_vine_db_backup_blob"] = _blob
                st.session_state["_vine_db_backup_filename"] = f"school_{_stamp}.db"
                _audit_log(
                    conn,
                    "Database backup",
                    "Prepared SQLite snapshot for download (admin).",
                    save_mode="immediate",
                    entity_type="database",
                    entity_id=None,
                )
                _clear_password_field_keys("db_backup_admin_pw_field")
                st.rerun()

    _bdata = st.session_state.get("_vine_db_backup_blob")
    _bfname = st.session_state.get("_vine_db_backup_filename") or "school.db"
    if _bdata:
        st.download_button(
            label="Download snapshot (.db)",
            data=_bdata,
            file_name=_bfname,
            mime="application/x-sqlite3",
            type="primary",
            key="db_backup_download_btn",
        )
        if st.button("Discard prepared backup", key="db_backup_discard_btn"):
            st.session_state.pop("_vine_db_backup_blob", None)
            st.session_state.pop("_vine_db_backup_filename", None)
            st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

elif menu == "School Calendar":
    st.markdown('<h2 class="section-header">School Calendar</h2>', unsafe_allow_html=True)
    st.markdown(
        f'<p class="vine-status-line"><strong>Today:</strong> {html_module.escape(date.today().strftime("%A, %d %B %Y"))}</p>',
        unsafe_allow_html=True,
    )

    _cur = get_current_term(conn)
    if _cur:
        _term_bits = (
            f"<strong>Current term:</strong> {html_module.escape(str(_cur['label']))} · status "
            f"<strong>{html_module.escape(str(_cur.get('status', 'active')))}</strong>"
        )
        if int(_cur.get("automation_cancelled") or 0):
            _term_bits += " · automation <strong>cancelled</strong>"
        st.markdown(f'<p class="vine-status-line">{_term_bits}</p>', unsafe_allow_html=True)
    else:
        st.markdown(
            '<p class="vine-status-line">No current term is marked active. Set dates below and save, or run term actions when ready.</p>',
            unsafe_allow_html=True,
        )

    _years = list_academic_years(conn)
    if not _years:
        st.warning("No academic years in the database.")
    else:
        _year_labels = [r[1] for r in _years]
        _year_ids = {r[1]: int(r[0]) for r in _years}
        _default_label = _year_labels[0]
        if _cur:
            _yrow = conn.execute(
                "SELECT label FROM academic_years WHERE id=?", (int(_cur["year_id"]),)
            ).fetchone()
            if _yrow and _yrow[0] in _year_labels:
                _default_label = _yrow[0]

        st.markdown('<div class="form-container">', unsafe_allow_html=True)
        _sel_year_label = st.selectbox(
            "Academic year",
            _year_labels,
            index=_year_labels.index(_default_label) if _default_label in _year_labels else 0,
            key="school_calendar_year_pick",
        )
        _year_id = _year_ids[_sel_year_label]
        _terms_raw = get_terms_for_year(conn, _year_id)

        _settings = get_calendar_settings(conn)
        _warn_days = st.number_input(
            "Dashboard warning days before term close",
            min_value=1,
            max_value=90,
            value=int(_settings.get("warn_days_before_close") or 14),
            key="school_calendar_warn_days",
            help="Shown on the Dashboard when the current term is within this many days of its closing date.",
        )

        st.markdown(
            '<p class="vine-help-text" style="margin: 1rem 0;">'
            "Set opening and closing dates for each term. VineLedger closes the current term on the closing date, "
            "graduates Grade 9 leavers and promotes other grades after Term 3, and bills all active students when the next term opens. "
            "Viewing dates does not require a password; saving or running actions does."
            "</p>",
            unsafe_allow_html=True,
        )

        _term_date_rows = []
        for _t in _terms_raw:
            (
                _tid,
                _yid,
                _tn,
                _tlabel,
                _op_s,
                _cl_s,
                _iscur,
                _status,
                _cancelled,
                _closed_at,
                _opened_at,
            ) = _t
            _op_d = None
            _cl_d = None
            if _op_s:
                try:
                    _op_d = date.fromisoformat(str(_op_s)[:10])
                except ValueError:
                    pass
            if _cl_s:
                try:
                    _cl_d = date.fromisoformat(str(_cl_s)[:10])
                except ValueError:
                    pass

            _badges = []
            if _iscur:
                _badges.append("current")
            if _status:
                _badges.append(str(_status))
            if _cancelled:
                _badges.append("automation cancelled")
            if _closed_at:
                _badges.append("closed processed")
            if _opened_at:
                _badges.append("opened processed")
            _badge_txt = " · ".join(_badges) if _badges else "upcoming"

            st.markdown(
                f'<h4 style="color: var(--primary); margin-top: 1.25rem;">{_tlabel}</h4>'
                f'<p class="vine-help-text" style="margin-bottom: 0.5rem;">{_badge_txt}</p>',
                unsafe_allow_html=True,
            )
            _c1, _c2 = st.columns(2)
            with _c1:
                _new_op = st.date_input(
                    "Opening date",
                    value=_op_d,
                    key=f"cal_open_{_year_id}_{_tn}",
                )
            with _c2:
                _new_cl = st.date_input(
                    "Closing date",
                    value=_cl_d,
                    key=f"cal_close_{_year_id}_{_tn}",
                )
            _term_date_rows.append({
                "term_number": int(_tn),
                "opening_date": _new_op,
                "closing_date": _new_cl,
            })

        st.markdown('<hr style="margin: 2rem 0; border: none; border-top: 1px solid var(--border);">', unsafe_allow_html=True)
        st.markdown('<h4 style="color: var(--text);">Save calendar dates</h4>', unsafe_allow_html=True)
        _cal_pw = st.text_input(
            "Admin password (required to save or run actions)",
            type="password",
            key="school_calendar_password",
        )
        if st.button("Save term dates & warning setting", type="primary", key="school_calendar_save_btn"):
            _cal_err = evaluate_admin_password_input(_cal_pw)
            if _cal_err is not None:
                _invalidate_admin_password_fields(
                    _cal_err,
                    "school_calendar_password",
                )
            else:
                _errs = validate_term_dates(_term_date_rows)
                if _errs:
                    _invalidate_admin_password_fields(
                        " ".join(_errs),
                        "school_calendar_password",
                        level="warn",
                    )
                else:
                    _dates_map = {
                        int(r["term_number"]): (r["opening_date"], r["closing_date"])
                        for r in _term_date_rows
                    }
                    save_school_calendar_year(conn, _year_id, _dates_map, warn_days=int(_warn_days))
                    st.markdown('<div class="success-message">School calendar saved.</div>', unsafe_allow_html=True)
                    _clear_password_field_keys("school_calendar_password")
                    st.rerun()

        if _cur and int(_cur.get("year_id")) == _year_id:
            st.markdown('<h4 style="color: var(--text); margin-top: 1.5rem;">Current term automation</h4>', unsafe_allow_html=True)
            _cancelled_now = int(_cur.get("automation_cancelled") or 0)
            if _cancelled_now:
                st.info("Automatic close/open is **cancelled** for the current term until you re-enable it.")
            if st.button(
                "Cancel automatic close/open for current term" if not _cancelled_now else "Re-enable automatic close/open",
                key="school_calendar_toggle_cancel",
            ):
                _cal_err2 = evaluate_admin_password_input(_cal_pw)
                if _cal_err2 is not None:
                    _invalidate_admin_password_fields(
                        _cal_err2,
                        "school_calendar_password",
                    )
                else:
                    cancel_term_automation(conn, int(_cur["id"]), cancelled=not _cancelled_now)
                    st.markdown('<div class="success-message">Automation setting updated.</div>', unsafe_allow_html=True)
                    _clear_password_field_keys("school_calendar_password")
                    st.rerun()

        st.markdown('<h4 style="color: var(--text); margin-top: 1.5rem;">Run term actions now</h4>', unsafe_allow_html=True)
        st.caption(
            "Closes the current term if its closing date has passed, opens terms whose opening dates have passed, "
            "and bills students. Same logic as the Dashboard automation."
        )
        if st.button("Run term actions now", key="school_calendar_run_now"):
            _cal_err3 = evaluate_admin_password_input(_cal_pw)
            if _cal_err3 is not None:
                _invalidate_admin_password_fields(
                    _cal_err3,
                    "school_calendar_password",
                )
            else:
                _run_msgs = run_term_actions_now(conn)
                if _run_msgs:
                    for _rm in _run_msgs:
                        if _rm:
                            st.markdown(f'<div class="success-message">{_rm}</div>', unsafe_allow_html=True)
                else:
                    st.markdown('<div class="info-message">No term actions were due to run.</div>', unsafe_allow_html=True)
                invalidate_student_cache()
                _clear_password_field_keys("school_calendar_password")
                st.rerun()

        st.markdown('</div>', unsafe_allow_html=True)

elif menu == "School Activity":
    _render_school_activity_page(conn)

touch_gate_activity()
