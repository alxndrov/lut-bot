"""
Read-only HTTPS-эндпоинт для отладки заказов и роутинга печати.

Отдельный сервер от вебхука Prodamus: слушает свой порт (DEBUG_API_PORT),
поднимается самоподписанным TLS и требует токен в заголовке X-Debug-Token.
Без DEBUG_API_TOKEN в .env сервер не запускается вовсе — см. bot.py.

Никаких изменяющих операций тут нет и не должно быть: только SELECT-запросы
через database.py.
"""
import hmac
import json
import logging
import ssl
import subprocess
from pathlib import Path

from aiohttp import web

import config
import database as db

logger = logging.getLogger(__name__)

CERT_DIR = Path(__file__).resolve().parent.parent / "certs"
CERT_FILE = CERT_DIR / "debug.crt"
KEY_FILE = CERT_DIR / "debug.key"


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def ensure_self_signed_cert() -> None:
    """Генерирует самоподписанный сертификат при первом запуске, если его ещё нет.

    Он нужен только для того, чтобы разговор шёл по HTTPS (это требование
    прокси, через который идут запросы), а не для проверки подлинности
    сервера — реальная защита эндпоинта — токен, а не сертификат.
    """
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("debug_api: генерирую самоподписанный TLS-сертификат...")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ed25519",
            "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
            "-days", "3650", "-nodes",
            "-subj", "/CN=lut-bot-debug",
        ],
        check=True, capture_output=True,
    )
    KEY_FILE.chmod(0o600)
    logger.info(f"debug_api: сертификат создан ({CERT_FILE}).")


def build_ssl_context() -> ssl.SSLContext:
    ensure_self_signed_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    return ctx


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    if request.path == "/debug/health":
        return await handler(request)
    token = config.DEBUG_API_TOKEN
    given = request.headers.get("X-Debug-Token", "")
    if not token or not hmac.compare_digest(given, token):
        logger.warning(f"debug_api: неавторизованный запрос {request.path} с {request.remote}")
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def list_orders(request: web.Request) -> web.Response:
    try:
        limit = max(1, min(int(request.query.get("limit", 20)), 100))
    except ValueError:
        limit = 20
    orders = await db.get_orders()
    rows = [{
        "id": o["id"],
        "order_code": o.get("order_code"),
        "product_id": o["product_id"],
        "product_name": o.get("product_name"),
        "buyer": f"@{o['username']}" if o.get("username")
                else (o.get("first_name") or f"id:{o.get('user_id')}"),
        "assignee_name": o.get("assignee_name"),
        "created_at": o.get("created_at"),
        "shipped_at": o.get("shipped_at"),
    } for o in orders[:limit]]
    return web.json_response(rows, dumps=_dumps)


async def get_order(request: web.Request) -> web.Response:
    code = request.match_info["code"]
    orders = await db.get_orders()
    order = next(
        (o for o in orders if o.get("order_code") == code or str(o["id"]) == code),
        None,
    )
    if not order:
        return web.json_response({"error": "not found"}, status=404)

    rounds = json.loads(order.get("rounds_json") or "[]")
    round_products = db.unpack_round_products(
        order.get("round_products_json"), rounds, order["product_id"])

    products = {}
    for pid in {order["product_id"], *round_products}:
        p = await db.get_product(pid)
        if not p:
            continue
        questions = await db.get_product_questions(pid)
        products[pid] = {
            "name": p["name"],
            "category": p.get("category"),
            "order_routing_text": p.get("order_routing_text"),
            "questions": [
                {"text": q["text"], "is_router": bool(q.get("is_router"))}
                for q in questions
            ],
        }

    out = dict(order)
    out.pop("rounds_json", None)
    out.pop("round_products_json", None)
    out["rounds"] = rounds
    out["round_products"] = round_products
    out["products"] = products
    out["routing"] = db.order_routing(order)
    return web.json_response(out, dumps=_dumps)


def create_debug_app() -> web.Application:
    app = web.Application(middlewares=[_auth_middleware])
    app.router.add_get("/debug/health", health)
    app.router.add_get("/debug/orders", list_orders)
    app.router.add_get("/debug/order/{code}", get_order)
    return app
