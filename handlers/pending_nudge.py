"""
Вопрос клиенту, который заполнил заказ, но не оплатил (основной бот).

Админ жмёт кнопку в «🛒 Незавершённые заказы» (handlers/admin.py), клиенту
приходит тёплое сообщение: всё сохранилось, можно продолжить с того же
места или рассказать, что помешало. Продолжение — сразу к оплате:
ответы, пункт выдачи и сумма уже лежат в pending_deliveries, заново
ничего заполнять не нужно.

Ответы клиента уходят админам в malimadmins (см. handlers/support.py):
«Продолжить» — уведомлением, вопрос и ответ текстом — как сообщение в поддержку.
"""
import json
import logging
from html import escape

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
from handlers.support import SupportState, notify_admins_text, pending_line

router = Router()
logger = logging.getLogger(__name__)

def nudge_text(product_name: str) -> str:
    return (
        "Привет! 👋\n"
        f"Вы начали оформлять заказ на «{escape(product_name)}», но не дошли до оплаты. "
        "Всё сохранилось: ответы, пункт выдачи и сумма.\n\n"
        "Если что-то помешало или остались вопросы, расскажите, нам правда важно. "
        "А если просто отвлеклись, можно продолжить с того же места 🙂"
    )


def nudge_keyboard(product_id: int) -> InlineKeyboardMarkup:
    # Быстрых ответов («дорого», «передумал») нет намеренно: пусть человек
    # напишет своими словами — текст тоже доходит до админов
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Продолжить оформление", callback_data=f"pn:go:{product_id}")],
        [InlineKeyboardButton(text="💬 Есть вопрос", callback_data=f"pn:ask:{product_id}")],
    ])


async def _load(callback: CallbackQuery, product_id: int) -> dict | None:
    """Незавершённый заказ этого клиента или None (уже оплачен/удалён)."""
    order = await db.get_pending_order(callback.from_user.id, product_id)
    if not order:
        await callback.answer()
        await callback.message.answer(
            "Этот заказ уже оформлен или больше не актуален. "
            "Новый можно собрать в каталоге 🙂",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📦 Каталог", callback_data="catalog")]]))
        return None
    order["product_name"] = (await db.get_product(product_id) or {}).get("name") or "заказ"
    return order


def _who(callback: CallbackQuery) -> str:
    u = callback.from_user
    username = f"@{u.username}" if u.username else f"id:{u.id}"
    return f"👤 {escape(u.first_name or '—')} {username} (id:{u.id})"


async def _pay_button(order: dict) -> tuple[int, InlineKeyboardMarkup | None]:
    """Свежая ссылка на оплату по сохранённому заказу — как «📨 Отправить клиенту»."""
    from handlers.admin import _pending_amount, _pending_payment_name
    from services.prodamus import build_payment_url
    product = await db.get_product(order["product_id"])
    amount = _pending_amount(order, product)
    if not amount:
        return 0, None
    url = build_payment_url(
        shop_url=config.PRODAMUS_SHOP_URL_PHYSICAL,
        product_name=await _pending_payment_name(order, product),
        price=amount, user_id=order["user_id"], product_id=order["product_id"],
        order_type="p", secret=config.PRODAMUS_SECRET_PHYSICAL,
        notification_url=config.PRODAMUS_WEBHOOK_URL_PHYSICAL,
    )
    return amount, InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"💳 Оплатить {amount} ₽", url=url)]])


def _summary(order: dict) -> str:
    """Что клиент уже выбрал — чтобы видел, за что платит."""
    lines = [f"🛒 <b>Ваш заказ: {escape(order['product_name'])}</b>"]
    try:
        rounds = json.loads(order.get("survey_json") or "[]")
    except Exception:
        rounds = []
    multi = len(rounds) > 1
    for ri, answers in enumerate(rounds, 1):
        if multi:
            lines.append(f"\n<b>Позиция {ri}</b>")
        for a in answers:
            ans = a.get("text") or ("фото" if a.get("photo") else ("файл" if a.get("doc") else "—"))
            q = (a.get("q") or "").split("\n")[0]
            lines.append(f"• {escape(q)} — {escape(str(ans))}")
    if order.get("delivery_str"):
        lines.append(f"\n🚚 {escape(order['delivery_str'])}")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("pn:go:"))
async def cb_continue(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[2])
    order = await _load(callback, product_id)
    if not order:
        return
    amount, kb = await _pay_button(order)
    await callback.answer()
    if not kb:
        await callback.message.answer(
            "Не получилось собрать ссылку на оплату — я передал команде, вам напишут.")
        await notify_admins_text(
            f"⚠️ <b>Клиент хочет продолжить заказ, но ссылку не собрать</b>\n"
            f"{_who(callback)}\n{pending_line(order)}\nУ заказа не посчитана сумма.",
            callback.from_user.id)
        return
    await callback.message.answer(
        _summary(order) + f"\n\n💵 Итого: <b>{amount} ₽</b>\n"
        "Заново ничего заполнять не нужно — осталось только оплатить.",
        parse_mode="HTML", reply_markup=kb)
    await notify_admins_text(
        f"▶️ <b>Клиент продолжил оформление</b>\n{_who(callback)}\n"
        f"{pending_line(order)}\nСсылка на оплату отправлена.",
        callback.from_user.id)


@router.callback_query(F.data.startswith("pn:ask:"))
async def cb_ask(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[2])
    order = await _load(callback, product_id)
    if not order:
        return
    await state.set_state(SupportState.waiting_text)
    await state.update_data(pending_pid=product_id)
    await callback.answer()
    await callback.message.answer(
        "Напишите вопрос одним сообщением — я передам команде, ответят прямо здесь.\n\n"
        "Чтобы отменить — /cancel.")
