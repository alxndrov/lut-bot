import asyncio
import os
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

import config
import database as db
from handlers import start, catalog, payment, admin, delivery, waitlist_handler, feedback, brief_handler, channel_access
from handlers import funnel_handler, bonus_handler, order_actions, order_widget, catalog_widget, client_order_widget, product_widget, expenses, cdek_account, finance, debug_cmd, consumables
from handlers import support, support_admin, logo, reviews, pending_nudge, operations_menu, refunds, shop_reviews, sellers_info, forget, course_gift, course_files, manual_sale
from handlers import course, course_offer, course_admin, course_orders, course_invoice, npd_admin, friend_order, course_survey, blogger_order
from handlers import pay_router, payments_admin
from handlers.prodamus_webhook import create_app as create_webhook_app
from services.daily_report import daily_report_loop
from services.finance_reports import report_loop
from services.pending_report import pending_report_loop
from services.finance_sheet import finance_sync_loop
from services.metrics_sheet import metrics_sync_loop
from services.leads_sheet import leads_sync_loop
from services.funnel import funnel_worker
from services.review_push import review_push_worker
from services.cdek_tracker import cdek_tracking_worker
from services.cdek_retry import cdek_retry_worker
from services.cdek_accounts import OLD_CONTRACT, CURRENT_CONTRACT
from services.message_log import MessageLogMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def _poll(dp: Dispatcher, bot: Bot, **kw) -> None:
    """Поллинг, который переживает сбой Telegram на старте: aiogram сначала
    спрашивает get_me и на 502 падает — а с ним весь процесс, и вместе с
    ботом ложатся мини-апп, ссылки на оплату и вебхуки касс."""
    while True:
        try:
            await bot.me()
            break
        except Exception as e:
            logging.warning(f"Telegram недоступен ({e}) — повтор через 15 с")
            await asyncio.sleep(15)
    await dp.start_polling(bot, **kw)


async def _try(what: str, call) -> None:
    try:
        # Не дольше 20 с: пока ждём Telegram, вебхук-сервер ещё не поднят
        await asyncio.wait_for(call, 20)
    except Exception as e:
        logging.warning(f"Старт: {what} не задано — {e}")


async def main():
    await db.init_db(existing_cdek_contract=OLD_CONTRACT or CURRENT_CONTRACT)
    from services import course_outreach
    await course_outreach.init()

    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Каждое входящее сообщение — в лог, до и после хендлеров
    dp.update.outer_middleware(MessageLogMiddleware("клиент"))
    dp.callback_query.middleware(start.PolicyGate())   # новое согласие после смены оператора
    # Нажатия в каталоге-виджете (Bot API 10.4) — aiogram их не знает
    dp.update.outer_middleware(catalog_widget.WidgetQueryMiddleware())

    # Регистрируем роутеры
    dp.include_router(course_files.router)  # /coursefiles — файлы курса от админа (раньше всех)
    dp.include_router(admin.router)   # Сначала админ, чтобы перехватывал FSM-состояния
    dp.include_router(start.router)
    dp.include_router(forget.router)        # /forget — удалить свои данные
    dp.include_router(course.router)        # кодовое слово — раньше поддержки
    dp.include_router(course_offer.router)  # личная скидка на курс (?start=offer_…)
    dp.include_router(friend_order.router)  # заказ для друга — только админам
    dp.include_router(blogger_order.router)  # бесплатный заказ блогера
    dp.include_router(course_invoice.router)  # счёт за курс — только админам
    dp.include_router(catalog.router)
    dp.include_router(catalog_widget.router)   # /catalogwidget — кому каталог виджетом
    dp.include_router(client_order_widget.router)   # /mywidget — пробник «Мой заказ»
    dp.include_router(product_widget.router)   # /productwidget — конструктор физтовара
    dp.include_router(shop_reviews.router)  # «Отзывы покупателей» в каталоге
    dp.include_router(sellers_info.router)  # «ℹ️ Продавцы и условия», /info
    dp.include_router(brief_handler.router)
    dp.include_router(delivery.router)      # before payment!
    dp.include_router(waitlist_handler.router)
    dp.include_router(payment.router)
    dp.include_router(pay_router.router)     # «У меня зарубежная карта»
    dp.include_router(feedback.router)
    dp.include_router(support.router)
    dp.include_router(pending_nudge.router)
    dp.include_router(channel_access.router)
    dp.include_router(funnel_handler.router)
    dp.include_router(bonus_handler.router)
    # Последним: ловит ответ на просьбу об отзыве — всё, что не разобрали выше
    dp.include_router(course_survey.router)   # кнопки опроса покупателей курса
    dp.include_router(support.review_router)

    commands = [
        BotCommand(command="start",       description="🏠 В начало"),
        BotCommand(command="catalog",     description="📦 Каталог товаров"),
        BotCommand(command="course",      description="🎬 Курс"),
        BotCommand(command="mypurchases", description="🧾 Мои покупки"),
        BotCommand(command="feedback",    description="💬 Оставить отзыв"),
        BotCommand(command="help",        description="🆘 Поддержка"),
        BotCommand(command="info",        description="ℹ️ Продавцы и условия"),
        BotCommand(command="forget",      description="🗑 Удалить мои данные"),
    ]
    # Сбой Telegram (502) на старте не должен ронять бота: без меню команд
    # жить можно, без вебхуков касс — нет
    await _try("меню команд", bot.set_my_commands(commands))
    await _try("команды админов", course.set_admin_commands(bot, commands))

    asyncio.create_task(finance_sync_loop())
    asyncio.create_task(metrics_sync_loop())
    asyncio.create_task(leads_sync_loop())   # «Миша Стор база заинтересованных»
    asyncio.create_task(course_outreach.sync_loop())
    asyncio.create_task(daily_report_loop(bot))
    asyncio.create_task(funnel_worker(bot))
    asyncio.create_task(review_push_worker(bot))
    asyncio.create_task(cdek_tracking_worker(bot))
    asyncio.create_task(cdek_retry_worker(bot))
    asyncio.create_task(course_orders.reminder_loop())
    from handlers import course_open
    asyncio.create_task(course_open.open_notice_loop(bot))   # «Уроки открыты» 00:00 и 10:00 МСК
    from services import npd_receipts
    asyncio.create_task(npd_receipts.retry_loop(bot))

    # Запускаем aiohttp-сервер для приёма вебхуков от Prodamus
    webhook_app = create_webhook_app(bot)
    try:
        bot_username = (await bot.get_me(request_timeout=15)).username or ""
    except Exception as e:
        # Без ника ссылки «вернуться в бот» после оплаты пустые — берём известный
        logging.warning(f"get_me не удался: {e}")
        bot_username = os.getenv("BOT_USERNAME", "malimabibot")
    course.setup_web(webhook_app, bot_username)
    pay_router.setup_web(webhook_app, bot_username)
    asyncio.create_task(pay_router.pay_worker(bot))
    # Кнопка приложения курса: до анонса — только админам
    await _try("кнопка приложения", course.sync_menu_button(bot))
    runner = web.AppRunner(webhook_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PRODAMUS_WEBHOOK_PORT)
    await site.start()
    logging.info(f"Webhook-сервер запущен на порту {config.PRODAMUS_WEBHOOK_PORT}")

    # Отдельный поллинг админского бота (malimadmins) — для кнопки «Взял заказ»
    if config.WAITLIST_BOT_TOKEN:
        admin_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
        admin_dp = Dispatcher(storage=MemoryStorage())
        admin_dp.update.outer_middleware(MessageLogMiddleware("админ"))
        # Не админ — дальше не идёт (обработчики проверяют и сами)
        from services.admin_guard import AdminOnlyMiddleware
        admin_dp.update.outer_middleware(AdminOnlyMiddleware())
        # Нажатия в виджете /worders (Bot API 10.4) — aiogram их не знает
        admin_dp.update.outer_middleware(order_widget.WidgetQueryMiddleware())
        # Первым: ловит ответы на карточки заказов по курсу и ввод времени созвона
        admin_dp.include_router(course_orders.router)
        admin_dp.include_router(npd_admin.router)   # /nalog — чеки «Мой налог»
        admin_dp.include_router(operations_menu.router)
        admin_dp.include_router(order_actions.router)
        admin_dp.include_router(order_widget.router)   # /worders — заказы виджетом
        admin_dp.include_router(refunds.router)
        admin_dp.include_router(course_admin.router)
        admin_dp.include_router(course_gift.router)   # /gift — курс в подарок
        admin_dp.include_router(blogger_order.admin_router)   # /blogger — товар блогеру
        admin_dp.include_router(course_offer.admin_router)   # /offer — личное предложение на курс
        admin_dp.include_router(course_survey.admin_router)  # /survey — опрос покупателей курса
        admin_dp.include_router(manual_sale.router)    # /sale — продажа вне бота
        admin_dp.include_router(expenses.router)
        admin_dp.include_router(cdek_account.router)
        admin_dp.include_router(finance.router)
        admin_dp.include_router(payments_admin.router)
        admin_dp.include_router(debug_cmd.router)
        admin_dp.include_router(consumables.router)
        admin_dp.include_router(reviews.router)
        admin_dp.include_router(support_admin.router)
        # Последним: ловит присланный файл, который не забрал никто
        admin_dp.include_router(logo.router)
        asyncio.create_task(
            _poll(admin_dp, admin_bot,
                                   allowed_updates=["callback_query", "message", "widget_query"])
        )
        asyncio.create_task(report_loop(admin_bot))
        asyncio.create_task(pending_report_loop(admin_bot))
        asyncio.create_task(refunds.receipt_reminder_loop(admin_bot))
        from services import settle
        asyncio.create_task(settle.remind_loop())   # 1-го и 16-го — пора рассчитаться
        # Догоняем кнопки у заказов, созданных до появления новых статусов
        asyncio.create_task(order_actions.refresh_open_orders(admin_bot))
        # Команда видна в меню бота
        try:
            await admin_bot.set_my_commands([
                BotCommand(command="orders", description="📋 Заказы в работе"),
                BotCommand(command="sentorders", description="📦 Отправленные заказы"),
                BotCommand(command="courseorders", description="🎓 Заказы по курсу"),
                BotCommand(command="sale", description="🧾 Продажа вне бота (перевод)"),
                BotCommand(command="gift", description="🎁 Курс в подарок"),
                BotCommand(command="blogger", description="🎬 Бесплатный товар блогеру"),
                BotCommand(command="offers", description="🤍 Личные предложения на курс"),
                BotCommand(command="nalog", description="🧾 Чеки «Мой налог» (ЮKassa)"),
                BotCommand(command="finance", description="💳 Финансы"),
                BotCommand(command="payments", description="🔀 Оплата: Prodamus / ЮKassa"),
                BotCommand(command="stock", description="📦 Расходники"),
                BotCommand(command="reviews", description="⭐️ Отзывы клиентов"),
                # Остальные команды работают, но в меню не выносятся, чтобы
                # не разрасталось: /expenses, /cdek, /expense, /refresh, /debug
            ])
        except Exception as e:
            logging.warning(f"Не удалось задать команды админского бота: {e}")
        logging.info("Поллинг админского бота (malimadmins) запущен.")

    logging.info("Бот запущен.")
    await _poll(dp, bot, allowed_updates=["message", "callback_query",
                                                  "chat_join_request", "pre_checkout_query",
                                                  "shipping_query", "widget_query"])


if __name__ == "__main__":
    asyncio.run(main())
