"""Опрос покупателей курса — от лица Миши, через основной бот.

Три группы по тому, сколько уроков человек открыл (course_lesson_views):
  1 — 4 урока и больше: оценка 1–10 и отзыв «что зашло, чего не хватило»,
      после отзыва — можно ли опубликовать его с именем (без имени
      публиковать можно и так);
  2 — 1–3 урока: «что мешает продолжить?»;
  3 — ни одного: «получилось открыть курс?».

Ответ текстом ловит support.on_free_message (зовёт on_reply раньше всего
остального). Всё, что человек нажал и написал, приходит админам в
malimadmins; тестовые прогоны (/survey test) — только тому, кто тестирует.
"""
import logging
import re
from html import escape

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import database as db

logger = logging.getLogger(__name__)

router = Router()          # основной бот: кнопки опроса
admin_router = Router()    # админский бот: /survey

GROUPS = {1: "посмотрели 4+ урока", 2: "начали, 1–3 урока", 3: "не открыли ни одного урока"}


def group_for(lessons: int) -> int:
    return 1 if lessons >= 4 else 2 if lessons >= 1 else 3


def _hi(name: str | None) -> str:
    return f"Привет, {escape(name or 'друг')}! Это Миша 👋"


INTRO = {
    1: ("Вижу, что большая часть уроков у тебя уже позади. Хочу спросить: как тебе курс?\n\n"
        "Оцени его от 1 до 10 кнопкой ниже или прямо в сообщении. И напиши пару слов: "
        "что зашло, а чего не хватило. Все ответы читаю сам, по ним буду дорабатывать курс."),
    2: ("Вижу, что курс уже начат, но дальше пока не пошло. Что мешает продолжить? "
        "Выбери вариант или напиши своими словами, мне правда важно понять."),
    3: ("Вижу, что уроки курса у тебя пока не открыты. Всё получилось? "
        "Если что-то не работает, скажи, починим."),
}

# Кнопки групп 2 и 3: код → (надпись, ответ, ждём ли текст, тревожно ли)
CHOICES = {
    "time": ("Нет времени",
             "Понимаю 🙂 Уроки никуда не денутся, возвращайся, когда будет время.\n"
             "Если есть мысли, что сделало бы курс удобнее (короче уроки, конспекты, "
             "что-то ещё), напиши, мне пригодится.", True, False),
    "hard": ("Сложно",
             "Спасибо за честный ответ! На каком уроке стало сложно и что именно непонятно? "
             "Напиши, помогу разобраться, а заодно доработаю урок.", True, False),
    "broken": ("Что-то не работает",
               "Давай разберёмся. Что происходит: не открывается приложение, не грузится видео "
               "или что-то ещё? Пришли скрин, если можно, и напиши, с чего смотришь: iPhone, "
               "Android или компьютер.", True, True),
    "other": ("Другое", "Напиши, что мешает, я прочитаю и отвечу.", True, False),
    "soon": ("Да, скоро начну",
             "Отлично! Курс открывается кнопкой «Курс» внизу чата или командой /course. "
             "Приятного просмотра 🎬", False, False),
    "noopen": ("Не открывается",
               "Давай починим. Что ты видишь: белый экран, бесконечную загрузку, ошибку? "
               "Пришли скрин, если можно, и напиши, с чего открываешь: iPhone, Android или "
               "компьютер. Посмотрю сам.", True, True),
    "where": ("Где найти курс?",
              "Курс внизу чата, кнопка «Курс». Или отправь команду /course. "
              "Если не получится, напиши, разберёмся 🙂", False, False),
}
GROUP_CHOICES = {2: ["time", "hard", "broken", "other"], 3: ["soon", "noopen", "where"]}

RATED_HIGH = ("Спасибо, очень приятно! 🙌\n"
              "Напиши пару слов: что зашло больше всего и чего не хватило? "
              "Можно текстом или голосовым.")
RATED_LOW = ("Спасибо за честную оценку.\n"
             "Расскажи, что не понравилось или чего не хватило? Пиши как есть, именно такие "
             "ответы помогают сделать курс лучше.")
PUBLISH_Q = ("Можно указать твоё имя, когда буду публиковать отзыв? "
             "Его увидят те, кто сейчас думает о курсе.")
PUBLISH_YES = "Спасибо! 🤍"
PUBLISH_NO = "Договорились, опубликую без имени. Спасибо ещё раз! 🤍"


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def intro_keyboard(survey_id: int, grp: int) -> InlineKeyboardMarkup:
    if grp == 1:
        return _kb([[(str(n), f"svy:{survey_id}:r:{n}") for n in range(1, 6)],
                    [(str(n), f"svy:{survey_id}:r:{n}") for n in range(6, 11)]])
    return _kb([[(CHOICES[c][0], f"svy:{survey_id}:c:{c}")] for c in GROUP_CHOICES[grp]])


# ── База ───────────────────────────────────────────────────────────────────────

async def _create(user_id: int, grp: int, test: bool) -> int:
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO course_survey (user_id, grp, test) VALUES (?, ?, ?)",
            (user_id, grp, 1 if test else 0))
        await conn.commit()
        return cur.lastrowid


async def _get(survey_id: int) -> dict | None:
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM course_survey WHERE id = ?", (survey_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def _active(user_id: int) -> dict | None:
    """Опрос, на который человек сейчас отвечает текстом (за последние 14 дней)."""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """SELECT * FROM course_survey WHERE user_id = ? AND stage = 'await'
                 AND sent_at >= datetime('now', '-14 days') ORDER BY id DESC LIMIT 1""",
            (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def _update(survey_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(f"UPDATE course_survey SET {cols} WHERE id = ?",
                           (*fields.values(), survey_id))
        await conn.commit()


async def audience() -> dict[int, list[dict]]:
    """Покупатели курса (без тестовых и отозванных доступов) по группам."""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """SELECT a.user_id, u.username, u.first_name,
                      (SELECT COUNT(*) FROM (SELECT DISTINCT section, lesson
                         FROM course_lesson_views v WHERE v.user_id = a.user_id)) AS lessons
                 FROM (SELECT DISTINCT user_id FROM course_access
                        WHERE revoked_at IS NULL AND COALESCE(test, 0) = 0) a
                 LEFT JOIN users u ON u.user_id = a.user_id""") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    groups: dict[int, list[dict]] = {1: [], 2: [], 3: []}
    for r in rows:
        groups[group_for(r["lessons"] or 0)].append(r)
    return groups


# ── Отправка ───────────────────────────────────────────────────────────────────

async def send(bot: Bot, user_id: int, grp: int, test: bool = False) -> int:
    user = await db.get_user(user_id) or {}
    survey_id = await _create(user_id, grp, test)
    await bot.send_message(user_id, f"{_hi(user.get('first_name'))}\n\n{INTRO[grp]}",
                           parse_mode="HTML", reply_markup=intro_keyboard(survey_id, grp))
    return survey_id


# ── Уведомления админам ────────────────────────────────────────────────────────

def _who(user) -> str:
    username = f"@{user.username}" if user.username else f"id:{user.id}"
    return f"{escape(user.first_name or '—')} {username} (id:{user.id})"


def _title(s: dict) -> str:
    test = "🧪 ТЕСТ · " if s["test"] else ""
    return f"📝 <b>{test}Опрос по курсу</b> · группа {s['grp']} ({GROUPS[s['grp']]})"


def _recipients(s: dict, user_id: int) -> list[int]:
    # Тест — только тому, кто проходит, остальных админов не дёргаем
    return [user_id] if s["test"] and user_id in config.ADMIN_IDS else list(config.ADMIN_IDS)


async def _notify(s: dict, user, line: str) -> None:
    from handlers.support import notify_admins_text
    text = f"{_title(s)}\n👤 {_who(user)}\n{line}"
    if s["test"]:
        bot = Bot(token=config.WAITLIST_BOT_TOKEN)
        try:
            for admin_id in _recipients(s, user.id):
                await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"survey: уведомление не ушло: {e}")
        finally:
            await bot.session.close()
        return
    await notify_admins_text(text, user.id)


# ── Кнопки ─────────────────────────────────────────────────────────────────────

async def _own(callback: CallbackQuery) -> tuple[dict | None, str, str]:
    _, sid, kind, value = (callback.data.split(":") + ["", "", "", ""])[:4]
    s = await _get(int(sid)) if sid.isdigit() else None
    if not s or s["user_id"] != callback.from_user.id:
        await callback.answer()
        return None, kind, value
    return s, kind, value


@router.callback_query(F.data.startswith("svy:"))
async def on_button(callback: CallbackQuery):
    s, kind, value = await _own(callback)
    if not s:
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if kind == "r" and value.isdigit():
        rating = int(value)
        await _update(s["id"], rating=rating)
        await _notify(s, callback.from_user, f"⭐️ Оценка: <b>{rating}</b> из 10")
        if s["stage"] == "await":
            await callback.message.answer(RATED_HIGH if rating >= 8 else RATED_LOW)
    elif kind == "c" and value in CHOICES:
        label, reply, wait_text, alarm = CHOICES[value]
        await _update(s["id"], reason=value, **({} if wait_text else {"stage": "done"}))
        await _notify(s, callback.from_user,
                      f"{'🚨' if alarm else '👆'} Нажал(а): <b>{escape(label)}</b>")
        await callback.message.answer(reply)
    elif kind == "p" and value in ("1", "0"):
        await _update(s["id"], publish=int(value), stage="done",
                      done_at=_now_sql())
        await _notify(s, callback.from_user,
                      "✅ Можно публиковать с именем" if value == "1" else "🙈 Публиковать без имени")
        await callback.message.answer(PUBLISH_YES if value == "1" else PUBLISH_NO)


def _now_sql() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── Ответ текстом ──────────────────────────────────────────────────────────────

_ONLY_RATING = re.compile(r"\s*(10|[1-9])\s*(?:/\s*10|из\s*10)?\s*[.!)]*\s*", re.I)
_RATING_IN_TEXT = re.compile(r"(?<!\d)(10|[1-9])\s*(?:/|из)\s*10(?!\d)", re.I)


async def on_reply(message: Message) -> bool:
    """Свободное сообщение — ответ на опрос? Тогда принимаем и возвращаем True."""
    s = await _active(message.from_user.id)
    if not s:
        return False
    text = message.text or message.caption or ""

    if s["grp"] == 1 and s["rating"] is None and message.text and _ONLY_RATING.fullmatch(text):
        # Прислал только цифру — это оценка, отзыв ещё впереди
        rating = int(_ONLY_RATING.fullmatch(text).group(1))
        await _update(s["id"], rating=rating)
        await _notify(s, message.from_user, f"⭐️ Оценка: <b>{rating}</b> из 10")
        await message.answer(RATED_HIGH if rating >= 8 else RATED_LOW)
        return True

    fields = {"answer": text or f"[{message.content_type}]", "answered_at": _now_sql()}
    if s["grp"] == 1 and s["rating"] is None:
        m = _RATING_IN_TEXT.search(text)
        if m:
            fields["rating"] = int(m.group(1))
    fields["stage"] = "publish" if s["grp"] == 1 else "done"
    await _update(s["id"], **fields)
    s.update(fields)

    from handlers.support import client_header, notify_admins
    extra = []
    if s.get("rating"):
        extra.append(f"⭐️ Оценка: <b>{s['rating']}</b> из 10")
    if s.get("reason"):
        extra.append(f"👆 Кнопка: {escape(CHOICES[s['reason']][0])}")
    header = client_header(message, _title(s), "\n".join(extra))
    await notify_admins(message, header, message.from_user.id,
                        recipient_ids=_recipients(s, message.from_user.id))

    name = escape(message.from_user.first_name or "")
    if s["grp"] == 1:
        await message.answer(f"Спасибо, {name}! 🙏 Это правда ценно.", parse_mode="HTML")
        await message.answer(PUBLISH_Q, reply_markup=_kb([[
            ("Да", f"svy:{s['id']}:p:1"), ("Нет", f"svy:{s['id']}:p:0")]]))
    else:
        await message.answer(f"Спасибо, {name}! 🙏 Получил, посмотрю.", parse_mode="HTML")
    return True


# ── Админский бот: /survey ─────────────────────────────────────────────────────

@admin_router.message(Command("survey"))
async def cmd_survey(message: Message, command: CommandObject):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    args = (command.args or "").split()
    if args[:1] == ["test"]:
        grps = [int(a) for a in args[1:] if a in ("1", "2", "3")] or [1, 2, 3]
        bot = Bot(token=config.BOT_TOKEN)
        try:
            for g in grps:
                await send(bot, message.from_user.id, g, test=True)
        except Exception as e:
            await message.answer(f"⚠️ Не отправилось: {escape(str(e))}", parse_mode="HTML")
            return
        finally:
            await bot.session.close()
        await message.answer(
            f"🧪 Прислал в основной бот опрос для групп {', '.join(map(str, grps))}. "
            "Ответы придут только тебе.")
        return

    groups = await audience()
    lines = ["📝 <b>Опрос покупателей курса</b>", ""]
    for g, people in groups.items():
        lines.append(f"Группа {g} — {GROUPS[g]}: <b>{len(people)}</b>")
    lines += ["", "Проверить тексты: <code>/survey test</code> (или <code>/survey test 2</code>)"]
    await message.answer("\n".join(lines), parse_mode="HTML")
