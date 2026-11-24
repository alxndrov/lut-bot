"""Подпись ссылки на оплату: Telegram и iOS отрезают «_»/«-» в самом конце ссылки."""
import unittest

from services import paylinks


def _token_ending(chars: str) -> str:
    for i in range(2000):
        t = paylinks.make_token(f"c_{i}_3", 6990, "Пакет", "offer:x")
        if t[-1] in chars:
            return t
    raise AssertionError("не нашёл подпись с таким концом")


class PaylinkSignatureTest(unittest.TestCase):
    def test_trailing_underscore_or_dash_may_be_lost(self):
        for ch in "_-":
            t = _token_ending(ch)
            self.assertIsNotNone(paylinks.parse_token(t))
            self.assertEqual(paylinks.parse_token(t[:-1])["amount"], 6990)

    def test_other_truncation_or_tamper_is_rejected(self):
        t = _token_ending("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
        self.assertIsNone(paylinks.parse_token(t[:-1]))
        t = _token_ending("_")
        self.assertIsNone(paylinks.parse_token(t[:-2]))
        self.assertIsNone(paylinks.parse_token(t[:-2] + "A"))
        body, sig = t.split(".")
        other = paylinks.make_token("c_1_3", 1, "Пакет", "offer:x").split(".")[0]
        self.assertIsNone(paylinks.parse_token(f"{other}.{sig[:-1]}"))


if __name__ == "__main__":
    unittest.main()
