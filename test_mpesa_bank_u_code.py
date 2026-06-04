"""M-Pesa U-code parsing and reference normalization for bank statements."""

import unittest

from utils import normalize_payment_reference_key, parse_mpesa_u_code_from_transaction_details


class TestMpesaBankUCode(unittest.TestCase):
    def test_u_code_after_254_phone(self):
        line = "254721247633 UEKBX53ITP 0766218116 HASSAN ABDI HUS"
        self.assertEqual(parse_mpesa_u_code_from_transaction_details(line), "UEKBX53ITP")

    def test_u_code_multiline_first_line_only(self):
        cell = "254795640455 UEK6251GV9 0766218116 ESTHER MUTHONI\nmBRQwibj39TD\n00mBRQwibj39TD"
        self.assertEqual(parse_mpesa_u_code_from_transaction_details(cell), "UEK6251GV9")

    def test_normalize_reference(self):
        self.assertEqual(normalize_payment_reference_key("  uek344zgqs  "), "UEK344ZGQS")


if __name__ == "__main__":
    unittest.main()
