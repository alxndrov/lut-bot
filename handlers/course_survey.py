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


async def cancel_open_tests(user_id: int) -> int:
    """Закрывает незаконченные тестовые опросы — новый тест начинается с чистого листа."""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cur = await conn.execute(
            """UPDATE course_survey SET stage = 'cancelled'
                WHERE user_id = ? AND test = 1 AND stage IN ('await', 'publish')""", (user_id,))
        await conn.commit()
        return cur.rowcount


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


# ── Карточка для админов ───────────────────────────────────────────────────────
# Одна на опрос у каждого админа: оценка, кнопка, ответ и публикация собираются
# в одном сообщении. Новое важное (оценка, кнопка, текст) — карточка приходит
# заново, а старая удаляется: так есть уведомление. Ответ про публикацию —
# тихая правка той же карточки.

def who_line(first_name: str | None, username: str | None, user_id: int) -> str:
    handle = f"@{username}" if username else f"id:{user_id}"
    return f"{escape(first_name or '—')} {handle} (id:{user_id})"


def _who(user) -> str:
    return who_line(user.first_name, user.username, user.id)


def _title(s: dict) -> str:
    test = "🧪 ТЕСТ · " if s["test"] else ""
    return f"📝 <b>{test}Опрос по курсу</b> · группа {s['grp']} ({GROUPS[s['grp']]})"


# Вопросы — коротко, для карточек админам: видно, на что человек отвечает
Q_FIRST = {1: "Как тебе курс? Оценка 1–10 и что зашло, чего не хватило",
           2: "Что мешает продолжить?",
           3: "Получилось открыть курс?"}
Q_RATED = {True: "Что зашло больше всего и чего не хватило?",
           False: "Что не понравилось или чего не хватило?"}
Q_FOLLOW = {"time": "Что сделало бы курс удобнее?",
            "hard": "На каком уроке стало сложно и что непонятно?",
            "broken": "Что не работает и с какого устройства?",
            "other": "Что мешает?",
            "noopen": "Что видно при открытии и с какого устройства?"}
Q_PUBLISH = "Можно указать имя при публикации отзыва?"
ANSWER_MAX = 3000   # карточка целиком не длиннее 4096 символов


def q_line(question: str, answer_html: str) -> str:
    return f"❓ {escape(question)}\n→ {answer_html}"


def answered_question(s: dict) -> str:
    """На какой вопрос отвечает текст — по состоянию опроса ДО этого сообщения."""
    if s["grp"] == 1:
        return Q_RATED[s["rating"] >= 8] if s.get("rating") else Q_FIRST[1]
    return Q_FOLLOW.get(s.get("reason") or "", Q_FIRST[s["grp"]])


def render_card(s: dict, who: str) -> str:
    lines = [_title(s), f"👤 {who}"]
    if s["grp"] == 1:
        if s.get("rating") and not s.get("rating_in_text"):
            lines.append(q_line(Q_FIRST[1], f"⭐️ <b>{s['rating']}</b> из 10"))
    elif s.get("reason"):
        label, _, _, alarm = CHOICES[s["reason"]]
        lines.append(q_line(Q_FIRST[s["grp"]],
                            f"{'🚨' if alarm else '👆'} <b>{escape(label)}</b>"))
    if s.get("answer"):
        answer = s["answer"]
        if len(answer) > ANSWER_MAX:
            answer = answer[:ANSWER_MAX] + "…"
        lines.append(f"❓ <b>{escape(s.get('answer_q') or answered_question(s))}</b>")
        if s.get("rating_in_text") and s.get("rating"):
            lines.append(f"⭐️ Оценка в тексте: <b>{s['rating']}</b> из 10")
        lines.append(f"💬 {escape(answer)}")
    if s.get("publish") is not None:
        lines.append(q_line(Q_PUBLISH, "✅ Да, с именем" if s["publish"] else "🙈 Нет, без имени"))
    elif s["grp"] == 1 and s.get("stage") == "publish":
        lines.append(q_line(Q_PUBLISH, "⏳ пока не ответил(а)"))
    return "\n".join(lines)


def _recipients(s: dict) -> list[int]:
    # Тест — только тому, кто проходит, остальных админов не дёргаем
    uid = s["user_id"]
    return [uid] if s["test"] and uid in config.ADMIN_IDS else list(config.ADMIN_IDS)


async def _card_ids(survey_id: int) -> dict[int, int]:
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute("SELECT chat_id, message_id FROM course_survey_cards "
                                "WHERE survey_id = ?", (survey_id,)) as cur:
            return {r[0]: r[1] for r in await cur.fetchall()}


async def _save_card(survey_id: int, chat_id: int, message_id: int) -> None:
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("INSERT OR REPLACE INTO course_survey_cards "
                           "(survey_id, chat_id, message_id) VALUES (?, ?, ?)",
                           (survey_id, chat_id, message_id))
        await conn.commit()


async def _show(s: dict, who: str, resend: bool = True, media: Message | None = None) -> None:
    """Показывает админам карточку опроса в текущем состоянии.

    resend — прислать заново (с уведомлением) и удалить прежнюю; иначе тихо
    поправить прежнюю. media — вложение из ответа: идёт копией следом."""
    from handlers.support import relay_media
    text = render_card(s, who)
    kb = None if s["test"] else InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="✍️ Ответить", callback_data=f"sup_reply:{s['user_id']}")]])
    old = await _card_ids(s["id"])
    bot = Bot(token=config.WAITLIST_BOT_TOKEN)
    try:
        for admin_id in _recipients(s):
            try:
                mid = old.get(admin_id)
                if mid and not resend:
                    try:
                        await bot.edit_message_text(text=text, chat_id=admin_id, message_id=mid,
                                                    parse_mode="HTML", reply_markup=kb)
                        continue
                    except Exception as e:
                        if "not modified" in str(e):
                            continue
                        logger.warning(f"survey: карточку не поправить ({e}) — шлю заново")
                sent = await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=kb)
                await _save_card(s["id"], admin_id, sent.message_id)
                if not s["test"]:   # ответ Reply на карточку уходит клиенту
                    await db.add_support_message(admin_id, sent.message_id, s["user_id"])
                if mid:
                    try:
                        await bot.delete_message(admin_id, mid)
                    except Exception:
                        pass
                if media is not None:
                    copy = await relay_media(media, bot, admin_id)
                    if copy and not s["test"]:
                        await db.add_support_message(admin_id, copy.message_id, s["user_id"])
            except Exception as e:
                logger.error(f"survey: карточка админу {admin_id} не ушла: {e}")
    finally:
        await bot.session.close()


# ── Кнопки ─────────────────────────────────────────────────────────────────────

async def _own(callback: CallbackQuery) -> tuple[dict | None, str, str]:
    _, sid, kind, value = (callback.data.split(":") + ["", "", "", ""])[:4]
    s = await _get(int(sid)) if sid.isdigit() else None
    if not s or s["user_id"] != callback.from_user.id:
        await callback.answer()
        return None, kind, value
    if s["stage"] == "cancelled":
        await callback.answer("Этот опрос уже закрыт.")
        return None, kind, value
    return s, kind, value


async def _next_test(bot: Bot, s: dict) -> None:
    """Тестовый прогон — по одной группе: закончил эту, приходит следующая."""
    if not s["test"] or s["grp"] >= 3:
        if s["test"]:
            await bot.send_message(s["user_id"], "🧪 Тест закончен: все три группы пройдены.")
        return
    grp = s["grp"] + 1
    await bot.send_message(s["user_id"], f"🧪 Дальше — группа {grp}: {GROUPS[grp]}.")
    await send(bot, s["user_id"], grp, test=True)


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
        await _update(s["id"], rating=rating, rating_in_text=0)
        s.update(rating=rating, rating_in_text=0)
        await _show(s, _who(callback.from_user))
        if s["stage"] == "await":
            await callback.message.answer(RATED_HIGH if rating >= 8 else RATED_LOW)
    elif kind == "c" and value in CHOICES:
        label, reply, wait_text, alarm = CHOICES[value]
        upd = {"reason": value, **({} if wait_text else {"stage": "done"})}
        await _update(s["id"], **upd)
        s.update(upd)
        await _show(s, _who(callback.from_user))
        await callback.message.answer(reply)
        if not wait_text:
            await _next_test(callback.bot, s)
    elif kind == "p" and value in ("1", "0"):
        upd = {"publish": int(value), "stage": "done", "done_at": _now_sql()}
        await _update(s["id"], **upd)
        s.update(upd)
        await _show(s, _who(callback.from_user), resend=False)   # тихо, той же карточкой
        await callback.message.answer(PUBLISH_YES if value == "1" else PUBLISH_NO)
        await _next_test(callback.bot, s)


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
        s["rating"] = rating
        await _show(s, _who(message.from_user))
        await message.answer(RATED_HIGH if rating >= 8 else RATED_LOW)
        return True

    fields = {"answer": text or f"[{message.content_type}]", "answered_at": _now_sql(),
              "answer_q": answered_question(s)}
    if s["grp"] == 1 and s["rating"] is None:
        m = _RATING_IN_TEXT.search(text)
        if m:
            fields.update(rating=int(m.group(1)), rating_in_text=1)
    fields["stage"] = "publish" if s["grp"] == 1 else "done"
    await _update(s["id"], **fields)
    s.update(fields)

    await _show(s, _who(message.from_user),
                media=None if message.content_type == "text" else message)

    name = escape(message.from_user.first_name or "")
    if s["grp"] == 1:
        await message.answer(f"Спасибо, {name}! 🙏 Это правда ценно.", parse_mode="HTML")
        await message.answer(PUBLISH_Q, reply_markup=_kb([[
            ("Да", f"svy:{s['id']}:p:1"), ("Нет", f"svy:{s['id']}:p:0")]]))
    else:
        await message.answer(f"Спасибо, {name}! 🙏 Получил, посмотрю.", parse_mode="HTML")
        await _next_test(message.bot, s)
    return True


# ── Админский бот: /survey ─────────────────────────────────────────────────────

@admin_router.message(Command("survey"))
async def cmd_survey(message: Message, command: CommandObject):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    args = (command.args or "").split()
    if args[:1] == ["test"]:
        # По одной группе: следующая придёт, когда эта пройдена (_next_test)
        grp = next((int(a) for a in args[1:] if a in ("1", "2", "3")), 1)
        await cancel_open_tests(message.from_user.id)
        bot = Bot(token=config.BOT_TOKEN)
        try:
            await send(bot, message.from_user.id, grp, test=True)
        except Exception as e:
            await message.answer(f"⚠️ Не отправилось: {escape(str(e))}", parse_mode="HTML")
            return
        finally:
            await bot.session.close()
        await message.answer(
            f"🧪 Прислал в основной бот опрос группы {grp}. Пройдёшь — придёт следующая. "
            "Ответы придут только тебе.")
        return

    groups = await audience()
    lines = ["📝 <b>Опрос покупателей курса</b>", ""]
    for g, people in groups.items():
        lines.append(f"Группа {g} — {GROUPS[g]}: <b>{len(people)}</b>")
    lines += ["", "Проверить тексты: <code>/survey test</code> (или <code>/survey test 2</code>)"]
    await message.answer("\n".join(lines), parse_mode="HTML")
