import hashlib
import hmac
import json
import unittest
from datetime import datetime, timezone
from urllib.parse import urlencode

from services import course as svc

TOKEN = "123:abc"


def _sign(user_id=7, auth_date=1_000_000, token=TOKEN):
    data = {"auth_date": str(auth_date), "user": json.dumps({"id": user_id})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


class InitDataTest(unittest.TestCase):
    def test_valid(self):
        user = svc.verify_init_data(_sign(), TOKEN, max_age=60, now=1_000_030)
        self.assertEqual(user["id"], 7)

    def test_wrong_token_expired_tampered(self):
        self.assertIsNone(svc.verify_init_data(_sign(token="1:x"), TOKEN, 60, 1_000_030))
        self.assertIsNone(svc.verify_init_data(_sign(), TOKEN, 60, 1_000_100))
        self.assertIsNone(svc.verify_init_data(_sign().replace("7", "8"), TOKEN, 60, 1_000_030))
        self.assertIsNone(svc.verify_init_data("", TOKEN, 60, 1_000_030))


class PricingTest(unittest.TestCase):
    opt = {"price": 4490, "price_full": 5990, "sections": "shooting,editing"}

    def test_switch_at_msk_midnight(self):
        self.assertEqual(svc.price_for(self.opt, datetime(2026, 10, 31, 20, 59, tzinfo=timezone.utc)), 4490)
        self.assertEqual(svc.price_for(self.opt, datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)), 5990)

    def test_available_options(self):
        opts = [{"slug": "shooting", "sections": "shooting"},
                {"slug": "editing", "sections": "editing"},
                {"slug": "bundle", "sections": "shooting,editing"}]
        slugs = lambda owned: [o["slug"] for o in svc.available_options(opts, owned)]
        self.assertEqual(slugs(set()), ["shooting", "editing", "bundle"])
        self.assertEqual(slugs({"shooting"}), ["editing"])
        self.assertEqual(slugs({"shooting", "editing"}), [])


if __name__ == "__main__":
    unittest.main()
