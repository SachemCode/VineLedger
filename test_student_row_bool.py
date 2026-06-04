"""student_row_bool accepts dict records from get_student_record."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import student_is_sponsored, student_row_bool


def test_student_row_bool_dict():
    rec = {"name": "Test", "is_sponsored": 1, "has_meal": 0}
    assert student_row_bool(rec, "is_sponsored") is True
    assert student_row_bool(rec, "has_meal") is False
    assert student_row_bool(rec, "missing", True) is True
    assert student_is_sponsored(rec) is True
