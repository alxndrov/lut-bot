"""Настройки для локального запуска тестов через pytest.

Тесты не ходят в Telegram (везде FakeBot), но aiogram проверяет формат
токена при создании Bot(). Подставляем фиктивные токены ДО загрузки config,
чтобы тесты не зависели от содержимого локального .env.
На сервере тесты гоняются через unittest — этот файл там не используется.
"""
import os

_DUMMY = "123456789:AAtestTOKENtestTOKENtestTOKENtest12"
os.environ["BOT_TOKEN"] = _DUMMY
os.environ["WAITLIST_BOT_TOKEN"] = _DUMMY
os.environ.setdefault("GOOGLE_SHEET_FINANCE_TAB", "cashflow")
