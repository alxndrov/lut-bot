import json
import aiosqlite
from typing import Optional

DB_PATH = "bot.db"

# Две суммы по доставке для отчётов: сколько реально ушло в СДЭК по новым
# заказам и сколько «принято за доставку» по старым, где счёт не сохранялся.
# Вторые пересчитываются формулой — см. services.payout.delivery_out.
_DELIVERY_COST_SQL = """
                      COALESCE(SUM(delivery_cost), 0) AS delivery_cost,
                      COALESCE(SUM(CASE WHEN COALESCE(delivery_cost, 0) = 0
                                        THEN delivery_amount ELSE 0 END), 0)
                          AS delivery_legacy"""


def question_photos(q: dict) -> list:
    """Список file_id фотографий вопроса (поддержка нескольких фото / альбома)."""
    pj = q.get("photos_json")
    if pj:
        try:
            v = json.loads(pj)
            if isinstance(v, list):
                return [x for x in v if x]
        except Exception:
            pass
    return [q["photo_id"]] if q.get("photo_id") else []


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                price INTEGER NOT NULL,
                file_id TEXT,
                file_name TEXT,
                photo_id TEXT,
                active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                product_id INTEGER NOT NULL,
                telegram_payment_id TEXT,
                amount INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migration: add category column if not exists
        try:
            await db.execute("ALTER TABLE products ADD COLUMN category TEXT DEFAULT 'digital'")
        except Exception:
            pass  # Column already exists
        # Migration: add instruction, video and payment link fields
        for col in [
            "ALTER TABLE products ADD COLUMN instruction_file_id TEXT",
            "ALTER TABLE products ADD COLUMN instruction_file_name TEXT",
            "ALTER TABLE products ADD COLUMN instruction_type TEXT DEFAULT 'document'",
            "ALTER TABLE products ADD COLUMN video_url TEXT",
            # Инфобиз: счётчик, динамическая цена, закрытый канал
            "ALTER TABLE products ADD COLUMN counter_visible INTEGER DEFAULT 0",
            "ALTER TABLE products ADD COLUMN price_trigger INTEGER DEFAULT NULL",
            "ALTER TABLE products ADD COLUMN price_after_trigger INTEGER DEFAULT NULL",
            "ALTER TABLE products ADD COLUMN channel_id TEXT DEFAULT NULL",
            "ALTER TABLE products ADD COLUMN channel_invite_link TEXT DEFAULT NULL",
            # Инфобиз: бонус для первых N покупателей (персональный разбор)
            "ALTER TABLE products ADD COLUMN bonus_limit INTEGER DEFAULT NULL",
            "ALTER TABLE products ADD COLUMN bonus_file_id TEXT DEFAULT NULL",   # не используется
            "ALTER TABLE products ADD COLUMN bonus_file_name TEXT DEFAULT NULL", # не используется
            "ALTER TABLE products ADD COLUMN bonus_text TEXT DEFAULT NULL",
            # Пуш с просьбой оставить отзыв
            "ALTER TABLE products ADD COLUMN review_push_text TEXT DEFAULT NULL",
            "ALTER TABLE products ADD COLUMN review_push_delay INTEGER DEFAULT NULL",
        ]:
            try:
                await db.execute(col)
            except Exception:
                pass
        # Settings table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Waitlist entries table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS waitlist_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                product_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Pending deliveries — временное хранилище адреса до завершения оплаты
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_deliveries (
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                delivery_str TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, product_id)
            )
        """)
        # Пользователи — для отслеживания запусков и аудитории
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT,
                launch_count INTEGER DEFAULT 1,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migration: source/ref tracking
        try:
            await db.execute("ALTER TABLE users ADD COLUMN ref TEXT DEFAULT NULL")
        except Exception:
            pass
        # Воронки лид-магнита
        await db.execute("""
            CREATE TABLE IF NOT EXISTS funnels (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                product_id INTEGER,
                active INTEGER DEFAULT 1
            )
        """)
        # Migration: добавляем slug — ASCII-safe ID для deep links
        try:
            await db.execute("ALTER TABLE funnels ADD COLUMN slug TEXT DEFAULT NULL")
        except Exception:
            pass
        # Проставляем slug существующим воронкам у которых его нет
        import secrets
        async with db.execute("SELECT id FROM funnels WHERE slug IS NULL") as cur:
            rows = await cur.fetchall()
        for (fid,) in rows:
            slug = "f" + secrets.token_hex(4)   # например f3a9c1d2 — 9 символов, всегда ASCII
            await db.execute("UPDATE funnels SET slug = ? WHERE id = ?", (slug, fid))
        await db.execute("""
            CREATE TABLE IF NOT EXISTS funnel_steps (
                funnel_id TEXT NOT NULL,
                step INTEGER NOT NULL,
                delay_seconds INTEGER NOT NULL,
                text TEXT NOT NULL,
                PRIMARY KEY (funnel_id, step)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS funnel_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                funnel_id TEXT NOT NULL,
                step INTEGER NOT NULL,
                send_at TIMESTAMP NOT NULL,
                sent INTEGER DEFAULT 0,
                cancelled INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migration: source tracking in funnel_queue
        try:
            await db.execute("ALTER TABLE funnel_queue ADD COLUMN source TEXT DEFAULT NULL")
        except Exception:
            pass
        # Доступы к закрытым каналам (инфобиз)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_access (
                user_id   INTEGER NOT NULL,
                channel_id TEXT NOT NULL,
                granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, channel_id)
            )
        """)
        # История расчётов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gross REAL NOT NULL,
                fee REAL NOT NULL,
                net REAL NOT NULL,
                count INTEGER NOT NULL,
                settled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Очередь пушей с просьбой оставить отзыв
        await db.execute("""
            CREATE TABLE IF NOT EXISTS review_push_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                send_at TIMESTAMP NOT NULL,
                sent INTEGER DEFAULT 0
            )
        """)
        # Бонусные разборы — первые N покупателей инфобиза
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bonus_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                product_id INTEGER NOT NULL,
                video_link TEXT,
                won_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                link_sent_at TIMESTAMP,
                UNIQUE(user_id, product_id)
            )
        """)
        # Вопросы опроса (пошаговый ТЗ) для физических товаров
        await db.execute("""
            CREATE TABLE IF NOT EXISTS product_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migration: опциональное фото к вопросу
        try:
            await db.execute("ALTER TABLE product_questions ADD COLUMN photo_id TEXT DEFAULT NULL")
        except Exception:
            pass
        # Migration: несколько фото к вопросу (альбом) — JSON-список file_id
        try:
            await db.execute("ALTER TABLE product_questions ADD COLUMN photos_json TEXT DEFAULT NULL")
        except Exception:
            pass
        # Migration: текст вопроса «добавить ещё позицию» (перезапуск опроса)
        try:
            await db.execute("ALTER TABLE products ADD COLUMN survey_repeat_text TEXT DEFAULT NULL")
        except Exception:
            pass
        # Migration: текст финального блока про доставку (спрашивается один раз в конце)
        try:
            await db.execute("ALTER TABLE products ADD COLUMN survey_delivery_text TEXT DEFAULT NULL")
        except Exception:
            pass
        # Migration: ответы опроса хранятся до оплаты — уведомление о заказе шлём после неё
        try:
            await db.execute("ALTER TABLE pending_deliveries ADD COLUMN survey_json TEXT DEFAULT NULL")
        except Exception:
            pass
        # Migration: сумма к оплате — нужна, чтобы провести заказ вручную,
        # если вебхук об оплате не дошёл (перезапуск бота, сбой сети)
        try:
            await db.execute("ALTER TABLE pending_deliveries ADD COLUMN amount INTEGER DEFAULT 0")
        except Exception:
            pass
        # Migration: габариты и вес посылки на каждый товар (NULL — берём
        # значения по умолчанию из .env)
        for col in ("pkg_weight INTEGER DEFAULT NULL",
                    "pkg_length INTEGER DEFAULT NULL",
                    "pkg_width INTEGER DEFAULT NULL",
                    "pkg_height INTEGER DEFAULT NULL"):
            try:
                await db.execute(f"ALTER TABLE products ADD COLUMN {col}")
            except Exception:
                pass
        # Migration: доставка внутри суммы заказа. Это транзитные деньги —
        # они уходят СДЭК, поэтому в выручку и в дележ их включать нельзя.
        try:
            await db.execute("ALTER TABLE purchases ADD COLUMN delivery_amount INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE pending_deliveries ADD COLUMN delivery_amount INTEGER DEFAULT 0")
        except Exception:
            pass
        # Migration: сколько из доставки реально уходит в СДЭК (тариф +
        # страховка + НДС). У заказов до августа 2026 колонка пустая —
        # отчёты для них выводят транзит по старой формуле
        for table in ("purchases", "pending_deliveries"):
            try:
                await db.execute(
                    f"ALTER TABLE {table} ADD COLUMN delivery_cost REAL DEFAULT 0")
            except Exception:
                pass
        # Migration: получатель для накладной СДЭК — ФИО и телефон
        for col in ("recipient_name TEXT DEFAULT NULL",
                    "recipient_phone TEXT DEFAULT NULL",
                    "pvz_code TEXT DEFAULT NULL"):
            try:
                await db.execute(f"ALTER TABLE pending_deliveries ADD COLUMN {col}")
            except Exception:
                pass
        # Расходы: пластик, упаковка и прочее. Вычитаются из чистой прибыли
        # до дележа — материалы оплачиваются из общего котла
        await db.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                spent_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                category   TEXT NOT NULL,
                amount     REAL NOT NULL,
                comment    TEXT,
                user_id    INTEGER,
                user_name  TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS expense_categories (
                name     TEXT PRIMARY KEY,
                position INTEGER DEFAULT 0
            )
        """)
        for pos, name in enumerate(("Пластик", "Упаковка", "Поп-фильтр")):
            await db.execute(
                "INSERT OR IGNORE INTO expense_categories (name, position) VALUES (?, ?)",
                (name, pos),
            )
        # Оплаченные счета СДЭК. СДЭК работает постоплатой: сначала возит,
        # потом выставляет счёт — его и записываем. Стоимость самих
        # накладных сюда НЕ пишется, она считается из заказов.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cdek_payments (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                paid_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                amount    REAL NOT NULL,
                comment   TEXT,
                user_id   INTEGER,
                user_name TEXT
            )
        """)
        # Фактически заплаченный НПД (налог самозанятого) — в отличие от
        # accrued-оценки в «Взаиморасчёте», это реальные платежи в «Мой
        # налог», нужны для кассового остатка (см. handlers/finance.py).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS npd_payments (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                paid_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                amount    REAL NOT NULL,
                comment   TEXT,
                user_id   INTEGER,
                user_name TEXT
            )
        """)
        # Фактические выплаты Мише/Дане с общего счёта — без этого «Кассовый
        # остаток» не знает, что часть уже посчитанной прибыли реально
        # забрали, и посчитает остаток завышенным.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payouts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                paid_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                recipient TEXT NOT NULL,
                amount    REAL NOT NULL,
                comment   TEXT,
                user_id   INTEGER,
                user_name TEXT
            )
        """)
        # Таблица первой версии счёта — она была под предоплату, а СДЭК
        # так не работает. Пустую убираем, чтобы не путалась; если в ней
        # что-то есть, оставляем как есть и разбираемся руками.
        try:
            async with db.execute("SELECT COUNT(*) FROM cdek_account") as cur:
                if (await cur.fetchone())[0] == 0:
                    await db.execute("DROP TABLE cdek_account")
        except Exception:
            pass
        # Отметки печати по каждому исполнителю: в разделённом заказе каждый
        # отмечает свою часть, и заказ считается распечатанным, когда отметились все
        await db.execute("""
            CREATE TABLE IF NOT EXISTS order_prints (
                order_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                user_name  TEXT,
                printed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (order_id, user_id)
            )
        """)
        # Migration: какие именно позиции отмечены (",1,3,"). Пусто в старых
        # строках — значит отмечена вся своя часть заказа
        try:
            await db.execute("ALTER TABLE order_prints ADD COLUMN positions TEXT DEFAULT NULL")
        except Exception:
            pass
        # Migration: согласие на обработку персональных данных. Пока не принято,
        # каталог не показываем; deep-link payload держим до принятия.
        for col in ("policy_accepted_at TIMESTAMP DEFAULT NULL",
                    "pending_payload TEXT DEFAULT NULL"):
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except Exception:
                pass
        # Заказы (физтовары) — для кнопки «Взял заказ» и подписи исполнителя.
        # Таблица создаётся ДО миграций ниже: они добавляют ей колонки, а на
        # чистой базе добавлять было бы нечему — ALTER молча проглатывался бы
        # except'ом, и заказы остались бы без половины полей.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                product_id INTEGER,
                prodamus_order_id TEXT,
                summary TEXT,
                assignee_id INTEGER DEFAULT NULL,
                assignee_name TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Копии уведомления о заказе в чатах админов (чтобы синхронно проставлять исполнителя)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS order_messages (
                order_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL
            )
        """)
        # Migration: структурированные ответы заказа (для гибкой аналитики по цвету/текстуре/…)
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN rounds_json TEXT DEFAULT NULL")
        except Exception:
            pass
        # Migration: отметка об отправке заказа
        for col in ("shipped_at TEXT DEFAULT NULL",
                    "shipped_by_id INTEGER DEFAULT NULL",
                    "shipped_by_name TEXT DEFAULT NULL"):
            try:
                await db.execute(f"ALTER TABLE orders ADD COLUMN {col}")
            except Exception:
                pass
        # Migration: когда клиенту сообщили, что посылка приехала в ПВЗ
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN arrived_notified_at TIMESTAMP DEFAULT NULL")
        except Exception:
            pass
        # Migration: кто печатает заказ. В заказе с разными позициями печатать
        # могут оба — тогда он должен быть виден у обоих, а не только
        # у одного исполнителя. Формат: ",id,id,"
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN printer_ids TEXT DEFAULT NULL")
        except Exception:
            pass
        # Migration: кто печатает КАЖДУЮ позицию — JSON-список ников по порядку.
        # Нужен, чтобы позицию можно было передать отдельно, а не весь заказ.
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN routing_json TEXT DEFAULT NULL")
        except Exception:
            pass
        # Migration: этап «распечатал» между «взял в работу» и «отправил»
        for col in ("printed_at TIMESTAMP DEFAULT NULL",
                    "printed_by_id INTEGER DEFAULT NULL",
                    "printed_by_name TEXT DEFAULT NULL"):
            try:
                await db.execute(f"ALTER TABLE orders ADD COLUMN {col}")
            except Exception:
                pass
        # Migration: количество позиций в покупке — для отчёта «по товарам».
        # Раньше «6 шт.» означало 6 покупок, хотя в одной могло быть 2 микрофона.
        try:
            await db.execute("ALTER TABLE purchases ADD COLUMN quantity INTEGER DEFAULT 1")
        except Exception:
            pass
        else:
            # Разовый пересчёт прошлых покупок: количество позиций лежит
            # в заказе, в rounds_json — по одному кругу ответов на позицию
            async with db.execute(
                "SELECT prodamus_order_id, rounds_json FROM orders "
                "WHERE rounds_json IS NOT NULL AND prodamus_order_id IS NOT NULL"
            ) as cur:
                rows = await cur.fetchall()
            for payment_id, rounds_json in rows:
                try:
                    qty = max(1, len([r for r in json.loads(rounds_json) if r]))
                except Exception:
                    continue
                if qty > 1:
                    await db.execute(
                        "UPDATE purchases SET quantity = ? WHERE telegram_payment_id = ?",
                        (qty, payment_id),
                    )
        # Migration: получатель прямо в заказе. В pending_deliveries эти поля
        # тоже есть, но та строка удаляется сразу после оплаты — значит,
        # для отчётов и выгрузки данные надо копировать в orders.
        for col in ("recipient_name TEXT DEFAULT NULL",
                    "recipient_phone TEXT DEFAULT NULL",
                    "pvz_code TEXT DEFAULT NULL"):
            try:
                await db.execute(f"ALTER TABLE orders ADD COLUMN {col}")
            except Exception:
                pass
        # Migration: собственный номер заказа магазина (malimabi-store-001)
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN order_code TEXT DEFAULT NULL")
        except Exception:
            pass
        # Migration: номер заказа в СДЭК (uuid + трек-номер) и telegram-файл
        # наклейки ШК — чтобы повторное нажатие кнопки не гоняло СДЭК заново
        for col in ("cdek_uuid TEXT DEFAULT NULL", "cdek_number TEXT DEFAULT NULL",
                    "cdek_barcode_file_id TEXT DEFAULT NULL"):
            try:
                await db.execute(f"ALTER TABLE orders ADD COLUMN {col}")
            except Exception:
                pass
        # Migration: вопрос-«распределитель» (по ответу определяется, кто печатает)
        try:
            await db.execute("ALTER TABLE product_questions ADD COLUMN is_router INTEGER DEFAULT 0")
        except Exception:
            pass
        # Migration: распределение по цвету — «Имя: номера» (кто печатает какой цвет)
        try:
            await db.execute("ALTER TABLE products ADD COLUMN order_routing_text TEXT DEFAULT NULL")
        except Exception:
            pass
        # Migration: сообщение клиенту после оплаты (можно редактировать; {order} = номер)
        try:
            await db.execute("ALTER TABLE products ADD COLUMN post_payment_text TEXT DEFAULT NULL")
        except Exception:
            pass
        # Migration: заказ, для которого поставлен пуш отзыва. У физтоваров
        # отсчёт идёт не от оплаты, а от отметки «отправлено» — без order_id
        # нечем было бы отличить повторную постановку (переотправка после
        # отмены) от первой и пуш ушёл бы клиенту дважды.
        try:
            await db.execute("ALTER TABLE review_push_queue ADD COLUMN order_id INTEGER DEFAULT NULL")
        except Exception:
            pass
        # Migration: разные товары в одном заказе («добавить другой товар» в
        # опросе) — JSON-список product_id по каждому раунду, параллельный
        # rounds_json/survey_json. Пусто — считаем, что все раунды одного
        # товара (orders.product_id / pending_deliveries.product_id).
        for table in ("pending_deliveries", "orders"):
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN round_products_json TEXT DEFAULT NULL")
            except Exception:
                pass
        await db.commit()


# --- Products ---

async def get_all_products(active_only=True) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        condition = "WHERE active = 1" if active_only else ""
        async with db.execute(f"SELECT * FROM products {condition} ORDER BY id") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_product(product_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE id = ?", (product_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def add_product(name: str, description: str, price: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO products (name, description, price, active) VALUES (?, ?, ?, 0)",
            (name, description, price),
        )
        await db.commit()
        return cursor.lastrowid


async def set_product_file(product_id: int, file_id: str, file_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET file_id = ?, file_name = ? WHERE id = ?",
            (file_id, file_name, product_id),
        )
        await db.commit()


async def set_product_photo(product_id: int, photo_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET photo_id = ? WHERE id = ?",
            (photo_id, product_id),
        )
        await db.commit()


async def update_product_active(product_id: int, active: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET active = ? WHERE id = ?",
            (1 if active else 0, product_id),
        )
        await db.commit()


async def update_product_name(product_id: int, name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE products SET name = ? WHERE id = ?", (name, product_id))
        await db.commit()


async def update_product_description(product_id: int, description: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE products SET description = ? WHERE id = ?", (description, product_id))
        await db.commit()


async def update_product_price(product_id: int, price: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE products SET price = ? WHERE id = ?", (price, product_id))
        await db.commit()


async def delete_product(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM products WHERE id = ?", (product_id,))
        await db.execute("DELETE FROM product_questions WHERE product_id = ?", (product_id,))
        await db.commit()


# --- Вопросы опроса (пошаговый ТЗ физического товара) ---

async def get_product_questions(product_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM product_questions WHERE product_id = ? ORDER BY position, id",
            (product_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def add_product_question(product_id: int, text: str,
                               photo_ids: list | None = None) -> int:
    photo_ids = photo_ids or []
    photos_json = json.dumps(photo_ids, ensure_ascii=False) if photo_ids else None
    photo_id = photo_ids[0] if photo_ids else None
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM product_questions WHERE product_id = ?",
            (product_id,),
        ) as cur:
            pos = int((await cur.fetchone())[0])
        cursor = await db.execute(
            "INSERT INTO product_questions (product_id, position, text, photo_id, photos_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (product_id, pos, text, photo_id, photos_json),
        )
        await db.commit()
        return cursor.lastrowid


async def get_product_question(question_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM product_questions WHERE id = ?", (question_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_product_question(question_id: int, text: str,
                                  photo_ids: list | None = None, change_photos: bool = False):
    """Обновляет текст вопроса. Если change_photos=True — также меняет фото
    (пустой список очистит фото)."""
    async with aiosqlite.connect(DB_PATH) as db:
        if change_photos:
            photo_ids = photo_ids or []
            photos_json = json.dumps(photo_ids, ensure_ascii=False) if photo_ids else None
            photo_id = photo_ids[0] if photo_ids else None
            await db.execute(
                "UPDATE product_questions SET text = ?, photo_id = ?, photos_json = ? WHERE id = ?",
                (text, photo_id, photos_json, question_id),
            )
        else:
            await db.execute(
                "UPDATE product_questions SET text = ? WHERE id = ?", (text, question_id)
            )
        await db.commit()


async def delete_product_question(question_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM product_questions WHERE id = ?", (question_id,))
        await db.commit()


async def toggle_router_question(product_id: int, question_id: int) -> bool:
    """Помечает вопрос как «распределитель» (по его ответу определяется исполнитель).
    Такой вопрос может быть только один на товар. Возвращает новое состояние."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_router FROM product_questions WHERE id = ?", (question_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        new_state = 0 if row[0] else 1
        if new_state:
            await db.execute(
                "UPDATE product_questions SET is_router = 0 WHERE product_id = ?", (product_id,)
            )
        await db.execute(
            "UPDATE product_questions SET is_router = ? WHERE id = ?", (new_state, question_id)
        )
        await db.commit()
        return bool(new_state)


async def set_order_routing_text(product_id: int, text: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET order_routing_text = ? WHERE id = ?", (text, product_id)
        )
        await db.commit()


async def set_post_payment_text(product_id: int, text: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET post_payment_text = ? WHERE id = ?", (text, product_id)
        )
        await db.commit()


async def move_product_question(question_id: int, direction: int):
    """Меняет местами вопрос с соседом. direction = -1 (вверх) или +1 (вниз)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT product_id, position FROM product_questions WHERE id = ?", (question_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return
        product_id, pos = row["product_id"], row["position"]
        if direction < 0:
            order = "position < ? ORDER BY position DESC"
        else:
            order = "position > ? ORDER BY position ASC"
        async with db.execute(
            f"SELECT id, position FROM product_questions WHERE product_id = ? AND {order} LIMIT 1",
            (product_id, pos),
        ) as cur:
            neighbor = await cur.fetchone()
        if not neighbor:
            return
        await db.execute("UPDATE product_questions SET position = ? WHERE id = ?",
                         (neighbor["position"], question_id))
        await db.execute("UPDATE product_questions SET position = ? WHERE id = ?",
                         (pos, neighbor["id"]))
        await db.commit()


async def set_product_instruction(product_id: int, file_id: str, file_name: str, file_type: str = "document"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET instruction_file_id = ?, instruction_file_name = ?, instruction_type = ? WHERE id = ?",
            (file_id, file_name, file_type, product_id),
        )
        await db.commit()


async def set_product_video_url(product_id: int, url: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE products SET video_url = ? WHERE id = ?", (url, product_id))
        await db.commit()



async def set_survey_repeat_text(product_id: int, text: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET survey_repeat_text = ? WHERE id = ?", (text, product_id)
        )
        await db.commit()


async def set_survey_delivery_text(product_id: int, text: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET survey_delivery_text = ? WHERE id = ?", (text, product_id)
        )
        await db.commit()


async def set_product_package(product_id: int, weight: int | None, length: int | None,
                              width: int | None, height: int | None):
    """Габариты посылки для товара. None — использовать значения из .env."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE products
               SET pkg_weight = ?, pkg_length = ?, pkg_width = ?, pkg_height = ?
               WHERE id = ?""",
            (weight, length, width, height, product_id),
        )
        await db.commit()


def product_package(product: dict | None) -> dict:
    """Габариты товара с откатом к общим значениям из .env."""
    import config
    product = product or {}
    return {
        "weight": product.get("pkg_weight") or config.CDEK_PACKAGE_WEIGHT,
        "length": product.get("pkg_length") or config.CDEK_PACKAGE_LENGTH,
        "width": product.get("pkg_width") or config.CDEK_PACKAGE_WIDTH,
        "height": product.get("pkg_height") or config.CDEK_PACKAGE_HEIGHT,
    }


def unpack_round_products(raw: str | None, rounds: list, fallback_product_id: int) -> list[int]:
    """Товар каждого раунда: parallel-массив к rounds из round_products_json.

    Пусто/битый JSON/не совпадает длиной с rounds — считаем все раунды
    товаром fallback_product_id (так лежат заказы до этой миграции и
    обычные заказы одного товара, где колонку вообще не пишем).
    """
    n = len(rounds)
    if raw:
        try:
            ids = json.loads(raw)
            if isinstance(ids, list) and len(ids) == n:
                return [int(x) for x in ids]
        except Exception:
            pass
    return [fallback_product_id] * n


async def update_product_category(product_id: int, category: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE products SET category = ? WHERE id = ?", (category, product_id))
        await db.commit()


async def get_product_purchase_count(product_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM purchases WHERE product_id = ?", (product_id,)
        ) as cur:
            row = await cur.fetchone()
            return int(row[0] or 0)


async def get_effective_price(product: dict) -> int:
    """Для инфобиз-товаров возвращает актуальную цену с учётом триггера."""
    if product.get("category") != "infobiz":
        return product["price"]
    trigger = product.get("price_trigger")
    after = product.get("price_after_trigger")
    if not trigger or not after:
        return product["price"]
    count = await get_product_purchase_count(product["id"])
    return after if count >= trigger else product["price"]


async def set_infobiz_counter_visible(product_id: int, visible: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET counter_visible = ? WHERE id = ?",
            (1 if visible else 0, product_id),
        )
        await db.commit()


async def set_infobiz_price_trigger(product_id: int, trigger: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET price_trigger = ? WHERE id = ?", (trigger, product_id)
        )
        await db.commit()


async def set_infobiz_price_after_trigger(product_id: int, price: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET price_after_trigger = ? WHERE id = ?", (price, product_id)
        )
        await db.commit()


async def set_product_channel_id(product_id: int, channel_id: str,
                                  invite_link: str | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET channel_id = ?, channel_invite_link = ? WHERE id = ?",
            (channel_id or None, invite_link, product_id),
        )
        await db.commit()


async def set_infobiz_bonus(product_id: int, limit: int | None, text: str | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET bonus_limit = ?, bonus_text = ? WHERE id = ?",
            (limit, text, product_id),
        )
        await db.commit()


# --- Пуш отзыва ---

async def set_review_push(product_id: int, delay: int | None, text: str | None):
    """Сохраняет настройки пуша отзыва для товара. delay=None — отключить."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET review_push_delay = ?, review_push_text = ? WHERE id = ?",
            (delay, text, product_id),
        )
        await db.commit()


async def enqueue_review_push(user_id: int, product_id: int, delay_seconds: int,
                              order_id: int | None = None):
    """Ставит в очередь отправку пуша через delay_seconds секунд.

    order_id — для физтоваров: отсчёт у них идёт от отметки «отправлено»,
    а не от оплаты (см. cb_order_shipped), и заказ могли отметить
    отправленным, откатить и отметить снова. Если пуш для этого заказа И
    этого товара уже стоит в очереди, второй раз не ставим — иначе клиент
    получит два одинаковых сообщения. Дедуп по паре (order_id, product_id),
    а не по одному order_id — в заказе может быть несколько разных товаров,
    каждому свой пуш.
    """
    from datetime import datetime, timezone, timedelta
    send_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    async with aiosqlite.connect(DB_PATH) as db:
        if order_id is not None:
            async with db.execute(
                "SELECT 1 FROM review_push_queue WHERE order_id = ? AND product_id = ?",
                (order_id, product_id),
            ) as cur:
                if await cur.fetchone():
                    return
        await db.execute(
            """INSERT INTO review_push_queue (user_id, product_id, send_at, order_id)
               VALUES (?, ?, ?, ?)""",
            (user_id, product_id, send_at, order_id),
        )
        await db.commit()


async def get_due_review_pushes() -> list[dict]:
    """Возвращает записи из очереди, которые уже пора отправить."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT rq.id, rq.user_id, rq.product_id,
                      p.review_push_text, p.name AS product_name
               FROM review_push_queue rq
               JOIN products p ON p.id = rq.product_id
               WHERE rq.sent = 0 AND rq.send_at <= ?""",
            (now,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def mark_review_push_sent(push_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE review_push_queue SET sent = 1 WHERE id = ?", (push_id,))
        await db.commit()


# --- Бонусные разборы ---

async def add_bonus_winner(user_id: int, username: str | None,
                            first_name: str, product_id: int):
    """Записывает победителя бонуса (первые N покупателей)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO bonus_reviews (user_id, username, first_name, product_id)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, product_id) DO NOTHING""",
            (user_id, username, first_name, product_id),
        )
        await db.commit()


async def is_bonus_winner(user_id: int, product_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM bonus_reviews WHERE user_id = ? AND product_id = ?",
            (user_id, product_id),
        ) as cur:
            return (await cur.fetchone()) is not None


async def get_bonus_winners_for_any_product(user_id: int) -> list[dict]:
    """Возвращает все бонусы пользователя (по всем продуктам)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT br.*, p.name AS product_name
               FROM bonus_reviews br
               JOIN products p ON p.id = br.product_id
               WHERE br.user_id = ?""",
            (user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def save_bonus_video_link(user_id: int, product_id: int, link: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE bonus_reviews
               SET video_link = ?, link_sent_at = CURRENT_TIMESTAMP
               WHERE user_id = ? AND product_id = ?""",
            (link, user_id, product_id),
        )
        await db.commit()


async def get_bonus_reviews_for_product(product_id: int) -> list[dict]:
    """Все победители бонуса по товару — для админки."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM bonus_reviews
               WHERE product_id = ?
               ORDER BY won_at""",
            (product_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# --- Воронки ---

async def get_all_funnels() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM funnels ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_funnel(funnel_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM funnels WHERE id = ?", (funnel_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_funnel_by_slug(slug: str) -> Optional[dict]:
    """Ищет воронку по ASCII-slug (используется в deep links)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM funnels WHERE slug = ?", (slug,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def create_funnel(funnel_id: str, name: str, product_id: Optional[int] = None):
    import secrets
    slug = "f" + secrets.token_hex(4)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO funnels (id, name, product_id, slug) VALUES (?, ?, ?, ?)",
            (funnel_id, name, product_id, slug),
        )
        await db.commit()
    return slug


async def update_funnel_product(funnel_id: str, product_id: Optional[int]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE funnels SET product_id = ? WHERE id = ?", (product_id, funnel_id)
        )
        await db.commit()


async def get_funnel_steps(funnel_id: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM funnel_steps WHERE funnel_id = ? ORDER BY step",
            (funnel_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def upsert_funnel_step(funnel_id: str, step: int, delay_seconds: int, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO funnel_steps (funnel_id, step, delay_seconds, text)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(funnel_id, step) DO UPDATE SET
                   delay_seconds = excluded.delay_seconds,
                   text = excluded.text""",
            (funnel_id, step, delay_seconds, text),
        )
        await db.commit()


async def delete_funnel_step(funnel_id: str, step: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM funnel_steps WHERE funnel_id = ? AND step = ?",
            (funnel_id, step),
        )
        await db.commit()


async def enqueue_funnel(user_id: int, funnel_id: str, source: Optional[str] = None):
    """Ставит все шаги воронки в очередь для пользователя (если ещё не в очереди)."""
    # Проверяем — не запущена ли уже эта воронка для юзера
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT COUNT(*) FROM funnel_queue
               WHERE user_id = ? AND funnel_id = ? AND cancelled = 0""",
            (user_id, funnel_id),
        ) as cur:
            if (await cur.fetchone())[0] > 0:
                return  # Уже в воронке

        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM funnel_steps WHERE funnel_id = ? ORDER BY step",
            (funnel_id,),
        ) as cur:
            steps = [dict(r) for r in await cur.fetchall()]

        for s in steps:
            await db.execute(
                """INSERT INTO funnel_queue (user_id, funnel_id, step, send_at, source)
                   VALUES (?, ?, ?, datetime('now', ? || ' seconds'), ?)""",
                (user_id, funnel_id, s["step"], str(s["delay_seconds"]), source),
            )
        await db.commit()


async def cancel_funnel_for_user(user_id: int, product_id: int):
    """Отменяет все pending-шаги воронок, привязанных к этому продукту."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE funnel_queue SET cancelled = 1
               WHERE user_id = ? AND sent = 0 AND cancelled = 0
                 AND funnel_id IN (SELECT id FROM funnels WHERE product_id = ?)""",
            (user_id, product_id),
        )
        await db.commit()


async def get_due_funnel_messages() -> list[dict]:
    """Возвращает сообщения, которые пора отправить."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT fq.id, fq.user_id, fq.funnel_id, fq.step,
                      fs.text
               FROM funnel_queue fq
               JOIN funnel_steps fs ON fs.funnel_id = fq.funnel_id AND fs.step = fq.step
               WHERE fq.sent = 0 AND fq.cancelled = 0
                 AND fq.send_at <= datetime('now')
               ORDER BY fq.send_at""",
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def mark_funnel_message_sent(queue_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE funnel_queue SET sent = 1 WHERE id = ?", (queue_id,)
        )
        await db.commit()


async def get_funnel_stats(funnel_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM funnel_queue WHERE funnel_id = ?",
            (funnel_id,),
        ) as cur:
            total = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM funnel_queue WHERE funnel_id = ? AND cancelled = 1",
            (funnel_id,),
        ) as cur:
            converted = (await cur.fetchone())[0]
        async with db.execute(
            """SELECT COUNT(DISTINCT user_id) FROM funnel_queue
               WHERE funnel_id = ? AND sent = 0 AND cancelled = 0""",
            (funnel_id,),
        ) as cur:
            active = (await cur.fetchone())[0]
    return {"total": total, "converted": converted, "active": active}


async def get_funnel_analytics(funnel_id: str) -> dict:
    """
    Полная аналитика по воронке:
    - total_enrolled: уникальных пользователей вошло в воронку
    - converted: купили (cancelled=1 означает, что воронка остановлена из-за покупки)
    - active: ещё в воронке (pending шаги есть, не cancelled)
    - dropped: получили все шаги, но не купили
    - conversion_rate: %
    - by_source: [{source, enrolled, converted, conversion_rate}] — по источникам
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Уникальные пользователи в воронке
        async with db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM funnel_queue WHERE funnel_id = ?",
            (funnel_id,),
        ) as cur:
            total_enrolled = (await cur.fetchone())[0]

        # Купили (отменена воронка из-за покупки)
        async with db.execute(
            """SELECT COUNT(DISTINCT user_id) FROM funnel_queue
               WHERE funnel_id = ? AND cancelled = 1""",
            (funnel_id,),
        ) as cur:
            converted = (await cur.fetchone())[0]

        # Активных (есть хоть один pending несentый шаг)
        async with db.execute(
            """SELECT COUNT(DISTINCT user_id) FROM funnel_queue
               WHERE funnel_id = ? AND sent = 0 AND cancelled = 0""",
            (funnel_id,),
        ) as cur:
            active = (await cur.fetchone())[0]

        # Аналитика по источникам
        async with db.execute(
            """SELECT
                 COALESCE(source, '(прямой)') AS source,
                 COUNT(DISTINCT user_id) AS enrolled,
                 COUNT(DISTINCT CASE WHEN cancelled = 1 THEN user_id END) AS converted
               FROM funnel_queue
               WHERE funnel_id = ?
               GROUP BY COALESCE(source, '(прямой)')
               ORDER BY enrolled DESC""",
            (funnel_id,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        by_source = []
        for r in rows:
            rate = round(r["converted"] / r["enrolled"] * 100, 1) if r["enrolled"] else 0
            by_source.append({
                "source": r["source"],
                "enrolled": r["enrolled"],
                "converted": r["converted"],
                "conversion_rate": rate,
            })

    dropped = total_enrolled - converted - active
    conversion_rate = round(converted / total_enrolled * 100, 1) if total_enrolled else 0

    return {
        "total_enrolled": total_enrolled,
        "converted": converted,
        "active": active,
        "dropped": max(dropped, 0),
        "conversion_rate": conversion_rate,
        "by_source": by_source,
    }


async def grant_channel_access(user_id: int, channel_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO channel_access (user_id, channel_id)
               VALUES (?, ?)
               ON CONFLICT(user_id, channel_id) DO NOTHING""",
            (user_id, channel_id),
        )
        await db.commit()


async def check_channel_access(user_id: int, channel_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM channel_access WHERE user_id = ? AND channel_id = ?",
            (user_id, channel_id),
        ) as cur:
            return (await cur.fetchone()) is not None


# --- Purchases ---

async def add_purchase(user_id: int, username: Optional[str], product_id: int,
                       telegram_payment_id: str, amount: int,
                       delivery_amount: int = 0, quantity: int = 1,
                       delivery_cost: float = 0.0) -> int:
    """Добавляет покупку и возвращает порядковый номер покупки этого товара (1-based).

    amount — вся сумма платежа, delivery_amount — сколько внутри неё доставка,
    delivery_cost — сколько из доставки уйдёт в СДЭК по счёту,
    quantity — сколько единиц товара в этой покупке.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO purchases
                   (user_id, username, product_id, telegram_payment_id, amount,
                    delivery_amount, quantity, delivery_cost)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, username, product_id, telegram_payment_id, amount,
             delivery_amount, max(1, quantity), delivery_cost),
        )
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*) FROM purchases WHERE product_id = ?", (product_id,)
        ) as cur:
            return int((await cur.fetchone())[0])


async def add_waitlist_entry(user_id: int, username: Optional[str], first_name: str, product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO waitlist_entries (user_id, username, first_name, product_id)
               VALUES (?, ?, ?, ?)""",
            (user_id, username, first_name, product_id),
        )
        await db.commit()


async def get_waitlist_entry(user_id: int, product_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM waitlist_entries WHERE user_id = ? AND product_id = ?",
            (user_id, product_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


ORDER_CODE_PREFIX = "malimabi-store"


async def peek_order_code() -> str:
    """Какой номер получит следующий заказ. Счётчик не трогает — для тестов."""
    value = await get_setting("order_counter")
    try:
        number = int(value) + 1
    except (TypeError, ValueError):
        number = 1
    return f"{ORDER_CODE_PREFIX}-{number:03d}"


async def next_order_code() -> str:
    """Следующий номер заказа магазина: malimabi-store-001, -002, ...

    Счётчик отдельный от id заказов и начинается с 001. Увеличивается
    в одной транзакции, чтобы два одновременных заказа не получили один номер.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT value FROM settings WHERE key = 'order_counter'"
        ) as cur:
            row = await cur.fetchone()
        try:
            number = int(row[0]) + 1 if row else 1
        except (TypeError, ValueError):
            number = 1
        await db.execute(
            "INSERT INTO settings (key, value) VALUES ('order_counter', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(number),),
        )
        await db.commit()
    return f"{ORDER_CODE_PREFIX}-{number:03d}"


async def get_purchase(user_id: int, product_id: int) -> Optional[dict]:
    """Возвращает последнюю покупку пользователя по конкретному товару, или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM purchases WHERE user_id = ? AND product_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, product_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_purchase_by_payment_id(telegram_payment_id: str) -> Optional[dict]:
    """Возвращает покупку по номеру платежа Prodamus (order_id), или None.
    Используется для защиты от повторной обработки одного и того же вебхука."""
    if not telegram_payment_id:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM purchases WHERE telegram_payment_id = ? LIMIT 1",
            (telegram_payment_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_user_purchases(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT pu.amount, pu.created_at, pr.name as product_name, pr.category
               FROM purchases pu
               LEFT JOIN products pr ON pu.product_id = pr.id
               WHERE pu.user_id = ?
               ORDER BY pu.created_at DESC""",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def save_pending_delivery(user_id: int, product_id: int, delivery_str: str,
                                survey_json: str | None = None, amount: int = 0,
                                recipient_name: str | None = None,
                                recipient_phone: str | None = None,
                                pvz_code: str | None = None,
                                delivery_amount: int = 0,
                                delivery_cost: float = 0.0,
                                round_products_json: str | None = None):
    """Сохраняет детали будущего заказа (адрес + ответы опроса + сумму) до оплаты.

    ФИО, телефон и код ПВЗ нужны, чтобы после оплаты завести заказ в СДЭК.
    delivery_amount — сколько взяли с клиента, delivery_cost — сколько из
    этого уйдёт в СДЭК по счёту (тариф + страховка + НДС).
    round_products_json — товар каждого раунда, если в заказе смешаны
    разные товары (см. handlers/brief_handler.py:_other_physical_products).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO pending_deliveries
                   (user_id, product_id, delivery_str, survey_json, amount,
                    recipient_name, recipient_phone, pvz_code, delivery_amount,
                    delivery_cost, round_products_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, product_id) DO UPDATE SET
                   delivery_str = excluded.delivery_str,
                   survey_json = excluded.survey_json,
                   amount = excluded.amount,
                   recipient_name = excluded.recipient_name,
                   recipient_phone = excluded.recipient_phone,
                   pvz_code = excluded.pvz_code,
                   delivery_amount = excluded.delivery_amount,
                   delivery_cost = excluded.delivery_cost,
                   round_products_json = excluded.round_products_json,
                   created_at = CURRENT_TIMESTAMP""",
            (user_id, product_id, delivery_str, survey_json, amount,
             recipient_name, recipient_phone, pvz_code, delivery_amount,
             delivery_cost, round_products_json),
        )
        await db.commit()


async def get_orders_tracking() -> list[dict]:
    """Заказы, по которым ещё ждём прибытия посылки в пункт выдачи.

    Берём только свежие: через два месяца посылка либо получена, либо
    уехала обратно — дальше опрашивать СДЭК бессмысленно.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM orders
               WHERE cdek_uuid IS NOT NULL
                 AND arrived_notified_at IS NULL
                 AND created_at >= datetime('now', '-60 days')
               ORDER BY created_at"""
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def set_order_arrived(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET arrived_notified_at = CURRENT_TIMESTAMP WHERE id = ?",
            (order_id,),
        )
        await db.commit()


async def set_order_cdek(order_id: int, cdek_uuid: str, cdek_number: str | None = None):
    """Привязывает к заказу идентификаторы накладной СДЭК."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET cdek_uuid = ?, cdek_number = ? WHERE id = ?",
            (cdek_uuid, cdek_number, order_id),
        )
        await db.commit()


async def set_order_barcode_file(order_id: int, file_id: str):
    """Запоминает присланную наклейку ШК — повторно шлём её же, без СДЭК."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET cdek_barcode_file_id = ? WHERE id = ?",
            (file_id, order_id),
        )
        await db.commit()


async def create_order(user_id: int, product_id: int, prodamus_order_id: str,
                       summary: str, rounds_json: str | None = None,
                       order_code: str | None = None,
                       recipient_name: str | None = None,
                       recipient_phone: str | None = None,
                       pvz_code: str | None = None,
                       round_products_json: str | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO orders
                   (user_id, product_id, prodamus_order_id, summary, rounds_json,
                    order_code, recipient_name, recipient_phone, pvz_code,
                    round_products_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, product_id, prodamus_order_id, summary, rounds_json,
             order_code, recipient_name, recipient_phone, pvz_code,
             round_products_json),
        )
        await db.commit()
        return cur.lastrowid


async def get_orders_for_product(product_id: int) -> list[dict]:
    """Все заказы товара (для аналитики): rounds_json + исполнитель."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT rounds_json, assignee_name, created_at, shipped_at, shipped_by_name "
            "FROM orders WHERE product_id = ?",
            (product_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def add_order_message(order_id: int, chat_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO order_messages (order_id, chat_id, message_id) VALUES (?, ?, ?)",
            (order_id, chat_id, message_id),
        )
        await db.commit()


async def get_order(order_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


def pack_printer_ids(ids) -> str:
    """[1,2] -> ',1,2,' — так LIKE '%,1,%' находит id целиком, без ложных совпадений."""
    uniq = sorted({int(i) for i in ids if i})
    return "," + ",".join(str(i) for i in uniq) + "," if uniq else ""


def printer_ids(order: dict) -> set[int]:
    """Кто печатает этот заказ. Пусто — значит только исполнитель."""
    raw = (order or {}).get("printer_ids") or ""
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


async def set_order_printers(order_id: int, ids) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET printer_ids = ? WHERE id = ?",
                         (pack_printer_ids(ids), order_id))
        await db.commit()


def parse_routing_line(summary: str) -> list:
    """Достаёт разметку позиций из текста заказа (для заказов до routing_json)."""
    for ln in (summary or "").split("\n"):
        if not ln.startswith("🖨 <b>Печат"):
            continue
        body = ln.split("</b>", 1)[-1].strip()
        if "Поз." not in body:
            return [None if body == "не определён" else body]
        whos = []
        for part in body.split(" · "):
            who = part.split("—", 1)[1].strip() if "—" in part else ""
            whos.append(None if who in ("", "не определён") else who)
        return whos
    return []


def order_routing(order: dict) -> list:
    """Ники печатающих по позициям заказа: ['@a', '@b']. Пусто — не размечен."""
    raw = (order or {}).get("routing_json")
    if not raw:
        return []
    try:
        whos = json.loads(raw)
    except Exception:
        return []
    return list(whos) if isinstance(whos, list) else []


async def set_order_routing(order_id: int, whos) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET routing_json = ? WHERE id = ?",
            (json.dumps(list(whos), ensure_ascii=False), order_id),
        )
        await db.commit()


async def is_policy_accepted(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT policy_accepted_at FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row and row[0])


async def accept_policy(user_id: int) -> Optional[str]:
    """Фиксирует согласие и возвращает отложенный deep-link payload."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT pending_payload FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        payload = row[0] if row else None
        await db.execute(
            "UPDATE users SET policy_accepted_at = CURRENT_TIMESTAMP, "
            "pending_payload = NULL WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()
    return payload


async def set_pending_payload(user_id: int, payload: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET pending_payload = ? WHERE user_id = ?",
                         (payload, user_id))
        await db.commit()


async def get_admin_by_id(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, username, first_name FROM users WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_admin_by_username(name: str, admin_ids: list[int]) -> Optional[dict]:
    """Ищет администратора по нику из настройки «кто печатает».

    Ник там пишется руками, поэтому кроме точного совпадения допускаем
    опечатку в виде лишней/недостающей буквы на конце (@baryshovv →
    @baryshovvv). Неоднозначные совпадения отбрасываем.
    """
    clean = (name or "").strip().lstrip("@").lower()
    if not clean or not admin_ids:
        return None
    placeholders = ",".join("?" * len(admin_ids))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT user_id, username FROM users WHERE user_id IN ({placeholders})",
            admin_ids,
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    for r in rows:
        if (r.get("username") or "").lower() == clean:
            return r
    near = [r for r in rows
            if (r.get("username") or "").lower().startswith(clean)
            or clean.startswith((r.get("username") or "").lower())]
    return near[0] if len(near) == 1 else None


async def get_order_messages(order_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT chat_id, message_id FROM order_messages WHERE order_id = ?", (order_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def set_order_assignee(order_id: int, assignee_id: int, assignee_name: str) -> bool:
    """Атомарно назначает исполнителя. True — если заказ был свободен и мы его заняли."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE orders SET assignee_id = ?, assignee_name = ? WHERE id = ? AND assignee_id IS NULL",
            (assignee_id, assignee_name, order_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def force_set_order_assignee(order_id: int, assignee_id: int, assignee_name: str):
    """Переназначает исполнителя (в т.ч. если заказ уже занят)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET assignee_id = ?, assignee_name = ? WHERE id = ?",
            (assignee_id, assignee_name, order_id),
        )
        await db.commit()


async def clear_order_assignee(order_id: int):
    """Снимает исполнителя — заказ снова свободен."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET assignee_id = NULL, assignee_name = NULL WHERE id = ?",
            (order_id,),
        )
        await db.commit()


async def add_order_print(order_id: int, user_id: int, user_name: str) -> bool:
    """Отмечает свою часть заказа распечатанной. False — если уже отмечал."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO order_prints (order_id, user_id, user_name) "
            "VALUES (?, ?, ?)",
            (order_id, user_id, user_name),
        )
        await db.commit()
        return cur.rowcount > 0


def print_positions(row: dict) -> set:
    """Номера позиций в отметке. Пусто — отметка на всю свою часть заказа."""
    raw = (row or {}).get("positions") or ""
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


async def set_order_print_positions(order_id: int, user_id: int, user_name: str,
                                    positions) -> None:
    """Переписывает отметку печати этого человека. Пусто — отметки нет."""
    positions = sorted({int(p) for p in positions if p})
    async with aiosqlite.connect(DB_PATH) as db:
        if not positions:
            await db.execute(
                "DELETE FROM order_prints WHERE order_id = ? AND user_id = ?",
                (order_id, user_id),
            )
        else:
            packed = "," + ",".join(str(p) for p in positions) + ","
            await db.execute(
                "INSERT INTO order_prints (order_id, user_id, user_name, positions) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(order_id, user_id) DO UPDATE SET "
                "  positions = excluded.positions, user_name = excluded.user_name, "
                "  printed_at = CURRENT_TIMESTAMP",
                (order_id, user_id, user_name, packed),
            )
        await db.commit()


async def remove_order_print(order_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM order_prints WHERE order_id = ? AND user_id = ?",
            (order_id, user_id),
        )
        await db.commit()


async def get_order_prints(order_id: int) -> list[dict]:
    """Кто уже отметил свою часть: [{user_id, user_name, printed_at}]."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM order_prints WHERE order_id = ? ORDER BY printed_at",
            (order_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def set_order_printed(order_id: int, user_id: int, user_name: str) -> bool:
    """Отмечает заказ распечатанным. False — если уже был отмечен."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """UPDATE orders
               SET printed_at = CURRENT_TIMESTAMP, printed_by_id = ?, printed_by_name = ?
               WHERE id = ? AND printed_at IS NULL""",
            (user_id, user_name, order_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def clear_order_printed(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET printed_at = NULL, printed_by_id = NULL, "
            "printed_by_name = NULL WHERE id = ?",
            (order_id,),
        )
        await db.commit()


async def set_order_shipped(order_id: int, user_id: int, user_name: str) -> bool:
    """Отмечает заказ отправленным. True — если отметка проставлена сейчас."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """UPDATE orders SET shipped_at = CURRENT_TIMESTAMP,
                                 shipped_by_id = ?, shipped_by_name = ?
               WHERE id = ? AND shipped_at IS NULL""",
            (user_id, user_name, order_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def clear_order_shipped(order_id: int):
    """Снимает отметку об отправке.

    Заодно снимает ещё не отправленный пуш отзыва, поставленный по этой
    отметке — иначе при повторной отправке заказа пуш не переставится
    (order_id уже занят) и уйдёт по старому, ошибочному времени.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE orders SET shipped_at = NULL, shipped_by_id = NULL,
                                 shipped_by_name = NULL WHERE id = ?""",
            (order_id,),
        )
        await db.execute(
            "DELETE FROM review_push_queue WHERE order_id = ? AND sent = 0",
            (order_id,),
        )
        await db.commit()


def period_bounds(dt_from: str, dt_to: str) -> tuple[str, str]:
    """Голую дату дотягиваем до полных суток.

    Иначе '2026-08-05' с обеих сторон означает полночь, и всё, что было
    в течение дня, в выборку не попадает.
    """
    if len(dt_from) == 10:
        dt_from += " 00:00:00"
    if len(dt_to) == 10:
        dt_to += " 23:59:59"
    return dt_from, dt_to


async def get_period_revenue(dt_from: str, dt_to: str) -> dict:
    """Выручка за период по линейкам: физтовары, цифра и доставка отдельно.

    Границы — момент времени ('ГГГГ-ММ-ДД ЧЧ:ММ:СС'), включительно с обеих
    сторон: взаиморасчёт может быть зафиксирован посреди дня.

    Доставка идёт транзитом в СДЭК, поэтому в стоимость товара не входит
    и в проценты партнёру не попадает.
    """
    dt_from, dt_to = period_bounds(dt_from, dt_to)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT COUNT(*) AS count,
                      COALESCE(SUM(pu.delivery_amount), 0) AS delivery,
                      COALESCE(SUM(pu.delivery_cost), 0) AS delivery_cost,
                      COALESCE(SUM(CASE WHEN COALESCE(pu.delivery_cost, 0) = 0
                                        THEN pu.delivery_amount ELSE 0 END), 0)
                          AS delivery_legacy,
                      COALESCE(SUM(CASE WHEN pr.category = 'physical'
                                        THEN pu.amount - COALESCE(pu.delivery_amount, 0)
                                        ELSE 0 END), 0) AS physical,
                      COALESCE(SUM(CASE WHEN pr.category = 'physical' OR pr.category IS NULL
                                        THEN 0
                                        ELSE pu.amount - COALESCE(pu.delivery_amount, 0)
                                   END), 0) AS digital,
                      COALESCE(SUM(CASE WHEN pr.category IS NULL
                                        THEN pu.amount - COALESCE(pu.delivery_amount, 0)
                                        ELSE 0 END), 0) AS unknown
               FROM purchases pu
               LEFT JOIN products pr ON pr.id = pu.product_id
               WHERE datetime(pu.created_at) BETWEEN datetime(?) AND datetime(?)""",
            (dt_from, dt_to),
        ) as cur:
            row = dict(await cur.fetchone())
    # Товар без категории считаем цифрой: физику мы всегда помечаем сами
    row["digital"] += row.pop("unknown")
    return row


async def get_revenue_by_month(limit: int = 6) -> list[dict]:
    """Принято по месяцам: [{'month': 'ГГГГ-ММ', 'gross', 'count'}], свежие сверху.

    НПД считается со всей принятой суммы (см. config.NPD_PERCENT) — отсюда
    берётся начисление налога по месяцам, см. get_npd_payments_by_month.

    created_at хранится в UTC (SQLite CURRENT_TIMESTAMP), а чек в «Мой
    налог» пробивается по МСК — без поправки покупки с 00:00 до 02:59 МСК
    (21:00-23:59 UTC предыдущих суток) попадали бы в предыдущий месяц,
    хотя по факту относятся уже к новому.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT strftime('%Y-%m', datetime(created_at, '+3 hours')) AS month,
                      COALESCE(SUM(amount), 0) AS gross, COUNT(*) AS count
               FROM purchases GROUP BY month ORDER BY month DESC LIMIT ?""",
            (int(limit),),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_purchases_by_month(month: str) -> list[dict]:
    """Отдельные покупки за месяц ('ГГГГ-ММ', по МСК) — сверить с «Мой налог»."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT pu.id, pu.user_id, pu.created_at, pu.amount, pu.delivery_amount,
                      pr.name AS product_name, u.username, u.first_name
               FROM purchases pu
               LEFT JOIN products pr ON pr.id = pu.product_id
               LEFT JOIN users u ON u.user_id = pu.user_id
               WHERE strftime('%Y-%m', datetime(pu.created_at, '+3 hours')) = ?
               ORDER BY pu.created_at""",
            (month,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_npd_payments_by_month(limit: int = 6) -> list[dict]:
    """Фактически оплаченный НПД по месяцам (по МСК): [{'month', 'total', 'count'}]."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT strftime('%Y-%m', datetime(paid_at, '+3 hours')) AS month,
                      COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count
               FROM npd_payments GROUP BY month ORDER BY month DESC LIMIT ?""",
            (int(limit),),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── Расходы ────────────────────────────────────────────────────────────

async def get_expense_categories() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name FROM expense_categories ORDER BY position, name"
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def add_expense_category(name: str) -> bool:
    """Новая статья расходов. False — если такая уже есть."""
    name = (name or "").strip()
    if not name:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM expense_categories"
        ) as cur:
            pos = (await cur.fetchone())[0]
        cur = await db.execute(
            "INSERT OR IGNORE INTO expense_categories (name, position) VALUES (?, ?)",
            (name, pos),
        )
        await db.commit()
        return cur.rowcount > 0


async def add_expense(category: str, amount: float, comment: str = "",
                      user_id: int = 0, user_name: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO expenses (category, amount, comment, user_id, user_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (category, float(amount), (comment or "").strip() or None,
             user_id, user_name),
        )
        await db.commit()
        return cur.lastrowid


async def delete_expense(expense_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_expenses(date_from: str, date_to: str) -> list[dict]:
    """Расходы за период (даты 'ГГГГ-ММ-ДД' включительно), свежие сверху."""
    date_from, date_to = period_bounds(date_from, date_to)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM expenses WHERE datetime(spent_at) BETWEEN datetime(?) AND datetime(?) "
            "ORDER BY spent_at DESC, id DESC",
            (date_from, date_to),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_expenses_summary(date_from: str, date_to: str) -> dict:
    """{'total': сумма, 'by_category': [{'category','total','cnt'}]}."""
    date_from, date_to = period_bounds(date_from, date_to)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT category, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS cnt "
            "FROM expenses WHERE datetime(spent_at) BETWEEN datetime(?) AND datetime(?) "
            "GROUP BY category ORDER BY total DESC",
            (date_from, date_to),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return {"total": sum(r["total"] for r in rows), "by_category": rows}


# ── Счёт СДЭК ──────────────────────────────────────────────────────────
#
# СДЭК работает постоплатой: мы отправляем посылки, а счёт за них приходит
# потом. Деньги на него клиент уже отдал вместе с заказом — доставка идёт
# транзитом и в дележ прибыли входит отдельно (services.payout.delivery_out).
#
# Отсюда две величины: сколько набежало по накладным и сколько мы уже
# отдали СДЭК по счетам. Разница — отложенные деньги, на которые придёт
# следующий счёт.

# Накладная набегает на счёт, когда заказ заведён в СДЭК (cdek_uuid) либо
# отмечен отправленным — накладную могли создать руками в кабинете.
_CDEK_ACCRUED_FROM = """
               FROM orders o
               LEFT JOIN purchases pu
                      ON pu.telegram_payment_id = o.prodamus_order_id
               WHERE (o.cdek_uuid IS NOT NULL OR o.shipped_at IS NOT NULL)
                 AND (COALESCE(pu.delivery_cost, 0) > 0
                      OR COALESCE(pu.delivery_amount, 0) > 0)"""

# Две суммы, как в отчётах: по новым заказам счёт СДЭК сохранён
# (тариф + страховка + НДС), у заказов до августа 2026 колонки не было —
# для них в СДЭК уходило то, что осталось от delivery_amount после
# комиссии Prodamus. Сводит их services.payout.delivery_out.
_CDEK_ACCRUED_SUMS = """
                      COALESCE(SUM(CASE WHEN COALESCE(pu.delivery_cost, 0) > 0
                                        THEN pu.delivery_cost ELSE 0 END), 0) AS cost,
                      COALESCE(SUM(CASE WHEN COALESCE(pu.delivery_cost, 0) = 0
                                        THEN COALESCE(pu.delivery_amount, 0)
                                        ELSE 0 END), 0) AS legacy"""

# Дата, на которую набежала накладная: отправка, а пока её нет — дата
# заказа. Накладная заводится сразу после оплаты, счёт идёт с того же дня.
_CDEK_ACCRUED_DATE = "COALESCE(o.shipped_at, o.created_at)"


async def add_cdek_payment(amount: float, comment: str = "",
                           user_id: int = 0, user_name: str = "") -> int:
    """Оплаченный счёт СДЭК: сколько денег мы им отдали."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO cdek_payments (amount, comment, user_id, user_name) "
            "VALUES (?, ?, ?, ?)",
            (float(amount), (comment or "").strip() or None, user_id, user_name),
        )
        await db.commit()
        return cur.lastrowid


async def delete_cdek_payment(payment_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM cdek_payments WHERE id = ?", (payment_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_cdek_payments(limit: int = 20) -> list[dict]:
    """Оплаченные счета, свежие сверху."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM cdek_payments ORDER BY paid_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_cdek_payments_summary(date_from: str | None = None,
                                    date_to: str | None = None) -> dict:
    """Оплаченные счета: {'total': сумма, 'count': сколько счетов}."""
    sql = ("SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count "
           "FROM cdek_payments")
    params: tuple = ()
    if date_from and date_to:
        date_from, date_to = period_bounds(date_from, date_to)
        sql += " WHERE datetime(paid_at) BETWEEN datetime(?) AND datetime(?)"
        params = (date_from, date_to)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return dict(await cur.fetchone())


def _cdek_total(row: dict) -> dict:
    """Дополняет строку выборки итогом: точные счета + оценка по старым."""
    import config
    from services.payout import delivery_out

    row = dict(row)
    row["cost"] = float(row["cost"])
    row["legacy"] = float(row["legacy"])
    row["total"] = delivery_out(row["cost"], row["legacy"],
                                config.PRODAMUS_FEE_PERCENT)
    return row


async def get_cdek_accrued(date_from: str | None = None,
                           date_to: str | None = None) -> dict:
    """Набежало по накладным: {'total', 'count', 'cost', 'legacy'}.

    legacy — часть суммы, посчитанная по старой формуле: у заказов до
    августа 2026 счёт СДЭК не сохранялся, и точной цифры для них нет.
    """
    sql = (f"SELECT COUNT(*) AS count,{_CDEK_ACCRUED_SUMS}{_CDEK_ACCRUED_FROM}")
    params: tuple = ()
    if date_from and date_to:
        date_from, date_to = period_bounds(date_from, date_to)
        sql += (f" AND datetime({_CDEK_ACCRUED_DATE}) "
                f"BETWEEN datetime(?) AND datetime(?)")
        params = (date_from, date_to)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return _cdek_total(await cur.fetchone())


async def get_cdek_accrued_by_month(limit: int = 6) -> list[dict]:
    """Накладные по месяцам (по МСК): [{'month': 'ГГГГ-ММ', 'total', 'count', …}]."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT strftime('%Y-%m', datetime({_CDEK_ACCRUED_DATE}, '+3 hours')) AS month, "
            f"COUNT(*) AS count,{_CDEK_ACCRUED_SUMS}{_CDEK_ACCRUED_FROM}"
            f" GROUP BY month ORDER BY month DESC LIMIT ?",
            (int(limit),),
        ) as cur:
            return [_cdek_total(r) for r in await cur.fetchall()]


async def get_cdek_account(date_from: str | None = None,
                           date_to: str | None = None) -> dict:
    """Состояние по СДЭК.

    due = набежало по накладным − оплачено счетов. Это отложенные деньги:
    столько мы должны СДЭК и столько придёт в следующем счёте. Минус
    означает переплату — оплатили вперёд.

    С границами периода те же суммы за него добавляются с префиксом period_.
    """
    accrued = await get_cdek_accrued()
    paid = await get_cdek_payments_summary()
    account = {
        "accrued": float(accrued["total"]),
        "accrued_count": int(accrued["count"]),
        # часть суммы посчитана по старой формуле — точного счёта нет
        "accrued_legacy": float(accrued["legacy"]),
        "paid": float(paid["total"]),
        "paid_count": int(paid["count"]),
        "due": float(accrued["total"]) - float(paid["total"]),
    }
    if date_from and date_to:
        p_accrued = await get_cdek_accrued(date_from, date_to)
        p_paid = await get_cdek_payments_summary(date_from, date_to)
        account["period_accrued"] = float(p_accrued["total"])
        account["period_accrued_count"] = int(p_accrued["count"])
        account["period_paid"] = float(p_paid["total"])
    return account


# ── Кассовый остаток: фактический НПД и выплаты партнёрам ────────────────
# (Взаиморасчёт считает НПД и доли начислением — сколько ДОЛЖНО уйти;
# здесь — сколько РЕАЛЬНО уже ушло, для сверки с банком.)

async def add_npd_payment(amount: float, comment: str = "",
                          user_id: int = 0, user_name: str = "",
                          paid_at: str | None = None) -> int:
    """paid_at — если платёж закрывает конкретный прошлый месяц (кнопка
    «месяц оплачен»), иначе пишется CURRENT_TIMESTAMP (реально сегодня)."""
    async with aiosqlite.connect(DB_PATH) as db:
        if paid_at:
            cur = await db.execute(
                "INSERT INTO npd_payments (amount, comment, user_id, user_name, paid_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (float(amount), (comment or "").strip() or None, user_id, user_name, paid_at),
            )
        else:
            cur = await db.execute(
                "INSERT INTO npd_payments (amount, comment, user_id, user_name) "
                "VALUES (?, ?, ?, ?)",
                (float(amount), (comment or "").strip() or None, user_id, user_name),
            )
        await db.commit()
        return cur.lastrowid


async def delete_npd_payment(payment_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM npd_payments WHERE id = ?", (payment_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_npd_payments(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM npd_payments ORDER BY paid_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_npd_payments_summary(date_from: str | None = None,
                                   date_to: str | None = None) -> dict:
    sql = "SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count FROM npd_payments"
    params: tuple = ()
    if date_from and date_to:
        date_from, date_to = period_bounds(date_from, date_to)
        sql += " WHERE datetime(paid_at) BETWEEN datetime(?) AND datetime(?)"
        params = (date_from, date_to)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return dict(await cur.fetchone())


async def add_payout(recipient: str, amount: float, comment: str = "",
                     user_id: int = 0, user_name: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO payouts (recipient, amount, comment, user_id, user_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (recipient, float(amount), (comment or "").strip() or None, user_id, user_name),
        )
        await db.commit()
        return cur.lastrowid


async def delete_payout(payout_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM payouts WHERE id = ?", (payout_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_payouts(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM payouts ORDER BY paid_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_payouts_summary(date_from: str | None = None,
                              date_to: str | None = None) -> dict:
    """{'total', 'count', 'by_recipient': {имя: сумма}}."""
    sql = "SELECT recipient, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count FROM payouts"
    params: tuple = ()
    if date_from and date_to:
        date_from, date_to = period_bounds(date_from, date_to)
        sql += " WHERE datetime(paid_at) BETWEEN datetime(?) AND datetime(?)"
        params = (date_from, date_to)
    sql += " GROUP BY recipient"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    by_recipient = {r["recipient"]: float(r["total"]) for r in rows}
    return {
        "total": sum(by_recipient.values()),
        "count": sum(int(r["count"]) for r in rows),
        "by_recipient": by_recipient,
    }


async def get_orders_export() -> list[dict]:
    """Все заказы для выгрузки в таблицу: заказ + товар + клиент + оплата."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT o.*, pr.name AS product_name, pr.price AS product_price,
                      u.username, u.first_name,
                      pu.amount AS paid_amount, pu.delivery_amount
               FROM orders o
               LEFT JOIN products pr ON o.product_id = pr.id
               LEFT JOIN users u ON o.user_id = u.user_id
               LEFT JOIN purchases pu
                      ON pu.telegram_payment_id = o.prodamus_order_id
               ORDER BY o.id"""
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_orders(only_unshipped: bool = False) -> list[dict]:
    """Оплаченные заказы (для аналитики отправок), свежие сверху."""
    where = "WHERE o.shipped_at IS NULL" if only_unshipped else ""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""SELECT o.*, pr.name AS product_name, u.username, u.first_name
                FROM orders o
                LEFT JOIN products pr ON o.product_id = pr.id
                LEFT JOIN users u ON o.user_id = u.user_id
                {where}
                ORDER BY o.created_at DESC"""
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def get_pending_orders() -> list[dict]:
    """Незавершённые заказы: опрос заполнен, но оплаты не было.
    (После успешной оплаты вебхук удаляет строку.)"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT pd.*, pr.name AS product_name, pr.price AS product_price,
                      u.username, u.first_name
               FROM pending_deliveries pd
               LEFT JOIN products pr ON pd.product_id = pr.id
               LEFT JOIN users u ON pd.user_id = u.user_id
               ORDER BY pd.created_at DESC"""
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def get_pending_order(user_id: int, product_id: int) -> Optional[dict]:
    """Возвращает {delivery_str, survey_json} — детали заказа, сохранённые до оплаты."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pending_deliveries WHERE user_id = ? AND product_id = ?",
            (user_id, product_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_pending_delivery(user_id: int, product_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT delivery_str FROM pending_deliveries WHERE user_id = ? AND product_id = ?",
            (user_id, product_id),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def delete_pending_delivery(user_id: int, product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM pending_deliveries WHERE user_id = ? AND product_id = ?",
            (user_id, product_id),
        )
        await db.commit()


async def upsert_user(user_id: int, username: Optional[str], first_name: str,
                      ref: Optional[str] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO users (user_id, username, first_name, launch_count, first_seen, last_seen, ref)
               VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username     = excluded.username,
                   first_name   = excluded.first_name,
                   launch_count = launch_count + 1,
                   last_seen    = CURRENT_TIMESTAMP,
                   ref          = COALESCE(users.ref, excluded.ref)""",
            (user_id, username, first_name, ref),
        )
        await db.commit()


async def get_user_ref(user_id: int) -> Optional[str]:
    """Возвращает источник (ref) пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT ref FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def add_settlement(gross: float, fee: float, net: float, count: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settlements (gross, fee, net, count) VALUES (?, ?, ?, ?)",
            (gross, fee, net, count),
        )
        await db.commit()


async def get_last_settlement() -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM settlements ORDER BY settled_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_sales_by_product() -> list[dict]:
    """Разбивка продаж по товарам: name, count, revenue (по убыванию выручки)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT COALESCE(pr.name, 'Товар #' || pu.product_id) AS name,
                      COUNT(*) AS count,
                      COALESCE(SUM(pu.amount), 0) AS revenue
               FROM purchases pu
               LEFT JOIN products pr ON pu.product_id = pr.id
               GROUP BY pu.product_id
               ORDER BY revenue DESC""",
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_purchases_export() -> list[dict]:
    """Все покупки для выгрузки в файл (свежие сверху)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT pu.id, pu.created_at, pu.user_id, pu.username,
                      COALESCE(pr.name, 'Товар #' || pu.product_id) AS product,
                      pu.amount, pu.telegram_payment_id
               FROM purchases pu
               LEFT JOIN products pr ON pu.product_id = pr.id
               ORDER BY pu.created_at DESC""",
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_monthly_earnings_breakdown(months: int = 12) -> list[dict]:
    """
    Возвращает список последних N месяцев с суммами продаж.
    Каждая запись: year, month, count, total
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""SELECT
                strftime('%Y', created_at) AS year,
                strftime('%m', created_at) AS month,
                COUNT(*) AS count,
                COALESCE(SUM(amount), 0) AS total,
                COALESCE(SUM(delivery_amount), 0) AS delivery,
                {_DELIVERY_COST_SQL}
               FROM purchases
               GROUP BY year, month
               ORDER BY year DESC, month DESC
               LIMIT ?""",
            (months,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_monthly_purchases_report(year: int, month: int) -> dict:
    """
    Полная сводка покупок за указанный месяц.
    Возвращает: count, total, by_product, new_users, avg_order, best_day.
    """
    month_from = f"{year:04d}-{month:02d}-01"
    # последний день месяца
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    month_to = f"{year:04d}-{month:02d}-{last_day:02d}"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute(
            f"""SELECT COUNT(*) as cnt, COALESCE(SUM(amount), 0) as total,
                      COALESCE(SUM(delivery_amount), 0) as delivery,
                      {_DELIVERY_COST_SQL}
               FROM purchases
               WHERE DATE(created_at) BETWEEN ? AND ?""",
            (month_from, month_to),
        ) as cur:
            row = await cur.fetchone()
            count = int(row["cnt"])
            total = float(row["total"])
            delivery = float(row["delivery"])
            delivery_cost = float(row["delivery_cost"])
            delivery_legacy = float(row["delivery_legacy"])

        async with db.execute(
            """SELECT pr.name, COALESCE(SUM(pu.quantity), COUNT(*)) as cnt,
                      COALESCE(SUM(pu.amount), 0) as subtotal
               FROM purchases pu
               LEFT JOIN products pr ON pu.product_id = pr.id
               WHERE DATE(pu.created_at) BETWEEN ? AND ?
               GROUP BY pu.product_id
               ORDER BY subtotal DESC""",
            (month_from, month_to),
        ) as cur:
            by_product = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            """SELECT COUNT(*) FROM users
               WHERE DATE(first_seen) BETWEEN ? AND ?""",
            (month_from, month_to),
        ) as cur:
            new_users = (await cur.fetchone())[0]

        avg_order = round(total / count, 2) if count else 0

        # Лучший день по выручке
        async with db.execute(
            """SELECT DATE(created_at) as day, COALESCE(SUM(amount), 0) as day_total
               FROM purchases
               WHERE DATE(created_at) BETWEEN ? AND ?
               GROUP BY day
               ORDER BY day_total DESC
               LIMIT 1""",
            (month_from, month_to),
        ) as cur:
            row = await cur.fetchone()
            best_day = dict(row) if row else None

        # Уникальных покупателей
        async with db.execute(
            """SELECT COUNT(DISTINCT user_id) FROM purchases
               WHERE DATE(created_at) BETWEEN ? AND ?""",
            (month_from, month_to),
        ) as cur:
            unique_buyers = (await cur.fetchone())[0]

    return {
        "count": count,
        "total": total,
        "delivery": delivery,
        "delivery_cost": delivery_cost,
        "delivery_legacy": delivery_legacy,
        "by_product": by_product,
        "new_users": new_users,
        "avg_order": avg_order,
        "best_day": best_day,
        "unique_buyers": unique_buyers,
    }


async def get_purchases_report(date_str: str) -> dict:
    """
    Возвращает сводку покупок за указанную дату (формат YYYY-MM-DD).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""SELECT COUNT(*) as cnt, COALESCE(SUM(amount), 0) as total,
                      COALESCE(SUM(delivery_amount), 0) as delivery,
                      {_DELIVERY_COST_SQL}
               FROM purchases
               WHERE DATE(created_at) = ?""",
            (date_str,),
        ) as cur:
            row = await cur.fetchone()
            count = row["cnt"]
            total = row["total"]
            delivery = row["delivery"]
            delivery_cost = float(row["delivery_cost"])
            delivery_legacy = float(row["delivery_legacy"])

        async with db.execute(
            """SELECT pr.name, COALESCE(SUM(pu.quantity), COUNT(*)) as cnt,
                      COALESCE(SUM(pu.amount), 0) as subtotal
               FROM purchases pu
               LEFT JOIN products pr ON pu.product_id = pr.id
               WHERE DATE(pu.created_at) = ?
               GROUP BY pu.product_id
               ORDER BY subtotal DESC""",
            (date_str,),
        ) as cur:
            rows = await cur.fetchall()
            by_product = [dict(r) for r in rows]

    return {"count": count, "total": total, "delivery": delivery,
            "delivery_cost": delivery_cost, "delivery_legacy": delivery_legacy,
            "by_product": by_product}


async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        # --- Пользователи ---
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(launch_count), 0) FROM users") as cur:
            total_users, total_launches = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE DATE(first_seen) = DATE('now')"
        ) as cur:
            new_today = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE first_seen >= DATE('now', '-7 days')"
        ) as cur:
            new_week = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE first_seen >= DATE('now', '-30 days')"
        ) as cur:
            new_month = (await cur.fetchone())[0]

        # --- Заказы и выручка (всё время) ---
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM purchases") as cur:
            total_orders, total_revenue = await cur.fetchone()
        async with db.execute("SELECT COUNT(DISTINCT user_id) FROM purchases") as cur:
            unique_buyers = (await cur.fetchone())[0]

        # --- Заказы и выручка за периоды ---
        for period, col_o, col_r in [
            ("DATE('now')", "orders_today", "revenue_today"),
            ("DATE('now', '-7 days')", "orders_week", "revenue_week"),
            ("DATE('now', '-30 days')", "orders_month", "revenue_month"),
        ]:
            pass  # заполним ниже через отдельные запросы

        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM purchases WHERE DATE(created_at)=DATE('now')"
        ) as cur:
            orders_today, revenue_today = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM purchases WHERE created_at >= DATE('now','-7 days')"
        ) as cur:
            orders_week, revenue_week = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM purchases WHERE created_at >= DATE('now','-30 days')"
        ) as cur:
            orders_month, revenue_month = await cur.fetchone()

        # --- Средний чек ---
        avg_order = round(total_revenue / total_orders, 0) if total_orders else 0

        # --- Топ товар ---
        async with db.execute(
            """SELECT pr.name, COUNT(*) as cnt
               FROM purchases pu LEFT JOIN products pr ON pu.product_id = pr.id
               GROUP BY pu.product_id ORDER BY cnt DESC LIMIT 1"""
        ) as cur:
            row = await cur.fetchone()
            top_product = f"{row[0]} ({row[1]} шт.)" if row and row[0] else "—"

        # --- Список ожидания ---
        async with db.execute("SELECT COUNT(*) FROM waitlist_entries") as cur:
            waitlist_total = (await cur.fetchone())[0]

        # --- Конверсия ---
        conversion = round(unique_buyers / total_users * 100, 1) if total_users else 0

        return {
            # пользователи
            "total_users":    total_users,
            "total_launches": total_launches,
            "new_today":      new_today,
            "new_week":       new_week,
            "new_month":      new_month,
            # продажи
            "total_orders":   total_orders,
            "total_revenue":  total_revenue,
            "unique_buyers":  unique_buyers,
            "avg_order":      int(avg_order),
            "orders_today":   orders_today,
            "revenue_today":  revenue_today,
            "orders_week":    orders_week,
            "revenue_week":   revenue_week,
            "orders_month":   orders_month,
            "revenue_month":  revenue_month,
            # прочее
            "top_product":    top_product,
            "waitlist_total": waitlist_total,
            "conversion":     conversion,
        }
