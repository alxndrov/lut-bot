#!/usr/bin/env python3
"""
Тест оплаты — симулирует вебхук от Prodamus для заданного товара.
Использование:
    python test_payment.py <product_id>
    python test_payment.py <product_id> <user_id>   # если нужен другой юзер

По умолчанию user_id = 140657458 (admin).
"""
import sys
import time
import hmac
import hashlib
import urllib.request
import urllib.parse

SECRET = "f94abc14032e5d2af289e5c366e076cb93c6bfd8e59a7e7141cd1a53bb178fc4"
WEBHOOK_URL = "http://127.0.0.1:8080/prodamus/webhook"
DEFAULT_USER_ID = 140657458

product_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
user_id = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_USER_ID

if not product_id:
    print("Использование: python test_payment.py <product_id> [user_id]")
    sys.exit(1)

data = {
    "order_id":       f"test_{int(time.time())}",
    "payment_status": "success",
    "payment_sum":    "1",
    "sys":            f"d_{user_id}_{product_id}",
}

# Подпись: sort by key → join values with | → HMAC-SHA256
sorted_values = [str(data[k]) for k in sorted(data.keys())]
sign = hmac.new(SECRET.encode(), "|".join(sorted_values).encode(), hashlib.sha256).hexdigest()
data["sign"] = sign

print(f"→ Отправляю тестовый вебхук: product_id={product_id}, user_id={user_id}")
print(f"  order_id: {data['order_id']}")
print(f"  sign:     {sign}")

body = urllib.parse.urlencode(data).encode()
req = urllib.request.Request(WEBHOOK_URL, data=body, method="POST")
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        response = r.read().decode()
        print(f"✅ Ответ сервера: {response}")
except Exception as e:
    print(f"❌ Ошибка: {e}")
