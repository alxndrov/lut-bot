"""/sale в malimadmins — продажа вне бота: договорились в личке, оплата
переводом (например, от юрлица по счёту). Любой товар каталога; у курса —
ещё и вариант (съёмка, монтаж, пакет, VIP).

Шаги: товар (у курса — вариант) → сумма → покупатель (физлицо или юрлицо/ИП с ИНН и
названием) → кому пришли деньги (Мише или Дане) → чек (бот пробивает сам
или уже пробит) → кому открыть курс (ник ученика или открытая ссылка) →
подтверждение. Кому открыть — только у курса: остальные товары передают сами.

Что делает бот: записывает покупку b2b-<id> без комиссии (database.
add_manual_sale; wallet — у кого деньги, для взаиморасчёта), у курса выдаёт
ссылку на доступ (handlers/course_gift.py), по желанию пробивает
чек в «Мой налог» того, кому пришли деньги (для юрлица — с его ИНН, налог
6%), и присылает ссылку на чек — переслать клиенту.
"""
import logging
import re
import secrets
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import database as db
from handlers import course_gift
from services import course as svc
from services import npd, npd_receipts

router = Router()   # админский бот
router.message.filter(F.from_user.id.in_(config.ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(config.ADMIN_IDS))
logger = logging.getLogger(__name__)

WALLETS = {"misha": "Мише", "danya": "Дане (Альфа)"}


def _rub(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".00", "") + " ₽"


class SaleForm(StatesGroup):
    amount = State()
    inn = State()
    name = State()
    recipient = State()


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d)
                                                  for t, d in row] for row in rows])


CANCEL = ("Отмена", "msale:cancel")


@router.message(Command("sale"))
async def cmd_sale(message: Message, state: FSMContext):
    await state.clear()
    products = await db.get_catalog_products(include_hidden=True)
    if not products:
        await message.answer("В каталоге нет товаров.")
        return
    await message.answer(
        "🧾 <b>Продажа вне бота</b> — оплата переводом (например, от юрлица).\n\nЧто купили?",
        parse_mode="HTML",
        reply_markup=_kb(*[[(p["name"], f"msale:prod:{p['id']}")] for p in products], [CANCEL]))


@router.callback_query(F.data == "msale:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.edit_reply_markup(reply_markup=None)


async def _ask_amount(callback: CallbackQuery, state: FSMContext, title: str, price: int, **data):
    await state.set_state(SaleForm.amount)
    await state.update_data(**data)
    await callback.answer()
    await callback.message.edit_text(
        f"<b>{escape(title)}</b>\n\nСколько заплатили? Нажми цену или напиши сумму в рублях.",
        parse_mode="HTML", reply_markup=_kb([(f"{price} ₽", f"msale:amt:{price}")], [CANCEL]))


@router.callback_query(F.data.startswith("msale:prod:"))
async def cb_product(callback: CallbackQuery, state: FSMContext):
    product = await db.get_product(int(callback.data.split(":")[2]))
    if not product:
        await callback.answer("Нет такого товара", show_alert=True)
        return
    options = await db.get_course_options(product["id"])
    if options:   # курс — сначала вариант
        await callback.answer()
        await callback.message.edit_text(
            f"<b>{escape(product['name'])}</b>\n\nКакой вариант?", parse_mode="HTML",
            reply_markup=_kb(*[[(f"{o['title']} · {svc.price_for(o)} ₽", f"msale:opt:{o['id']}")]
                               for o in options], [CANCEL]))
        return
    price = await db.get_effective_price(product)
    await _ask_amount(callback, state, product["name"], price, product_id=product["id"],
                      option_id=None)


@router.callback_query(F.data.startswith("msale:opt:"))
async def cb_option(callback: CallbackQuery, state: FSMContext):
    option = await db.get_course_option(int(callback.data.split(":")[2]))
    if not option:
        await callback.answer("Нет такого варианта", show_alert=True)
        return
    await _ask_amount(callback, state, option["title"], svc.price_for(option),
                      product_id=option["product_id"], option_id=option["id"])


async def _ask_kind(message: Message, state: FSMContext, amount: float):
    await state.update_data(amount=amount)
    await state.set_state(None)
    await message.answer(f"Сумма: <b>{_rub(amount)}</b>\n\nКто платил?",
                         parse_mode="HTML",
                         reply_markup=_kb([("👤 Физлицо", "msale:kind:person"),
                                           ("🏢 Юрлицо или ИП", "msale:kind:legal")], [CANCEL]))


@router.callback_query(F.data.startswith("msale:amt:"))
async def cb_amount(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await _ask_kind(callback.message, state, float(callback.data.split(":")[2]))


@router.message(StateFilter(SaleForm.amount), F.text)
async def msg_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(" ", "").replace(",", ".").replace("₽", ""))
    except ValueError:
        amount = 0
    if amount <= 0:
        await message.answer("Не понял сумму. Пример: 4990")
        return
    await _ask_kind(message, state, amount)


@router.callback_query(F.data.startswith("msale:kind:"))
async def cb_kind(callback: CallbackQuery, state: FSMContext):
    kind = callback.data.split(":")[2]
    await state.update_data(buyer_kind=kind)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    if kind == "legal":
        await state.set_state(SaleForm.inn)
        await callback.message.answer("ИНН покупателя (10 цифр у юрлица, 12 у ИП) — из платёжки.")
    else:
        await _ask_wallet(callback.message)


@router.message(StateFilter(SaleForm.inn), F.text)
async def msg_inn(message: Message, state: FSMContext):
    inn = re.sub(r"\D", "", message.text)
    if len(inn) not in (10, 12):
        await message.answer("ИНН — 10 или 12 цифр. Ещё раз?")
        return
    await state.update_data(buyer_inn=inn)
    await state.set_state(SaleForm.name)
    await message.answer("Название покупателя, как в платёжке: ООО «Ромашка» или ИП Иванов И. И.")


@router.message(StateFilter(SaleForm.name), F.text)
async def msg_name(message: Message, state: FSMContext):
    await state.update_data(buyer_name=message.text.strip()[:200])
    await state.set_state(None)
    await _ask_wallet(message)


async def _ask_wallet(message: Message):
    await message.answer("Кому пришли деньги?",
                         reply_markup=_kb([("👤 Мише", "msale:wal:misha"),
                                           ("🏦 Дане (Альфа)", "msale:wal:danya")], [CANCEL]))


@router.callback_query(F.data.startswith("msale:wal:"))
async def cb_wallet(callback: CallbackQuery, state: FSMContext):
    wallet = callback.data.split(":")[2]
    await state.update_data(wallet=wallet)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    who = npd.ACCOUNTS[wallet]
    await callback.message.answer(
        f"Чек в «Мой налог». Пробивает тот, кому пришли деньги — {who}.",
        reply_markup=_kb([(f"🧾 Бот пробьёт чек ({who})", "msale:rc:bot")],
                         [("✅ Чек уже пробит вручную", "msale:rc:self")], [CANCEL]))


@router.callback_query(F.data.startswith("msale:rc:"))
async def cb_receipt(callback: CallbackQuery, state: FSMContext):
    await state.update_data(receipt=callback.data.split(":")[2])
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    if not (await state.get_data()).get("option_id"):   # не курс — доступ открывать не нужно
        await state.update_data(recipient=None)
        await _confirm(callback.message, state)
        return
    await state.set_state(SaleForm.recipient)
    await callback.message.answer(
        "Кому открыть курс? Напиши ник ученика в Telegram (@ник) — курс откроется только ему. "
        "Или «-» — дам открытую ссылку: курс получит тот, кто первым её откроет.")


@router.message(StateFilter(SaleForm.recipient), F.text)
async def msg_recipient(message: Message, state: FSMContext):
    text = message.text.strip()
    recipient = None if text in ("-", "—") else text.lstrip("@").lower()
    if recipient is not None and not re.fullmatch(r"[a-z0-9_]{4,32}", recipient):
        await message.answer("Ник в Telegram — латиница, цифры и _, от 4 символов. Или «-».")
        return
    await state.update_data(recipient=recipient)
    await _confirm(message, state)


async def _title(d: dict) -> str:
    if d.get("option_id"):
        return (await db.get_course_option(d["option_id"]))["title"]
    return (await db.get_product(d["product_id"]))["name"]


async def _confirm(message: Message, state: FSMContext):
    await state.set_state(None)
    d = await state.get_data()
    recipient = d.get("recipient")
    buyer = ("физлицо" if d["buyer_kind"] == "person"
             else f"{escape(d['buyer_name'])}, ИНН {d['buyer_inn']}")
    access = ((f"🎁 Курс откроется: {'@' + escape(recipient) if recipient else 'по открытой ссылке'}")
              if d.get("option_id") else "📦 Товар передаёте сами — бот только записывает продажу")
    await message.answer(
        "Проверь и проведи:\n\n"
        f"🛍 {escape(await _title(d))}\n"
        f"💰 {_rub(d['amount'])} — без комиссии\n"
        f"🧑‍💼 Покупатель: {buyer}\n"
        f"🏦 Деньги пришли {WALLETS[d['wallet']]}\n"
        f"🧾 Чек: {'пробьёт бот (' + npd.ACCOUNTS[d['wallet']] + ')' if d['receipt'] == 'bot' else 'уже пробит'}\n"
        + access,
        parse_mode="HTML", reply_markup=_kb([("✅ Провести", "msale:go")], [CANCEL]))


async def _issue_receipt(sale: dict) -> str:
    """Пробивает чек и возвращает ссылку на него. NpdError — наверх."""
    name = (await npd_receipts.receipt_name("c", sale["option_id"]) if sale.get("option_id")
            else await npd_receipts.receipt_name("d", sale["product_id"]))
    uuid = await npd.create_receipt(name, sale["amount"], account=sale["wallet"],
                                    client_inn=sale["buyer_inn"], client_name=sale["buyer_name"])
    url = await npd.print_url(uuid, account=sale["wallet"])
    code = f"b2b-{sale['id']}"
    if await db.add_npd_receipt(code, None, name, sale["amount"], None, sale["wallet"]):
        await db.set_npd_receipt(code, "issued", receipt_uuid=uuid)
    await db.update_manual_sale(sale["id"], receipt_uuid=uuid, receipt_url=url)
    logger.info(f"sale: чек {uuid} за {code} ({sale['wallet']})")
    return url


def _receipt_text(url: str) -> str:
    return f'🧾 <a href="{escape(url)}">Чек</a> — перешли его клиенту.'


@router.callback_query(F.data == "msale:go")
async def cb_go(callback: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if "product_id" not in d or "recipient" not in d:
        await callback.answer("Начни заново: /sale", show_alert=True)
        return
    await state.clear()
    await callback.answer("Провожу…")
    await callback.message.edit_reply_markup(reply_markup=None)
    option = await db.get_course_option(d["option_id"]) if d.get("option_id") else None
    title = await _title(d)
    sale = await db.add_manual_sale(d["product_id"], option, d["amount"], d["buyer_kind"],
                                    d.get("buyer_inn"), d.get("buyer_name"), d["wallet"],
                                    d["receipt"], callback.from_user.id)
    logger.info(f"sale: b2b-{sale['id']} {title} {d['amount']} ₽ → {d['wallet']} "
                f"(админ {callback.from_user.id})")
    lines = [f"✅ Продажа b2b-{sale['id']} проведена.", ""]
    if option:   # курс — ссылка на доступ для ученика
        token = secrets.token_urlsafe(9)
        await db.create_course_gift(token, sale["purchase_id"], 0, svc.option_sections(option),
                                    d["recipient"])
        await db.update_manual_sale(sale["id"], gift_token=token)
        lines.append("🎁 Ссылка на курс для ученика (одноразовая"
                     + (f", только для @{escape(d['recipient'])}" if d["recipient"] else "")
                     + f"):\n{await course_gift.gift_link(token)}")
    else:
        lines.append("📦 Товар передайте покупателю сами.")

    buyer = d.get("buyer_name") or "физлицо"
    from handlers.prodamus_webhook import _send_notify
    await _send_notify(None, f"{'🎓 <b>Курс оплачен переводом</b>' if option else '🛍 <b>Продажа вне бота</b>'}"
                             f"\n\n🧑‍💼 {escape(buyer)}\n🛍 {escape(title)}\n💵 {_rub(d['amount'])}\n"
                             f"🏦 Деньги у {'Миши' if d['wallet'] == 'misha' else 'Дани'}")
    from services import gsheets
    gsheets.request_finance_sync()

    kb = None
    if d["receipt"] == "bot":
        try:
            lines += ["", _receipt_text(await _issue_receipt(sale))]
        except Exception as e:
            lines += ["", f"⚠️ Чек не пробился: {escape(str(e)[:200])}"]
            kb = _kb([("🧾 Пробить чек ещё раз", f"msale:receipt:{sale['id']}")])
    await callback.message.answer("\n".join(lines), parse_mode="HTML",
                                  disable_web_page_preview=True, reply_markup=kb)


@router.callback_query(F.data.startswith("msale:receipt:"))
async def cb_retry_receipt(callback: CallbackQuery):
    sale = await db.get_manual_sale(int(callback.data.split(":")[2]))
    if not sale:
        await callback.answer("Продажа не найдена", show_alert=True)
        return
    if sale.get("receipt_url"):
        await callback.answer()
        await callback.message.answer(_receipt_text(sale["receipt_url"]), parse_mode="HTML")
        return
    await callback.answer("Пробиваю…")
    try:
        url = await _issue_receipt(sale)
    except Exception as e:
        await callback.message.answer(f"⚠️ Чек не пробился: {escape(str(e)[:200])}")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(_receipt_text(url), parse_mode="HTML")
