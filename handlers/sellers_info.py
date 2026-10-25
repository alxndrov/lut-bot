"""«ℹ️ Продавцы и условия» (кнопка в каталоге и /info): кто продаёт, как
заказать, оплатить и вернуть, контакты и ссылки на оферту и политику.
По этому экрану PayAnyWay проверяет магазин (ФИО и ИНН продавца, цены в
рублях, контакты, порядок заказа, оплаты, доставки и возврата)."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
from services.sellers import CONTACT, DANYA, MISHA

router = Router()


def info_text() -> str:
    return (
        "ℹ️ <b>Продавцы и условия</b>\n\n"
        "<b>Цифровые товары (LUT и другие файлы) и онлайн-курсы</b>\n"
        f"Самозанятый {MISHA['name']}, ИНН {MISHA['inn']}\n\n"
        "<b>Физические товары</b> — изделия 3D-печати: рукоятки для микрофонов, кейсы и другое\n"
        f"Самозанятый {DANYA['name']}, ИНН {DANYA['inn']}\n\n"
        "<b>Как заказать.</b> Выберите товар в каталоге — цены указаны в рублях. Для изделия "
        "под заказ ответьте на вопросы о модели устройства, цвете и надписи, для товара из "
        "наличия выберите вариант; затем укажите пункт выдачи. Перед оплатой бот покажет "
        "сумму и продавца.\n\n"
        "<b>Оплата.</b> Цифровые товары и курсы — российской картой или СБП (PayAnyWay) либо "
        "картой иностранного банка в долларах (lava.top). Физические товары — картой или СБП "
        "(Prodamus). Чек приходит после оплаты.\n\n"
        "<b>Получение.</b> Цифровые товары и доступ к курсам — в боте сразу после оплаты. "
        "Изделия под заказ изготавливаются за 2–4 рабочих дня, товары из наличия отправляются "
        "в течение 3 рабочих дней; доставка — СДЭК до выбранного пункта выдачи.\n\n"
        "<b>Возврат.</b> Курс — полностью до старта, после старта — за непросмотренные уроки. "
        "Цифровые файлы после получения не возвращаются. Изделия, сделанные под заказ по "
        "вашим параметрам, надлежащего качества не возвращаются. Товары из наличия можно "
        "вернуть в течение 7 дней после получения, если сохранён их вид. При дефекте — замена "
        "или возврат денег в течение 14 дней. Подробно — в оферте.\n\n"
        f"<b>Связь:</b> {CONTACT}, поддержка в боте — /help\n"
        "Удалить свои данные из бота — /forget"
    )


def info_keyboard() -> InlineKeyboardMarkup:
    rows = []
    if config.OFFER_URL:
        rows.append([InlineKeyboardButton(text="📄 Оферта", url=config.OFFER_URL)])
    if config.PRIVACY_POLICY_URL:
        rows.append([InlineKeyboardButton(text="🔒 Политика обработки данных",
                                          url=config.PRIVACY_POLICY_URL)])
    if config.CONSENT_URL and config.CONSENT_URL != config.PRIVACY_POLICY_URL:
        rows.append([InlineKeyboardButton(text="✍️ Согласие на обработку данных",
                                          url=config.CONSENT_URL)])
    rows.append([InlineKeyboardButton(text="◀️ К каталогу", callback_data="catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("info"))
async def cmd_info(message: Message):
    await message.answer(info_text(), parse_mode="HTML", reply_markup=info_keyboard(),
                         disable_web_page_preview=True)


@router.callback_query(F.data == "info:sellers")
async def cb_info(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(info_text(), parse_mode="HTML", reply_markup=info_keyboard(),
                                  disable_web_page_preview=True)
