"""enrich_students_for_view: deletion / transfer columns for View Students."""

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app


@pytest.fixture
def memory_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE fee_structure (
            id INTEGER PRIMARY KEY,
            fee_name TEXT NOT NULL,
            fee_category TEXT NOT NULL,
            fee_amount REAL NOT NULL DEFAULT 0,
            grade_applicable TEXT,
            transport_route TEXT,
            is_optional INTEGER DEFAULT 1,
            is_one_time INTEGER DEFAULT 0,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE payments (
            student_id INTEGER,
            amount REAL,
            payment_date TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE student_fee_items (
            student_id INTEGER,
            fee_item_id INTEGER
        )
        """
    )
    conn.commit()
    yield conn
    conn.close()


def test_enrich_scheduled_deletion_shows_days_reason(memory_conn):
    df = pd.DataFrame(
        [
            {
                "id": 1,
                "status": "Scheduled for Deletion",
                "deletion_scheduled": "2030-06-15 12:00:00",
                "deletion_reason": "Duplicate record",
                "has_transport": 0,
                "co_curricular_activities": None,
            }
        ]
    )
    out = app.enrich_students_for_view(df, memory_conn)
    row = out.iloc[0]
    assert row["to_be_deleted"] == "Yes"
    assert row["days_till_deleted"].isdigit()
    assert int(row["days_till_deleted"]) >= 0
    assert row["deletion_reason"] == "Duplicate record"
    assert row["transferred"] == "No"


def test_enrich_transfer_reason_and_flag(memory_conn):
    df = pd.DataFrame(
        [
            {
                "id": 2,
                "status": "Transferred",
                "deletion_scheduled": "2030-01-01 00:00:00",
                "transfer_reason": "Moved schools",
                "deletion_reason": None,
                "has_transport": 0,
                "co_curricular_activities": None,
            }
        ]
    )
    out = app.enrich_students_for_view(df, memory_conn)
    row = out.iloc[0]
    assert row["to_be_deleted"] == "Yes"
    assert row["transferred"] == "Yes"
    assert row["transfer_reason"] == "Moved schools"


def test_enrich_status_casefold_scheduled(memory_conn):
    df = pd.DataFrame(
        [
            {
                "id": 3,
                "status": "SCHEDULED FOR DELETION",
                "deletion_scheduled": "2030-12-01 08:00:00",
                "deletion_reason": "Other",
                "has_transport": 0,
                "co_curricular_activities": None,
            }
        ]
    )
    out = app.enrich_students_for_view(df, memory_conn)
    assert out.iloc[0]["to_be_deleted"] == "Yes"


def test_enrich_exit_pending_missing_schedule_still_yes(memory_conn):
    df = pd.DataFrame(
        [
            {
                "id": 4,
                "status": "Transferred",
                "deletion_scheduled": None,
                "transfer_reason": None,
                "has_transport": 0,
                "co_curricular_activities": None,
            }
        ]
    )
    out = app.enrich_students_for_view(df, memory_conn)
    row = out.iloc[0]
    assert row["to_be_deleted"] == "Yes"
    assert row["days_till_deleted"] == "—"
