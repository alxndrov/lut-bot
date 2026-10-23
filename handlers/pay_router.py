"""
Куда ведёт ссылка на оплату: PayAnyWay, Prodamus или ЮKassa.

С 23.10.2026 основная — PayAnyWay (платежи принимает Барышов М. П., чеки
делает PayAnyWay.Self). Prodamus — только для зарубежных карт (via='pd',
кнопка «У меня зарубежная карта»). ЮKassa — по выбору в /payments.

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
from datetime import datetime, timezone
from html import escape

from aiohttp import web
from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
import secrets

from services import lava, payanyway, paylinks, yookassa
from services.prodamus import prodamus_url

logger = logging.getLogger(__name__)
router = Router()   # кнопка «У меня зарубежная карта» в основном боте

PROVIDER_SETTING = "pay_provider"   # 'payanyway' | 'prodamus' | 'yookassa'
POLL_SECONDS = 20
ALERT_EVERY = 3600                  # не чаще раза в час про сбой ЮKassa

_last_alert = 0.0
_lava_polled = [0.0]


async def current_provider(kind: str = "c") -> str:
    """Куда ведут ссылки сейчас. Схема Дани (23.10.2026): цифра (курс, LUT) —
    PayAnyWay Миши, физтовары ('p') — Prodamus Дани. Настройка /payments
    действует на цифру (на случай сбоя PayAnyWay). Способ без ключей не выбирается."""
    chosen = await db.get_setting(PROVIDER_SETTING)
    if kind == "p":
        return "yookassa" if chosen == "yookassa" and config.YOOKASSA_ENABLED else "prodamus"
    if chosen == "yookassa" and config.YOOKASSA_ENABLED:
        return "yookassa"
    if chosen == "prodamus":
        return "prodamus"
    return "payanyway" if config.PAYANYWAY_ENABLED else "prodamus"


def backup_keyboard(order_num: str, amount: int, name: str,
                    provider: str = "yookassa") -> InlineKeyboardMarkup:
    first = (InlineKeyboardButton(text=f"💳 Оплатить ещё раз · {amount} ₽",
                                  url=paylinks.link(order_num, amount, name, via="paw"))
             if provider == "payanyway" else
             InlineKeyboardButton(text=f"💳 Оплатить через ЮKassa · {amount} ₽",
                                  url=paylinks.link(order_num, amount, name, via="yk")))
    return InlineKeyboardMarkup(inline_keyboard=[
        [first],
        # Зарубежные карты принимает Prodamus — в обычные поля карты на его странице
        [InlineKeyboardButton(text="🌍 У меня зарубежная карта",
                              callback_data=f"payforeign:{order_num}")],
    ])


FOREIGN_TEXT = (
    "Зарубежной картой можно оплатить на странице Prodamus: откройте ссылку ниже "
    "и введите данные карты — номер, срок и CVC — в поля карты.\n\n"
    "Сумма {amount} ₽ — банк спишет её в валюте вашей карты по своему курсу."
)
FOREIGN_DIGITAL_TEXT = (
    "Оплата картой зарубежного банка — в долларах через lava.top. По ссылке ниже "
    "укажите email (туда придёт подтверждение) и оплатите картой.\n\n"
    "Pay by a non-Russian card in USD via lava.top: enter your email and pay."
)


@router.callback_query(F.data.startswith("payforeign:"))
async def cb_foreign_card(callback: CallbackQuery):
    order_num = callback.data.split(":", 1)[1]
    await callback.answer()
    click = await db.get_pay_click(order_num)
    if not click or click["paid_at"]:
        await callback.message.answer("Этот заказ уже оплачен или устарел — "
                                      "откройте товар в каталоге заново 🤍")
        return
    digital = order_num[:1] in ("c", "d") and config.LAVA_ENABLED
    await callback.message.answer(
        FOREIGN_DIGITAL_TEXT if digital else FOREIGN_TEXT.format(amount=click["amount"]),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=f"🌍 Оплатить зарубежной картой · {click['amount']} ₽",
            url=paylinks.link(order_num, click["amount"], click["name"],
                              via="lava" if digital else "pd"))]]))
    # Админам — одно сообщение на заказ, для сведения
    key = f"foreign_card:{order_num}"
    if await db.get_setting(key):
        return
    await db.set_setting(key, datetime.now(timezone.utc).isoformat())
    u = callback.from_user
    who = f"@{u.username}" if u.username else f"id:{u.id}"
    from handlers.prodamus_webhook import _send_notify
    await _send_notify(callback.bot, (
        f"🌍 <b>Клиент платит зарубежной картой</b>\n\n"
        f"👤 {escape(u.full_name or '—')} {who}\n"
        f"🛍 {escape(click['name'] or order_num)} — {click['amount']} ₽\n\n"
        f"Бот дал ему ссылку на оплату (цифра — lava.top, физтовар — Prodamus). Если оплата не придёт — напишите клиенту."))
    logger.info(f"pay: зарубежная карта — {order_num} ({who})")


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


async def _payanyway_url(link: dict, return_url: str) -> str:
    fresh = await db.fresh_paw_payment(link["order_num"], link["amount"])
    if fresh:
        return fresh["url"]
    txn = payanyway.new_transaction_id(link["order_num"])
    url = payanyway.form_url(txn, link["amount"], link["name"], str(link["user_id"]), return_url)
    await db.add_paw_payment(txn, link["order_num"], link["kind"], link["amount"], link["name"], url)
    return url


def _lava_form(link: dict, usd: int, rate: float, email: str, error: str = "") -> web.Response:
    err = f'<p style="color:#c0392b">{escape(error)}</p>' if error else ""
    return web.Response(content_type="text/html", text=(
        '<!doctype html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Payment by foreign card</title>"
        '<body style="font:17px/1.5 system-ui,sans-serif;max-width:420px;margin:10vh auto;padding:0 16px">'
        f"<h2>🌍 {escape(link['name'])}</h2>"
        f"<p>К оплате: <b>${usd}</b> (≈ {link['amount']} ₽ по курсу ЦБ {rate:.2f} ₽ "
        f"+ комиссия за зарубежную оплату).<br><small>Total: ${usd} USD.</small></p>"
        "<p>Email — на него платёжный сервис пришлёт подтверждение оплаты.<br>"
        "<small>Email for the payment confirmation.</small></p>"
        f"{err}<form method=post>"
        f'<input name=email type=email required value="{escape(email)}" placeholder="you@example.com" '
        'style="width:100%;font:inherit;padding:10px;box-sizing:border-box;border:1px solid #bbb;border-radius:8px">'
        '<button style="margin-top:12px;width:100%;font:inherit;padding:12px;border:0;border-radius:8px;'
        'background:#111;color:#fff">Перейти к оплате · Pay</button></form>'
        '<p><small>Оплата через lava.top (LAVALANE LTD). Продолжая, вы принимаете оферту '
        "и согласие на обработку персональных данных.</small></p></body>"))


async def _lava_page(request: web.Request, link: dict, bot_username: str) -> web.Response:
    """Зарубежная карта на цифру: спрашиваем email (подставляем прошлый), создаём
    счёт lava.top в долларах и отправляем на его страницу."""
    try:
        rate = await lava.usd_rate()
    except lava.LavaError as e:
        logger.error(f"lava: {e}")
        return _page("Оплата временно недоступна", "Напишите нам в боте — поможем оплатить.",
                     bot_username, status=503)
    usd = lava.usd_price(link["amount"], rate)
    saved = await db.get_user_email(link["user_id"]) or ""
    if request.method != "POST":
        return _lava_form(link, usd, rate, saved)
    email = ((await request.post()).get("email") or "").strip()
    if not lava.valid_email(email):
        return _lava_form(link, usd, rate, email, "Похоже, в адресе ошибка. Please check the email.")
    if email != saved:
        await db.set_user_email(link["user_id"], email)
    fresh = await db.fresh_lava_payment(link["order_num"], link["amount"], email)
    if fresh:
        url = fresh["url"]
    else:
        try:
            inv = await lava.create_invoice(link["kind"], link["order_num"], usd, email,
                                            f"https://t.me/{bot_username}" if bot_username else "")
        except lava.LavaError as e:
            logger.error(f"lava: счёт {link['order_num']} не создан: {e}")
            if "email" in str(e).lower():
                return _lava_form(link, usd, rate, email,
                                  "Этот email платёжный сервис не принимает — укажите другой.")
            await _alert(request.app["bot"], f"⚠️ <b>lava.top не создала счёт</b>\n"
                                             f"{escape(link['order_num'])}: {escape(str(e))}")
            return _page("Оплата временно недоступна", "Напишите нам в боте — поможем оплатить.",
                         bot_username, status=502)
        url = inv["paymentUrl"]
        await db.add_lava_payment(inv["id"], link["order_num"], link["kind"], link["amount"],
                                  usd, rate, email, link["name"], url)
    await db.record_pay_click(link["order_num"], link["user_id"], link["amount"],
                              link["name"], "lava")
    logger.info(f"pay: {link['order_num']} {link['amount']} ₽ → lava.top ${usd}")
    raise web.HTTPSeeOther(url)


async def handle_lava_webhook(request: web.Request) -> web.Response:
    """payment.success / payment.failed от lava.top. Оплату перепроверяем
    запросом счёта своим ключом — тело вебхука само по себе не доказательство."""
    if config.LAVA_WEBHOOK_SECRET:
        got = request.headers.get("X-Api-Key", "")
        if not got or not secrets.compare_digest(got, config.LAVA_WEBHOOK_SECRET):
            logger.warning("lava webhook: неверный ключ")
            return web.Response(status=401, text="unauthorized")
    try:
        body = await request.json()
        invoice_id = body.get("contractId") or ""
    except Exception:
        return web.Response(status=400, text="bad request")
    logger.info(f"lava webhook: {body.get('eventType')} {invoice_id} {body.get('amount')} {body.get('currency')}")
    row = await db.get_lava_payment(invoice_id)
    if row:
        await check_lava_payment(request.app["bot"], row)
    return web.Response(text="ok")


async def check_lava_payment(bot: Bot, row: dict):
    try:
        inv = await lava.get_invoice(row["id"])
    except Exception as e:
        logger.warning(f"lava: статус {row['id']} не получен: {e}")
        return
    status = inv.get("status")
    if status == "FAILED":
        await db.set_lava_status(row["id"], "failed")
        return
    if status != "COMPLETED":
        return
    receipt = inv.get("receipt") or {}
    usd = float(receipt.get("amount") or row["amount_usd"])
    rate = await lava.usd_rate()          # курс ЦБ на день получения денег — для чека и учёта
    paid_rub = int(round(usd * rate))
    if not await db.claim_lava_payment(row["id"], paid_rub, receipt.get("fee")):
        return
    logger.info(f"lava: оплачен {row['id']} {row['order_num']} ${usd} = {paid_rub} ₽")
    from handlers.prodamus_webhook import _send_notify, provision_payment
    order_type, user_id, product_id = row["order_num"].split("_", 2)
    try:
        await provision_payment(bot, int(user_id), int(product_id), order_type,
                                f"lava-{row['id']}", paid_rub)
    except Exception as e:
        logger.exception(f"lava: выдача {row['order_num']} упала: {e}")
        await _send_notify(bot, f"🔴 <b>Оплата lava.top пришла, но заказ не провёлся</b>\n"
                                f"{escape(row['order_num'])}, ${usd:.0f} ({paid_rub} ₽)\n{escape(str(e))}")
    # Чек — в «Мой налог» Миши: lava.top чеков не делает
    try:
        from services import npd_receipts
        name = await npd_receipts.receipt_name(order_type, int(product_id))
        await npd_receipts.issue_for_payment(bot, f"lava-{row['id']}", int(user_id), name,
                                             float(paid_rub), inv.get("datetime"), account="misha")
    except Exception as e:
        logger.exception(f"lava: чек за {row['id']} не заведён: {e}")


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

    # Зарубежная карта: цифра — lava.top Миши (спрашиваем email), физтовары —
    # Prodamus Дани
    if link["via"] in ("lava", "pd") and link["kind"] in ("c", "d") and config.LAVA_ENABLED:
        return await _lava_page(request, link, bot_username)
    # via: yk — запасная ЮKassa, pd — только Prodamus (зарубежная карта на физтовар),
    # paw — PayAnyWay (повтор оплаты, проверочный платёж)
    provider = ("yookassa" if link["via"] == "yk" and config.YOOKASSA_ENABLED
                else "prodamus" if link["via"] in ("pd", "lava")
                else "payanyway" if link["via"] == "paw" and config.PAYANYWAY_ENABLED
                else await current_provider(link["kind"]))
    shop = config.PRODAMUS_SHOP_URL_PHYSICAL if link["kind"] == "p" else config.PRODAMUS_SHOP_URL
    if provider == "prodamus" and not shop and config.YOOKASSA_ENABLED:
        provider = "yookassa"

    url = None
    if provider == "payanyway":
        url = await _payanyway_url(link, f"https://t.me/{bot_username}" if bot_username else "")
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


async def _notify(bot: Bot, text: str):
    from handlers.prodamus_webhook import _send_notify
    await _send_notify(bot, text)


async def _alert(bot: Bot, text: str):
    global _last_alert
    if time.monotonic() - _last_alert < ALERT_EVERY:
        return
    _last_alert = time.monotonic()
    from handlers.prodamus_webhook import _send_notify
    await _send_notify(bot, text)


# ── Уведомление PayAnyWay (Pay URL) ────────────────────────────────────────────

def _xml(txn: str, code: int) -> web.Response:
    return web.Response(text=payanyway.response_xml(txn, code), content_type="application/xml")


async def handle_payanyway_notify(request: web.Request) -> web.Response:
    """Оплата прошла. Проверяем подпись, номер оплаты и сумму, выдаём товар
    той же provision_payment. Повторное уведомление — просто 200."""
    params = dict(request.query)
    if request.method == "POST":
        params.update({k: v for k, v in (await request.post()).items() if isinstance(v, str)})
    txn = params.get("MNT_TRANSACTION_ID", "")
    bot = request.app["bot"]
    logger.info(f"payanyway: уведомление {txn} {params.get('MNT_AMOUNT')} ₽ "
                f"op={params.get('MNT_OPERATION_ID')} test={params.get('MNT_TEST_MODE')}")
    if not config.PAYANYWAY_ENABLED:
        return web.Response(text="FAIL", status=503)
    if not txn:   # пустой запрос (сканеры, проверка адреса) — без тревоги
        return web.Response(text="FAIL")
    if params.get("MNT_ID") != config.PAYANYWAY_MNT_ID or not payanyway.notification_valid(params):
        # FAIL — PayAnyWay повторит в течение суток: успеем поправить код в .env
        logger.warning(f"payanyway: подпись/счёт не сошлись для {txn}")
        await _alert(bot, "⚠️ <b>PayAnyWay: уведомление об оплате с неверной подписью</b>\n"
                          f"Оплата {escape(txn)}. Проверьте PAYANYWAY_SECRET (код проверки "
                          "целостности) в .env — PayAnyWay повторит уведомление.")
        return web.Response(text="FAIL")
    row = await db.get_paw_payment(txn)
    if not row:
        await _notify(bot, f"⚠️ <b>PayAnyWay: оплата {escape(txn)} не из бота</b>\n"
                          f"{escape(params.get('MNT_AMOUNT', ''))} ₽ — заказ не найден, товар не выдан.")
        return _xml(txn, 200)
    amount = float(params.get("MNT_AMOUNT") or 0)
    if abs(amount - row["amount"]) > 0.009:
        await _notify(bot, f"⚠️ <b>PayAnyWay: сумма не совпала</b>\n{escape(row['order_num'])}: "
                          f"ждали {row['amount']} ₽, пришло {amount:.2f} ₽. Товар не выдан — "
                          "разберитесь вручную.")
        return _xml(txn, 200)
    op = params.get("MNT_OPERATION_ID") or txn
    if not await db.claim_paw_payment(txn, op):
        return _xml(txn, 200)   # уже выдали
    from handlers.prodamus_webhook import _send_notify, provision_payment
    if row["kind"] == "t" or params.get("MNT_TEST_MODE") == "1":
        await _send_notify(bot, f"🧪 Тестовый платёж PayAnyWay на {amount:.0f} ₽ прошёл — "
                                f"оплата и уведомления работают. Товар не выдаётся.")
        return _xml(txn, 200)
    order_type, user_id, product_id = row["order_num"].split("_", 2)
    try:
        await provision_payment(bot, int(user_id), int(product_id), order_type,
                                f"paw-{txn}", int(round(amount)))
    except Exception as e:
        logger.exception(f"payanyway: выдача {row['order_num']} упала: {e}")
        await _send_notify(bot, f"🔴 <b>Оплата PayAnyWay пришла, но заказ не провёлся</b>\n"
                                f"{escape(row['order_num'])}, {amount:.0f} ₽, оплата {escape(txn)}\n"
                                f"{escape(str(e))}")
    # Чек делает PayAnyWay.Self в «Мой налог» Миши — бот его не пробивает
    return _xml(txn, 200)


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

PAW_BACKUP_TEXT = (
    "Получилось оплатить? 🙂\n\n"
    "Если страница оплаты не открылась или банк отклонил платёж — попробуйте ещё раз: "
    "картой или через СБП. Сумма та же, {amount} ₽.\n\n"
    "Карта зарубежного банка? Нажмите кнопку ниже."
)
BACKUP_TEXT = (
    "Получилось оплатить? 🙂\n\n"
    "Если страница оплаты не открылась или карта не проходит, вот запасной способ — "
    "через ЮKassa: карта, SberPay или T-Pay. Сумма та же, {amount} ₽.\n\n"
    "Карта зарубежного банка? Нажмите кнопку ниже — поможем оплатить."
)
FAILED_TEXT = (
    "😔 Оплата не прошла. Так бывает: банк иногда отклоняет платёж.\n\n"
    "Попробуйте запасной способ — через ЮKassa: карта, SberPay или T-Pay. "
    "Сумма та же, {amount} ₽.\n\n"
    "Карта зарубежного банка? Нажмите кнопку ниже — поможем оплатить."
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
        # Перешли в PayAnyWay — «оплатить ещё раз» + зарубежная карта; в Prodamus
        # (основным он бывает по выбору в /payments) — запасная ЮKassa
        if click.get("provider") == "payanyway":
            text = PAW_BACKUP_TEXT
        elif click.get("provider") == "prodamus" and config.YOOKASSA_ENABLED \
                and await current_provider(click["order_num"][:1]) == "prodamus":
            text = BACKUP_TEXT
        else:
            continue   # зарубежная карта в Prodamus при основном PayAnyWay — не дёргаем
        try:
            await bot.send_message(
                click["user_id"], text.format(amount=click["amount"]),
                reply_markup=backup_keyboard(click["order_num"], click["amount"], click["name"],
                                             click.get("provider") or "yookassa"))
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
            # lava.top — опрос раз в 2 минуты, на случай если вебхук не дошёл
            if config.LAVA_ENABLED and time.monotonic() - _lava_polled[0] > 120:
                _lava_polled[0] = time.monotonic()
                for row in await db.lava_payments_to_check():
                    await check_lava_payment(bot, row)
            if config.YOOKASSA_ENABLED or config.PAYANYWAY_ENABLED:
                await _send_backups(bot)
        except Exception as e:
            logger.exception(f"pay_worker: {e}")
        await asyncio.sleep(POLL_SECONDS)


def setup_web(app: web.Application, bot_username: str = ""):
    app["bot_username"] = bot_username
    app.router.add_route("*", "/pay/{token}", handle_pay)   # POST — email для lava.top
    app.router.add_post("/lava/webhook", handle_lava_webhook)
    app.router.add_post("/yookassa/webhook", handle_yookassa_webhook)
    app.router.add_route("*", "/payanyway/notify", handle_payanyway_notify)
