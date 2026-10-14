"""
Куда ведёт ссылка на оплату: Prodamus или ЮKassa.

Все кнопки «Оплатить» ведут на наш адрес /pay/<токен> (services/paylinks.py),
а он отправляет клиента дальше — смотря по настройке в malimadmins (/payments):

- «ЮKassa» — сразу создаём платёж в ЮKassa и ведём на её страницу;
- «Prodamus» — ведём в Prodamus, а если через PAY_BACKUP_DELAY_MIN минут
  оплаты нет, бот спрашивает, получилось ли, и даёт запасную ссылку ЮKassa.
  Её же даём сразу, если Prodamus сообщил, что платёж не прошёл.

Настройка действует и на уже отправленные кнопки: решение принимается в
момент перехода. Оплата ЮKassa выдаёт товар той же provision_payment, что
и вебхук Prodamus. Статус платежа берём из API: по уведомлению ЮKassa
(/yookassa/webhook) и опросом — уведомление может не дойти.
"""
import asyncio
import logging
import time
from html import escape

from aiohttp import web
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
from services import paylinks, yookassa
from services.prodamus import prodamus_url

logger = logging.getLogger(__name__)

PROVIDER_SETTING = "pay_provider"   # 'prodamus' | 'yookassa'
POLL_SECONDS = 20
ALERT_EVERY = 3600                  # не чаще раза в час про сбой ЮKassa

_last_alert = 0.0


async def current_provider() -> str:
    """Куда ведут ссылки сейчас. ЮKassa без ключей не выбирается."""
    if config.YOOKASSA_ENABLED and await db.get_setting(PROVIDER_SETTING) == "yookassa":
        return "yookassa"
    return "prodamus"


def backup_keyboard(order_num: str, amount: int, name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"💳 Оплатить через ЮKassa · {amount} ₽",
        url=paylinks.link(order_num, amount, name, via="yk"))]])


# ── Переход по ссылке ──────────────────────────────────────────────────────────

def _page(title: str, text: str, bot_username: str, status: int = 200) -> web.Response:
    back = (f'<p><a href="https://t.me/{bot_username}">Вернуться в бот</a></p>'
            if bot_username else "")
    return web.Response(status=status, content_type="text/html", text=(
        '<!doctype html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title>"
        '<body style="font:17px/1.5 system-ui,sans-serif;max-width:420px;margin:15vh auto;padding:0 16px">'
        f"<h2>{escape(title)}</h2><p>{escape(text)}</p>{back}</body>"))


async def _yookassa_url(link: dict, return_url: str) -> str:
    fresh = await db.fresh_yk_payment(link["order_num"], link["amount"])
    if fresh:
        return fresh["confirmation_url"]
    p = await yookassa.create_payment(link["kind"], link["order_num"], link["amount"],
                                      link["name"], return_url)
    url = p["confirmation"]["confirmation_url"]
    await db.add_yk_payment(p["id"], link["order_num"], link["kind"], link["amount"], url)
    return url


async def handle_pay(request: web.Request) -> web.Response:
    bot_username = request.app.get("bot_username", "")
    link = paylinks.parse_token(request.match_info["token"])
    if not link:
        return _page("Ссылка не работает",
                     "Похоже, ссылка повреждена. Вернитесь в бот и нажмите «Оплатить» ещё раз.",
                     bot_username, status=400)

    # Заказ физтовара уже оплачен — повторный переход по старой кнопке не
    # должен привести ко второй оплате
    if link["kind"] == "p":
        _, uid, pid = link["order_num"].split("_", 2)
        click = await db.get_pay_click(link["order_num"])
        if click and click["paid_at"] and not await db.get_pending_order(int(uid), int(pid)):
            return _page("Заказ уже оплачен ✅",
                         "Оплата по этому заказу уже пришла. Подтверждение — в боте.",
                         bot_username)

    provider = "yookassa" if link["via"] == "yk" and config.YOOKASSA_ENABLED \
        else await current_provider()
    shop = config.PRODAMUS_SHOP_URL_PHYSICAL if link["kind"] == "p" else config.PRODAMUS_SHOP_URL
    if not shop and config.YOOKASSA_ENABLED:
        provider = "yookassa"

    url = None
    if provider == "yookassa":
        try:
            url = await _yookassa_url(link, f"https://t.me/{bot_username}" if bot_username
                                      else "https://t.me/")
        except Exception as e:
            logger.error(f"pay: ЮKassa не создала платёж {link['order_num']}: {e}")
            await _alert(request.app["bot"], f"⚠️ <b>ЮKassa не создала платёж</b>\n"
                                             f"{escape(str(e))}\n\nКлиента отправил в Prodamus.")
            if link["kind"] == "t":   # проверочный платёж — только ЮKassa
                return _page("ЮKassa не отвечает", str(e), bot_username, status=502)
            provider = "prodamus"
    if provider == "prodamus":
        if not shop:
            return _page("Оплата временно недоступна",
                         "Напишите нам в боте — поможем оплатить.", bot_username, status=503)
        url = prodamus_url(link["order_num"], link["amount"], link["name"])

    await db.record_pay_click(link["order_num"], link["user_id"], link["amount"],
                              link["name"], provider)
    logger.info(f"pay: {link['order_num']} {link['amount']} ₽ → {provider}"
                f"{' (запасная)' if link['via'] else ''}")
    raise web.HTTPFound(url)


async def _alert(bot: Bot, text: str):
    global _last_alert
    if time.monotonic() - _last_alert < ALERT_EVERY:
        return
    _last_alert = time.monotonic()
    from handlers.prodamus_webhook import _send_notify
    await _send_notify(bot, text)


# ── Статус платежа ЮKassa ──────────────────────────────────────────────────────

async def handle_yookassa_webhook(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        payment_id = body["object"]["id"]
    except Exception:
        return web.Response(text="bad request", status=400)
    logger.info(f"yookassa webhook: {body.get('event')} {payment_id}")
    row = await db.get_yk_payment(payment_id)
    if row:
        await check_yk_payment(request.app["bot"], row)
    return web.Response(text="ok")


async def check_yk_payment(bot: Bot, row: dict):
    """Узнаёт статус платежа в ЮKassa и, если оплачен, выдаёт товар."""
    try:
        p = await yookassa.get_payment(row["kind"], row["id"])
    except Exception as e:
        logger.warning(f"yookassa: статус {row['id']} не получен: {e}")
        return
    status = p.get("status")
    if status != "succeeded":
        await db.set_yk_status(row["id"], status if status == "canceled" else "pending")
        return
    if not await db.claim_yk_payment(row["id"]):
        return   # уже выдали — уведомление и опрос пришли вместе
    income = float((p.get("income_amount") or {}).get("value") or 0) or None
    await db.set_yk_status(row["id"], "succeeded", income)
    amount = int(float(p["amount"]["value"]))
    order_num = (p.get("metadata") or {}).get("order_num") or row["order_num"]
    logger.info(f"yookassa: оплачен {row['id']} {order_num} {amount} ₽")

    from handlers.prodamus_webhook import _send_notify, provision_payment
    if order_num.startswith("t_"):
        await _send_notify(bot, f"🧪 Тестовый платёж ЮKassa на {amount} ₽ прошёл — "
                                f"оплата работает. Деньги можно вернуть в кабинете ЮKassa.")
        return
    order_type, user_id, product_id = order_num.split("_", 2)
    try:
        await provision_payment(bot, int(user_id), int(product_id), order_type,
                                f"yk-{row['id']}", amount)
    except Exception as e:
        logger.exception(f"yookassa: выдача {order_num} упала: {e}")
        await _send_notify(bot, f"🔴 <b>Оплата ЮKassa пришла, но заказ не провёлся</b>\n"
                                f"{order_num}, {amount} ₽, платёж {row['id']}\n{escape(str(e))}")
    # Чек в «Мой налог» — за любые полученные деньги, даже если выдача упала:
    # доход есть в любом случае. ЮKassa чеки самозанятых больше не делает.
    try:
        from services import npd_receipts
        name = await npd_receipts.receipt_name(order_type, int(product_id))
        await npd_receipts.issue_for_payment(bot, f"yk-{row['id']}", int(user_id), name,
                                             float(p["amount"]["value"]), p.get("captured_at"))
    except Exception as e:
        logger.exception(f"yookassa: чек за {row['id']} не заведён: {e}")


# ── Запасная ссылка ────────────────────────────────────────────────────────────

BACKUP_TEXT = (
    "Получилось оплатить? 🙂\n\n"
    "Если страница оплаты не открылась или карта не проходит, вот запасной способ — "
    "через ЮKassa: карта, SberPay или T-Pay. Сумма та же, {amount} ₽."
)
FAILED_TEXT = (
    "😔 Оплата не прошла. Так бывает: банк иногда отклоняет платёж.\n\n"
    "Попробуйте запасной способ — через ЮKassa: карта, SberPay или T-Pay. "
    "Сумма та же, {amount} ₽."
)


async def _backup_allowed(click: dict) -> bool:
    """Заказ ещё ждёт оплаты и его оформлял сам клиент (ссылку заказа для
    друга админ пересылает другу — спрашивать админа незачем)."""
    order_type, uid, pid = click["order_num"].split("_", 2)
    if order_type == "p":
        pending = await db.get_pending_order(int(uid), int(pid))
        return bool(pending) and not pending.get("friend_by")
    return order_type in ("c", "d")


async def offer_after_failure(bot: Bot, order_num: str) -> bool:
    """Prodamus сообщил, что платёж не прошёл: сразу даём ЮKassa.
    False — предложить нечего (ЮKassa нет или ссылка была не наша)."""
    if not config.YOOKASSA_ENABLED:
        return False
    click = await db.get_pay_click(order_num)
    if not click or click["paid_at"] or not await _backup_allowed(click):
        return False
    await db.claim_pay_backup(order_num)
    await bot.send_message(click["user_id"], FAILED_TEXT.format(amount=click["amount"]),
                           reply_markup=backup_keyboard(order_num, click["amount"], click["name"]))
    return True


async def _send_backups(bot: Bot):
    for click in await db.due_pay_backups(config.PAY_BACKUP_DELAY_MIN):
        if not await db.claim_pay_backup(click["order_num"]):
            continue
        # Оплата могла прийти, пока шёл обход, — спрашиваем только неоплативших
        fresh = await db.get_pay_click(click["order_num"])
        if not fresh or fresh["paid_at"] or not await _backup_allowed(fresh):
            continue
        try:
            await bot.send_message(
                click["user_id"], BACKUP_TEXT.format(amount=click["amount"]),
                reply_markup=backup_keyboard(click["order_num"], click["amount"], click["name"]))
            logger.info(f"pay: запасная ссылка {click['order_num']}")
        except Exception as e:
            logger.warning(f"pay: запасную ссылку {click['order_num']} не отправил: {e}")


async def pay_worker(bot: Bot):
    """Опрос платежей ЮKassa и запасные ссылки после Prodamus."""
    while True:
        try:
            if config.YOOKASSA_ENABLED:
                for row in await db.yk_payments_to_check():
                    await check_yk_payment(bot, row)
                await _send_backups(bot)
        except Exception as e:
            logger.exception(f"pay_worker: {e}")
        await asyncio.sleep(POLL_SECONDS)


def setup_web(app: web.Application, bot_username: str = ""):
    app["bot_username"] = bot_username
    app.router.add_get("/pay/{token}", handle_pay)
    app.router.add_post("/yookassa/webhook", handle_yookassa_webhook)
