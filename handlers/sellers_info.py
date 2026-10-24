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
        "<b>Цифровые товары (LUT) и онлайн-курс</b>\n"
        f"Самозанятый {MISHA['name']}, ИНН {MISHA['inn']}\n\n"
        "<b>Рукоятки для микрофонов</b> (изготовление по индивидуальному заказу)\n"
        f"Самозанятый {DANYA['name']}, ИНН {DANYA['inn']}\n\n"
        "<b>Как заказать.</b> Выберите товар в каталоге — цены указаны в рублях. Для рукоятки "
        "ответьте на вопросы о модели микрофона, цвете и доставке. Перед оплатой бот покажет "
        "сумму и продавца.\n\n"
        "<b>Оплата.</b> Цифровые товары и курс — российской картой или СБП (PayAnyWay) либо "
        "картой иностранного банка в долларах (lava.top). Рукоятки — картой или СБП (Prodamus). "
        "Чек приходит после оплаты.\n\n"
        "<b>Получение.</b> Цифровые товары и доступ к курсу — в боте сразу после оплаты. "
        "Рукоятки изготавливаются за 2–4 рабочих дня и отправляются СДЭК до выбранного "
        "пункта выдачи.\n\n"
        "<b>Возврат.</b> Курс — полностью до старта, после старта — за непросмотренные уроки. "
        "Цифровые файлы после получения не возвращаются. Рукоятки сделаны по индивидуальному "
        "заказу и надлежащего качества не возвращаются; при дефекте — замена или возврат денег "
        "в течение 14 дней. Подробно — в оферте.\n\n"
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
