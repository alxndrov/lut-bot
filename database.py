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
            "INSERT INTO products (name, description, price) VALUES (?, ?, ?)",
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


async def delete_product(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM products WHERE id = ?", (product_id,))
        await db.commit()


# --- Purchases ---

async def add_purchase(user_id: int, username: Optional[str], product_id: int,
                       telegram_payment_id: str, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO purchases (user_id, username, product_id, telegram_payment_id, amount)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, username, product_id, telegram_payment_id, amount),
        )
        await db.commit()


async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM purchases") as cur:
            row = await cur.fetchone()
            total_orders, total_revenue = row
        async with db.execute("SELECT COUNT(DISTINCT user_id) FROM purchases") as cur:
            row = await cur.fetchone()
            unique_buyers = row[0]
        return {
            "total_orders": total_orders,
            "total_revenue": total_revenue,
            "unique_buyers": unique_buyers,
        }
