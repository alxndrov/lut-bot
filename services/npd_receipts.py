"""Чеки «Мой налог» за оплаты через ЮKassa — автоматически.

После оплаты ЮKassa (handlers/pay_router.check_yk_payment) бот регистрирует
доход в «Мой налог» на сумму оплаты и присылает покупателю ссылку на чек.
Не вышло (вход слетел, сайт налоговой недоступен) — чек остаётся в очереди,
админам приходит сообщение, бот повторяет каждые 10 минут. При полном
возврате по такому платежу чек отменяется сам (причина «Возврат средств»).

Оплаты Prodamus сюда не попадают — чеки по ним делает Prodamus.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot

import config
import database as db
from services import npd

logger = logging.getLogger(__name__)

RETRY_EVERY = 600
# Строка о чеке внизу уведомления о продаже / карточки заказа (оплаты ЮKassa)
LINE_PENDING = "🧾 Чек: пробиваю в «Мой налог»…"
LINE_FAILED = "⚠️ Чек ещё не пробит — бот повторяет сам, статус в /nalog"
LINE_CANCELLED = "🧾 Чек отменён — возврат"


def line_issued(url: str, delivered: bool) -> str:
    link = f'<a href="{url}">Чек</a>'
    return (f"🧾 {link} отправлен клиенту" if delivered else
            f"⚠️ {link} пробит, но клиенту не доставлен (бот у него заблокирован?) — "
            f"отправьте ссылку вручную")
# Сверка: дошли ли до «Мой налог» чеки Продамуса. С 08.10.2026 Продамус перестал
# их передавать — ждём неделю (до 21.10), потом напоминаем раз в неделю, пока не починят.
S_PRODAMUS_FROM = "npd_prodamus_watch_from"   # с какой даты сверять оплаты (YYYY-MM-DD, МСК)
S_PRODAMUS_NEXT = "npd_prodamus_next_check"   # когда сверять в следующий раз (YYYY-MM-DD)
# В этот день спросить Даню о результате, даже если всё в порядке (решение Дани:
# Продамус обещал выдать чеки за 08–12.10 — 07.11 проверить; крайний срок 09.11)
S_PRODAMUS_ASK = "npd_prodamus_ask_on"
PRODAMUS_GRACE = timedelta(days=2)            # свежим оплатам даём время
MSK = timezone(timedelta(hours=3))
MAX_ATTEMPTS = 6 * 24 * 14   # повторяем раз в 10 минут две недели — домашний комп мог быть выключен
_reminded_day: dict = {"day": None}


async def receipt_name(order_type: str, product_id: int) -> str:
    """Что написать в чеке. Курс — по варианту, остальное — по названию товара."""
    if order_type == "c":
        option = await db.get_course_option(product_id)
        if option:
            return f"Доступ к онлайн-курсу: {option['title']}"
    product = await db.get_product(product_id)
    return (product or {}).get("name") or "Оплата заказа"


async def _notify_admins(text: str):
    from handlers.prodamus_webhook import _send_notify
    await _send_notify(None, text)


async def update_sale_messages(payment_id: str, line: str):
    """Дописывает строку о чеке под уведомлением о продаже у всех админов:
    курс и цифра — сообщение «Курс оплачен»/«Новая покупка», физтовар —
    карточка заказа. Ошибка здесь не мешает самому чеку."""
    try:
        messages = await db.get_sale_messages(payment_id)
        if messages:
            admin_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
            try:
                for m in messages:
                    try:
                        await admin_bot.edit_message_text(
                            f"{m['text']}\n\n{line}", chat_id=m["chat_id"],
                            message_id=m["message_id"], parse_mode="HTML",
                            disable_web_page_preview=True)
                    except Exception as e:
                        logger.info(f"npd: уведомление {m['chat_id']}/{m['message_id']} "
                                    f"не обновлено: {e}")
            finally:
                await admin_bot.session.close()
        order_id = await db.get_order_id_by_payment(payment_id)
        if order_id:
            await db.set_order_receipt_line(order_id, line)
            from handlers.order_actions import _sync_order_messages
            admin_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
            try:
                await _sync_order_messages(admin_bot, await db.get_order(order_id))
            finally:
                await admin_bot.session.close()
    except Exception:
        logger.exception(f"npd: строка о чеке для {payment_id} не обновлена")


async def _already_issued(row: dict, paid: datetime | None) -> str | None:
    """Прошлая попытка могла создать чек, но ответ не дошёл (таймаут). Перед
    повтором ищем в «Мой налог» чек с той же суммой, названием и временем
    оплаты, ещё не привязанный к другой оплате, — чтобы не пробить второй."""
    when = paid or datetime.now(timezone.utc)
    known = {r["receipt_uuid"] for r in await db.get_npd_receipts() if r.get("receipt_uuid")}
    for i in await npd.list_incomes(when - timedelta(days=1), datetime.now(timezone.utc)):
        if (not i.get("cancellationInfo") and i.get("approvedReceiptUuid") not in known
                and abs(float(i["totalAmount"]) - float(row["amount"])) < 0.01
                and i.get("name") == row["name"]
                and (not paid or abs(datetime.fromisoformat(i["operationTime"]) - paid)
                     < timedelta(minutes=2))):
            logger.info(f"npd: чек за {row['payment_id']} уже есть — {i['approvedReceiptUuid']}")
            return i["approvedReceiptUuid"]
    return None


async def _try_issue(bot: Bot, row: dict) -> bool:
    paid = None
    if row.get("paid_at"):
        try:
            paid = datetime.fromisoformat(row["paid_at"].replace("Z", "+00:00"))
        except ValueError:
            paid = None
    # Только после таймаута: тогда чек мог создаться без ответа нам
    timed_out = npd.TIMEOUT_MSG in (row.get("error") or "")
    uuid = await _already_issued(row, paid) if timed_out else None
    if not uuid:
        uuid = await npd.create_receipt(row["name"], row["amount"], paid)
    await db.set_npd_receipt(row["payment_id"], "issued", receipt_uuid=uuid)
    url = await npd.print_url(uuid)
    logger.info(f"npd: чек {uuid} за {row['payment_id']} — {row['amount']} ₽")
    delivered = False
    if row.get("user_id"):
        try:
            await bot.send_message(row["user_id"], f'🧾 <a href="{url}">Чек об оплате</a> — '
                                                   f'{row["amount"]:.0f} ₽', parse_mode="HTML")
            delivered = True
        except Exception as e:
            logger.info(f"npd: чек клиенту {row['user_id']} не отправлен: {e}")
    await update_sale_messages(row["payment_id"], line_issued(url, delivered))
    return True


async def issue_for_payment(bot: Bot, payment_id: str, user_id: int | None, name: str,
                            amount: float, paid_at: str | None):
    """Заводит чек по оплате ЮKassa и сразу пробует его пробить."""
    if not await db.add_npd_receipt(payment_id, user_id, name, amount, paid_at):
        return
    row = await db.get_npd_receipt(payment_id)
    try:
        await _try_issue(bot, row)
    except Exception as e:
        await db.set_npd_receipt(payment_id, "failed", error=str(e)[:300])
        logger.warning(f"npd: чек за {payment_id} не пробит: {e}")
        await update_sale_messages(payment_id, LINE_FAILED)
        hint = ("Подключи «Мой налог» командой /nalog — бот пробьёт чек сам."
                if isinstance(e, npd.NpdNotConnected) else
                "Бот повторит сам каждые 10 минут; статус — /nalog.")
        await _notify_admins(
            f"⚠️ <b>Чек в «Мой налог» не пробит</b>\n{escape(name)} — {amount:.0f} ₽ "
            f"(платёж ЮKassa {escape(payment_id)})\n{escape(str(e)[:200])}\n\n{hint}")


async def retry_failed(bot: Bot) -> tuple[int, int]:
    """Повторяет непробитые. Возвращает (пробито, осталось)."""
    rows = [r for r in await db.get_npd_receipts(("pending", "failed"))
            if r["attempts"] < MAX_ATTEMPTS]
    done = 0
    for row in rows:
        try:
            await _try_issue(bot, row)
            done += 1
        except npd.NpdNotConnected as e:
            await db.set_npd_receipt(row["payment_id"], "failed", error=str(e)[:300])
            break   # без входа остальные тоже не пробьются
        except Exception as e:
            await db.set_npd_receipt(row["payment_id"], "failed", error=str(e)[:300])
    left = len(await db.get_npd_receipts(("pending", "failed")))
    return done, left


async def cancel_for_refund(payment_id: str) -> bool:
    """Полный возврат по оплате ЮKassa — отменить её чек. True — отменён."""
    row = await db.get_npd_receipt(payment_id)
    if not row or row["status"] != "issued" or not row.get("receipt_uuid"):
        return False
    try:
        await npd.cancel_receipt(row["receipt_uuid"], npd.CANCEL_REFUND)
    except Exception as e:
        logger.warning(f"npd: чек {row['receipt_uuid']} не отменён: {e}")
        return False
    await db.set_npd_receipt(payment_id, "cancelled")
    await update_sale_messages(payment_id, LINE_CANCELLED)
    logger.info(f"npd: чек {row['receipt_uuid']} отменён (возврат {payment_id})")
    return True


def _is_prodamus(payment_id: str) -> bool:
    # paw- — PayAnyWay: чеки в «Мой налог» Миши, сюда не относятся
    return not payment_id.startswith(("yk-", "paw-", "manual", "test"))


async def prodamus_missing() -> list[dict]:
    """Оплаты Продамуса без чека в «Мой налог»: [{payment_id, amount, paid, users, names}]."""
    since_s = await db.get_setting(S_PRODAMUS_FROM)
    if not since_s:
        return []
    since = datetime.fromisoformat(since_s).replace(tzinfo=MSK)
    until = datetime.now(timezone.utc) - PRODAMUS_GRACE
    groups: dict[str, dict] = {}
    for p in await db.get_purchases_between(since.astimezone(timezone.utc), until):
        pid = str(p["telegram_payment_id"] or "")
        if not _is_prodamus(pid):
            continue
        g = groups.setdefault(pid, {"payment_id": pid, "amount": 0, "users": set(), "names": [],
                                    "user_id": p["user_id"], "option_id": p.get("course_option_id"),
                                    "paid": datetime.strptime(p["created_at"][:19], "%Y-%m-%d %H:%M:%S")})
        g["amount"] += p["amount"]
        g["users"].add(p.get("username") or str(p["user_id"]))
        g["names"].append(p.get("pname") or "")
    if not groups:
        return []
    incomes = [i for i in await npd.list_incomes(since - timedelta(days=1), datetime.now(MSK))
               if not i.get("cancellationInfo")]
    used: set[int] = set()

    def when(i):   # у Продамуса operationTime по факту в UTC, хоть и с «+03:00»
        return datetime.fromisoformat(i["operationTime"]).replace(tzinfo=None)
    missing = []
    for g in sorted(groups.values(), key=lambda g: g["paid"]):
        cands = [(abs(when(i) - g["paid"]), n) for n, i in enumerate(incomes)
                 if n not in used and abs(float(i["totalAmount"]) - g["amount"]) < 0.01
                 and abs(when(i) - g["paid"]) <= timedelta(days=1)]
        if cands:
            used.add(min(cands)[1])
        else:
            missing.append(g)
    return missing


async def _notify_admins_kb(text: str, kb):
    admin_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
    try:
        for admin_id in config.ADMIN_IDS:
            try:
                await admin_bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=kb)
            except Exception as e:
                logger.warning(f"npd: админу {admin_id} не отправлено: {e}")
    finally:
        await admin_bot.session.close()


def _missing_lines(missing: list[dict]) -> list[str]:
    total = sum(g["amount"] for g in missing)
    lines = [f"Без чека {len(missing)} оплат на {total:,.0f} ₽".replace(",", " ") + ":", ""]
    for g in missing[:25]:
        paid = (g["paid"] + timedelta(hours=3)).strftime("%d.%m")
        lines.append(f"• {paid} — {g['amount']:.0f} ₽ · "
                     + ", ".join("@" + u for u in sorted(g["users"])))
    if len(missing) > 25:
        lines.append(f"… и ещё {len(missing) - 25}")
    return lines


async def check_prodamus(force: bool = False) -> list[dict] | None:
    """Раз в день смотрит, не пора ли сверить. None — не пора или нет связи."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    today = datetime.now(MSK).date()
    next_s = await db.get_setting(S_PRODAMUS_NEXT)
    if not force and (not next_s or today < datetime.fromisoformat(next_s).date()):
        return None
    try:
        missing = await prodamus_missing()
    except Exception as e:
        logger.warning(f"npd: сверка Продамуса не вышла: {e}")
        return None   # повторим через 10 минут
    ask_s = await db.get_setting(S_PRODAMUS_ASK)
    asking = bool(ask_s) and today >= datetime.fromisoformat(ask_s).date()
    if missing:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=f"🧾 Пробить эти {len(missing)} чеков через бота", callback_data="npd:prodamus_issue")]])
        if asking:
            head = ["🧾 <b>Проверка чеков Продамуса — выдал ли он все?</b>",
                    "Нет, не все. Крайний срок — 9 ноября."]
            tail = ["", "Пробить оставшиеся через бота? Клиенты получат ссылку на чек. "
                        "Если Продамус вот-вот дошлёт — не нажимай, спрошу завтра снова."]
            nxt = today + timedelta(days=1)
        else:
            head = ["⚠️ <b>Продамус не передаёт чеки в «Мой налог»</b>"]
            tail = ["", "Следующая сверка — через неделю."]
            nxt = today + timedelta(days=7)
        await _notify_admins_kb("\n".join(head + _missing_lines(missing) + tail), kb)
        await db.set_setting(S_PRODAMUS_NEXT, nxt.isoformat())
    else:
        if asking:
            await _notify_admins("✅ <b>Проверка чеков Продамуса:</b> все оплаты через Продамус "
                                 "с чеками в «Мой налог» — всё сработало.")
            await db.set_setting(S_PRODAMUS_ASK, "")
        await db.set_setting(S_PRODAMUS_NEXT, (today + timedelta(days=1)).isoformat())
    return missing


async def issue_prodamus_missing(bot: Bot) -> tuple[int, int]:
    """Пробивает чеки за оплаты Продамуса, по которым он их не выдал.
    Возвращает (пробито, не вышло)."""
    done = failed = 0
    for g in await prodamus_missing():
        if g.get("option_id"):
            name = await receipt_name("c", g["option_id"])
        else:
            name = ", ".join(dict.fromkeys(n for n in g["names"] if n)) or "Оплата заказа"
        await issue_for_payment(bot, g["payment_id"], g["user_id"], name, float(g["amount"]),
                                g["paid"].strftime("%Y-%m-%dT%H:%M:%SZ"))
        row = await db.get_npd_receipt(g["payment_id"])
        if row and row["status"] == "issued":
            done += 1
        else:
            failed += 1
    return done, failed


async def retry_loop(bot: Bot):
    while True:
        await asyncio.sleep(RETRY_EVERY)
        try:
            done, left = await retry_failed(bot)
            if done:
                await _notify_admins(f"🧾 Пробил отложенные чеки в «Мой налог»: {done}"
                                     + (f", осталось {left}" if left else ""))
            if await npd.connected():
                await check_prodamus()
            # Раз в день — если чеки висят: не подключён вход или нет связи
            today = datetime.now(timezone(timedelta(hours=3))).date()
            if left and _reminded_day["day"] != today:
                _reminded_day["day"] = today
                if not await npd.connected():
                    await _notify_admins(f"🧾 {left} чек(ов) ждут, пока подключат «Мой налог»: /nalog")
                else:
                    rows = await db.get_npd_receipts(("pending", "failed"))
                    why = escape((rows[0].get("error") or "")[:200]) if rows else ""
                    await _notify_admins(f"🧾 {left} чек(ов) всё ещё не пробиты.\n{why}\n/nalog")
        except Exception:
            logger.exception("npd: повтор чеков упал")
