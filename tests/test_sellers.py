import unittest

from handlers.sellers_info import info_text
from services.sellers import seller_line


class SellersTest(unittest.TestCase):
    def test_seller_by_category(self):
        self.assertIn("Александров Д. К., ИНН 745114732883", seller_line("physical"))
        for category in ("digital", "infobiz", "course"):
            self.assertIn("Барышов М. П., ИНН 745312244278", seller_line(category))

    def test_info_has_what_payanyway_checks(self):
        text = info_text()
        for must in ("Барышов Михаил Павлович", "745312244278", "Александров Даниил Константинович",
                     "745114732883", "mikhail.baryshov@gmail.com", "Возврат", "Оплата", "СДЭК"):
            self.assertIn(must, text)


if __name__ == "__main__":
    unittest.main()
