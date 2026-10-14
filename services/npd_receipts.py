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


async def _try_issue(bot: Bot, row: dict) -> bool:
    paid = None
    if row.get("paid_at"):
        try:
            paid = datetime.fromisoformat(row["paid_at"].replace("Z", "+00:00"))
        except ValueError:
            paid = None
    uuid = await npd.create_receipt(row["name"], row["amount"], paid)
    await db.set_npd_receipt(row["payment_id"], "issued", receipt_uuid=uuid)
    url = await npd.print_url(uuid)
    logger.info(f"npd: чек {uuid} за {row['payment_id']} — {row['amount']} ₽")
    if row.get("user_id"):
        try:
            await bot.send_message(row["user_id"], f'🧾 <a href="{url}">Чек об оплате</a> — '
                                                   f'{row["amount"]:.0f} ₽', parse_mode="HTML")
        except Exception as e:
            logger.info(f"npd: чек клиенту {row['user_id']} не отправлен: {e}")
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
    logger.info(f"npd: чек {row['receipt_uuid']} отменён (возврат {payment_id})")
    return True


async def retry_loop(bot: Bot):
    while True:
        await asyncio.sleep(RETRY_EVERY)
        try:
            done, left = await retry_failed(bot)
            if done:
                await _notify_admins(f"🧾 Пробил отложенные чеки в «Мой налог»: {done}"
                                     + (f", осталось {left}" if left else ""))
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
