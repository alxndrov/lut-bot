import aiosqlite
from typing import Optional

DB_PATH = "bot.db"


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
                       telegram_payment_id: str, amount: int) -> int:
    """Добавляет покупку и возвращает порядковый номер покупки этого товара (1-based)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO purchases (user_id, username, product_id, telegram_payment_id, amount)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, username, product_id, telegram_payment_id, amount),
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


async def save_pending_delivery(user_id: int, product_id: int, delivery_str: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO pending_deliveries (user_id, product_id, delivery_str)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, product_id) DO UPDATE SET delivery_str = excluded.delivery_str,
               created_at = CURRENT_TIMESTAMP""",
            (user_id, product_id, delivery_str),
        )
        await db.commit()


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


async def get_monthly_earnings_breakdown(months: int = 12) -> list[dict]:
    """
    Возвращает список последних N месяцев с суммами продаж.
    Каждая запись: year, month, count, total
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT
                strftime('%Y', created_at) AS year,
                strftime('%m', created_at) AS month,
                COUNT(*) AS count,
                COALESCE(SUM(amount), 0) AS total
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
            """SELECT COUNT(*) as cnt, COALESCE(SUM(amount), 0) as total
               FROM purchases
               WHERE DATE(created_at) BETWEEN ? AND ?""",
            (month_from, month_to),
        ) as cur:
            row = await cur.fetchone()
            count = int(row["cnt"])
            total = float(row["total"])

        async with db.execute(
            """SELECT pr.name, COUNT(*) as cnt, COALESCE(SUM(pu.amount), 0) as subtotal
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
            """SELECT COUNT(*) as cnt, COALESCE(SUM(amount), 0) as total
               FROM purchases
               WHERE DATE(created_at) = ?""",
            (date_str,),
        ) as cur:
            row = await cur.fetchone()
            count = row["cnt"]
            total = row["total"]

        async with db.execute(
            """SELECT pr.name, COUNT(*) as cnt, COALESCE(SUM(pu.amount), 0) as subtotal
               FROM purchases pu
               LEFT JOIN products pr ON pu.product_id = pr.id
               WHERE DATE(pu.created_at) = ?
               GROUP BY pu.product_id
               ORDER BY subtotal DESC""",
            (date_str,),
        ) as cur:
            rows = await cur.fetchall()
            by_product = [dict(r) for r in rows]

    return {"count": count, "total": total, "by_product": by_product}


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
