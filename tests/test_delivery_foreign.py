import unittest

from handlers.delivery import _normalize_phone


class ForeignPhoneTests(unittest.TestCase):
    def test_russian_numbers_as_before(self):
        for raw in ("+7 900 036-39-45", "89000363945", "9000363945", "79000363945"):
            self.assertEqual(_normalize_phone(raw), "+79000363945")

    def test_foreign_number_with_country_code(self):
        self.assertEqual(_normalize_phone("+996(555)540-523"), "+996555540523")
        self.assertEqual(_normalize_phone("+375 29 123-45-67"), "+375291234567")

    def test_foreign_digits_without_plus_are_ambiguous(self):
        self.assertIsNone(_normalize_phone("996555540523"))
        self.assertIsNone(_normalize_phone("123"))
