"""Kenyan MSISDN normalization (07… / 01… and +254)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import normalize_kenya_msisdn


class KenyaMsisdnTests(unittest.TestCase):
    def test_plus_254_seven_and_one(self):
        self.assertEqual(normalize_kenya_msisdn("+254790886553"), "254790886553")
        self.assertEqual(normalize_kenya_msisdn("+254135448776"), "254135448776")

    def test_local_07_and_01(self):
        self.assertEqual(normalize_kenya_msisdn("0790886553"), "254790886553")
        self.assertEqual(normalize_kenya_msisdn("0135448776"), "254135448776")

    def test_nine_digits_without_country(self):
        self.assertEqual(normalize_kenya_msisdn("790886553"), "254790886553")
        self.assertEqual(normalize_kenya_msisdn("135448776"), "254135448776")

    def test_spaces_and_plus_stripped(self):
        self.assertEqual(
            normalize_kenya_msisdn("+254 790 886 553"),
            "254790886553",
        )


if __name__ == "__main__":
    unittest.main()
