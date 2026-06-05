"""Payment internal / reference ids: 9-char A-Z0-9 format."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import init_db, new_internal_payment_id, new_payment_alnum_ref


class PaymentIdFormatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.conn = init_db()

    def tearDown(self):
        self._tmp.cleanup()

    def test_alnum_ref_length_and_charset(self):
        s = new_payment_alnum_ref(9)
        self.assertEqual(len(s), 9)
        self.assertRegex(s, r"^[A-Z0-9]{9}$")

    def test_internal_id_format_and_unique(self):
        a = new_internal_payment_id()
        b = new_internal_payment_id()
        self.assertEqual(len(a), 9)
        self.assertRegex(a, r"^[A-Z0-9]{9}$")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
