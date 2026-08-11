import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

import config
import database as db
from handlers import start, catalog, payment, admin, delivery, waitlist_handler, feedback, brief_handler, channel_access
from handlers import funnel_handler, bonus_handler, order_actions, expenses, cdek_account
from handlers.prodamus_webhook import create_app as create_webhook_app
from services.daily_report import daily_report_loop, monthly_report_loop
from services.funnel import funnel_worker
from services.review_push import review_push_worker
from services.cdek_tracker import cdek_tracking_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main():
    await db.init_db()

    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Регистрируем роутеры
    dp.include_router(admin.router)   # Сначала админ, чтобы перехватывал FSM-состояния
    dp.include_router(start.router)
    dp.include_router(catalog.router)
    dp.include_router(brief_handler.router)
    dp.include_router(delivery.router)      # before payment!
    dp.include_router(waitlist_handler.router)
    dp.include_router(payment.router)
    dp.include_router(feedback.router)
    dp.include_router(channel_access.router)
    dp.include_router(funnel_handler.router)
    dp.include_router(bonus_handler.router)

    await bot.set_my_commands([
        BotCommand(command="start",       description="🏠 В начало"),
        BotCommand(command="catalog",     description="📦 Каталог товаров"),
        BotCommand(command="mypurchases", description="🧾 Мои покупки"),
        BotCommand(command="feedback",    description="💬 Оставить отзыв"),
    ])

    asyncio.create_task(daily_report_loop(bot))
    asyncio.create_task(monthly_report_loop(bot))
    asyncio.create_task(funnel_worker(bot))
    asyncio.create_task(review_push_worker(bot))
    asyncio.create_task(cdek_tracking_worker(bot))

    # Запускаем aiohttp-сервер для приёма вебхуков от Prodamus
    webhook_app = create_webhook_app(bot)
    runner = web.AppRunner(webhook_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PRODAMUS_WEBHOOK_PORT)
    await site.start()
    logging.info(f"Webhook-сервер запущен на порту {config.PRODAMUS_WEBHOOK_PORT}")

    # Отдельный поллинг админского бота (malimadmins) — для кнопки «Взял заказ»
    if config.WAITLIST_BOT_TOKEN:
        admin_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
        admin_dp = Dispatcher(storage=MemoryStorage())
        admin_dp.include_router(order_actions.router)
        admin_dp.include_router(expenses.router)
        admin_dp.include_router(cdek_account.router)
        asyncio.create_task(
            admin_dp.start_polling(admin_bot,
                                   allowed_updates=["callback_query", "message"])
        )
        # Догоняем кнопки у заказов, созданных до появления новых статусов
        asyncio.create_task(order_actions.refresh_open_orders(admin_bot))
        # Команда видна в меню бота
        try:
            await admin_bot.set_my_commands([
                BotCommand(command="myorders", description="Мои заказы"),
                BotCommand(command="allorders", description="Все заказы и статусы"),
                BotCommand(command="sentorders", description="Отправленные заказы"),
                BotCommand(command="refresh", description="Обновить карточки заказов"),
                BotCommand(command="expense", description="Внести расход"),
                BotCommand(command="expenses", description="Расходы за месяц"),
                BotCommand(command="cdek", description="Счёт СДЭК: остаток и траты"),
            ])
        except Exception as e:
            logging.warning(f"Не удалось задать команды админского бота: {e}")
        logging.info("Поллинг админского бота (malimadmins) запущен.")

    logging.info("Бот запущен.")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query",
                                                  "chat_join_request", "pre_checkout_query",
                                                  "shipping_query"])


if __name__ == "__main__":
    asyncio.run(main())
