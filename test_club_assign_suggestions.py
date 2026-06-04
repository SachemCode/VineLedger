"""Club assign autocomplete helper (_club_assign_suggestion_rows)."""

import pandas as pd
import pytest

import app


@pytest.fixture
def sample_students_df():
    return pd.DataFrame(
        [
            {"id": 1, "name": "Rachel Adams", "student_code": "S001", "grade": "Grade 1"},
            {"id": 2, "name": "Brian Smith", "student_code": "S002", "grade": "Grade 2"},
            {"id": 3, "name": "Raymond Lee", "student_code": "S003", "grade": "Grade 1"},
        ]
    )


def test_suggestions_empty_query_returns_empty(sample_students_df):
    out = app._club_assign_suggestion_rows(
        sample_students_df,
        "   ",
        grade_filter="All grades",
        intent_ids=[],
        cap=12,
    )
    assert out.empty


def test_suggestions_prefix_order_and_cap(sample_students_df):
    out = app._club_assign_suggestion_rows(
        sample_students_df,
        "ra",
        grade_filter="All grades",
        intent_ids=[],
        cap=2,
    )
    assert len(out) == 2
    # "Raymond" and "Rachel" prefix "ra"; "Brian" contains "ra" but not prefix — should rank after
    names = out["name"].tolist()
    assert names[0] in ("Raymond Lee", "Rachel Adams")
    assert names[1] in ("Raymond Lee", "Rachel Adams")


def test_suggestions_hides_intent_ids(sample_students_df):
    out = app._club_assign_suggestion_rows(
        sample_students_df,
        "ra",
        grade_filter="All grades",
        intent_ids=[1],
        cap=12,
    )
    assert 1 not in out["id"].astype(int).tolist()


def test_suggestions_grade_filter(sample_students_df):
    """Grade filter excludes rows not in that grade (query must not match grade substring)."""
    out = app._club_assign_suggestion_rows(
        sample_students_df,
        "S003",
        grade_filter="Grade 2",
        intent_ids=[],
        cap=12,
    )
    assert out.empty
