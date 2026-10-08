"""
Кнопки заказа в админском боте (malimadmins / WAITLIST_BOT_TOKEN):
«Распечатал»/«Собрал», «Заказ отправил», повтор и возврат.
Этот бот слушает только callback_query — отдельным поллингом в bot.py.
"""
import asyncio
import html
import json
import logging
import re
from datetime import datetime, timedelta

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)

import config
import database as db
from services.gsheets import request_sync, request_finance_printer_update

router = Router()
logger = logging.getLogger(__name__)

# Строки последнего /myorders: заказ → [(чат, сообщение, номер, есть ли карточка)].
# Нужны, чтобы при смене статуса обновлять не только карточку заказа,
# но и пункт списка. Живёт до перезапуска — список легко перезапросить.
_LIST_ITEMS: dict[int, list[tuple[int, int, int, bool, str]]] = {}

def _order_positions(order: dict) -> int:
    """Сколько позиций в заказе: по разметке печати, иначе по ответам клиента."""
    whos = db.order_routing(order)
    if whos:
        return len(whos)
    try:
        rounds = json.loads(order.get("rounds_json") or "[]")
        return max(1, len(rounds))
    except Exception:
        return 1


def _admin_id_by_name(name: str | None) -> int | None:
    """Ник из разметки → id админа. Синхронно, по кэшу ников.

    Ник в настройке пишется руками, поэтому допускаем опечатку в букве
    на конце — так же, как db.get_admin_by_username.
    """
    clean = (name or "").strip().lstrip("@").lower()
    if not clean:
        return None
    for uid, nick in _ADMIN_NAMES.items():
        n = nick.lstrip("@").lower()
        if n == clean or n.startswith(clean) or clean.startswith(n):
            return uid
    return None


# Товары «в наличии» (готовые микрофоны и т.п.) не печатают, а собирают:
# для их позиций этап называется «Собрал», а не «Распечатал». Отметка та же
# (order_prints) — меняются только слова. Флаг «в наличии» меняют в карточке
# товара, поэтому список перечитываем при каждом обращении к заказам.
_STOCK_IDS: set[int] = set()

_PRINT_WORDS = {"icon": "🖨", "did": "Распечатал", "do": "Напечатать",
                "pos_done": "напечатана", "done": "распечатан", "all_done": "Распечатано",
                "wait": "Ждём печать", "doing": "печатает", "doing_you": "печатаешь"}
_ASSEMBLY_WORDS = {"icon": "🛠", "did": "Собрал", "do": "Собрать",
                   "pos_done": "собрана", "done": "собран", "all_done": "Собрано",
                   "wait": "Ждём сборку", "doing": "собирает", "doing_you": "собираешь"}
# Заказ, где одни позиции печатают, а другие собирают
_MIXED_WORDS = dict(_PRINT_WORDS, icon="✅", done="готов", all_done="Готово",
                    wait="Ждём", doing="делает", doing_you="делаешь")


async def _refresh_stock_ids():
    ids = await db.get_stock_product_ids()
    _STOCK_IDS.clear()
    _STOCK_IDS.update(ids)


def _words(order: dict, positions=None) -> dict:
    """Слова этапа для этих позиций (по умолчанию — всего заказа)."""
    total = _order_positions(order)
    rp = _round_products(order, total)
    positions = list(positions or range(1, total + 1))
    stock = [p - 1 < len(rp) and rp[p - 1] in _STOCK_IDS for p in positions]
    if all(stock):
        return _ASSEMBLY_WORDS
    return _PRINT_WORDS if not any(stock) else _MIXED_WORDS


def _my_positions(order: dict, viewer_id: int | None) -> list[int]:
    """Номера позиций, которые печатает этот админ."""
    if not viewer_id:
        return []
    return [i + 1 for i, w in enumerate(db.order_routing(order))
            if _admin_id_by_name(w) == viewer_id]


def _print_map(order: dict, prints: list) -> dict[int, dict]:
    """{номер позиции: отметка о печати}.

    Отметка без позиций — из времён, когда печать отмечалась целиком;
    считаем закрытыми все позиции её автора.
    """
    total = _order_positions(order)
    whos = db.order_routing(order)
    out: dict[int, dict] = {}
    for p in prints:
        positions = db.print_positions(p)
        if not positions:
            positions = {i + 1 for i, w in enumerate(whos)
                         if _admin_id_by_name(w) == p["user_id"]}
            positions = positions or set(range(1, total + 1))
        for pos in positions:
            out.setdefault(pos, p)
    return out


def _all_printed(order: dict, prints: list) -> bool:
    total = _order_positions(order)
    printed = _print_map(order, prints)
    return all(pos in printed for pos in range(1, total + 1))


def order_assigned_keyboard(order: dict, viewer_id: int | None = None,
                            prints: list | None = None) -> InlineKeyboardMarkup:
    """Основные кнопки заказа: распечатать (собрать) → отправить.

    В заказе из нескольких позиций печать отмечается по каждой отдельно —
    даже когда все позиции печатает один человек: он делает их не разом.
    """
    oid = order["id"]
    total = _order_positions(order)
    printed = _print_map(order, prints or [])

    rows = []
    if total > 1:
        # Свои позиции; если смотрящий не печатает (например, только
        # исполнитель) — показываем все, иначе отметить будет некому
        show = _my_positions(order, viewer_id) or list(range(1, total + 1))
        for pos in show:
            w = _words(order, [pos])
            if pos in printed:
                rows.append([InlineKeyboardButton(
                    text=f"✅ Поз.{pos} {w['pos_done']} — отменить",
                    callback_data=f"order_unprint:{oid}:{pos}")])
            else:
                rows.append([InlineKeyboardButton(
                    text=f"{w['icon']} {w['did']} поз.{pos}",
                    callback_data=f"order_printed:{oid}:{pos}")])
    elif printed:
        rows.append([InlineKeyboardButton(text=f"↩️ Отменить «{_words(order)['did'].lower()}»",
                                          callback_data=f"order_unprint:{oid}:1")])
    else:
        w = _words(order)
        rows.append([InlineKeyboardButton(text=f"{w['icon']} {w['did']}",
                                          callback_data=f"order_printed:{oid}:1")])

    # Наклейка СДЭК — когда печатать больше нечего и пора клеить на коробку.
    # Без накладной печатать нечего: её заводят вручную в кабинете
    if order.get("cdek_uuid") and all(p in printed for p in range(1, total + 1)):
        rows.append([InlineKeyboardButton(text="🏷 Штрихкод СДЭК",
                                          callback_data=f"order_barcode:{oid}")])

    # «Заказ отправил» — следующий шаг: появляется, когда всё распечатано
    # (собрано), чтобы не отметить отправку раньше времени
    if all(p in printed for p in range(1, total + 1)):
        rows.append([InlineKeyboardButton(text="📦 Заказ отправил",
                                          callback_data=f"order_shipped:{oid}")])
    # «Повторить заказ» — только у отправленного (см. order_shipped_keyboard)
    from handlers.refunds import refund_rows
    rows += refund_rows(order)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _msk(ts: str | None) -> str:
    """'2026-07-27 12:45:14' (UTC) → '27.07 15:45'."""
    if not ts:
        return ""
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S") + timedelta(hours=3)
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return ""


# Разбор строки печати из текста заказа живёт в database — им пользуется
# и расчёт долей, которому до обработчиков дела нет
parse_routing_line = db.parse_routing_line


def _without_routing_line(summary: str) -> str:
    """Убирает строку «🖨 Печатает: …» — она осталась в тексте старых заказов,
    а исполнитель и так виден в строке «Взял в работу»."""
    return "\n".join(ln for ln in (summary or "").split("\n")
                     if not ln.startswith("🖨 <b>Печат"))


def _order_text(order: dict, prints: list | None = None) -> str:
    text = _without_routing_line(order["summary"])
    if order.get("repeat_of_order_id") and order.get("repeat_created_by_name"):
        text += ("\n🔁 <b>Повтор оформил:</b> "
                 + html.escape(order["repeat_created_by_name"]))
    if order.get("assignee_name"):
        text += f"\n\n🧑‍🔧 <b>Взял в работу:</b> {order['assignee_name']}"

    prints = prints or []
    total = _order_positions(order)
    if total > 1:
        # Несколько позиций: печать отмечается по каждой, показываем по каждой
        printed = _print_map(order, prints)
        whos = db.order_routing(order)

        def _pos_line(pos: int) -> str:
            who = whos[pos - 1] if pos <= len(whos) else None
            mark = printed.get(pos)
            if mark:
                when = _msk(mark["printed_at"])
                return f"Поз.{pos} — {mark['user_name']}" + (f" · {when}" if when else "")
            return f"Поз.{pos} — {who or 'не определён'}"

        done = [p for p in range(1, total + 1) if p in printed]
        waiting = [p for p in range(1, total + 1) if p not in printed]
        w = _words(order)
        text += f"\n{w['icon']} <b>{w['all_done']}:</b> " + (
            " · ".join(_pos_line(p) for p in done) if done else "—")
        if waiting:
            text += (f"\n⏳ <b>{_words(order, waiting)['wait']}:</b> "
                     + " · ".join(_pos_line(p) for p in waiting))
    elif order.get("printed_at"):
        when = _msk(order["printed_at"])
        w = _words(order)
        text += f"\n{w['icon']} <b>{w['did']}:</b> {order.get('printed_by_name') or ''}"
        if when:
            text += f" · {when}"
    if order.get("shipped_at"):
        when = _msk(order["shipped_at"])
        text += f"\n📦 <b>Отправлен:</b> {order.get('shipped_by_name') or ''}"
        if when:
            text += f" · {when}"
    if order.get("cdek_number"):
        text += f"\n📦 <b>Трек-номер СДЭК:</b> <code>{order['cdek_number']}</code>"
    from handlers.refunds import refund_line
    text += refund_line(order)
    return text


def order_shipped_keyboard(order_id: int, order: dict | None = None) -> InlineKeyboardMarkup:
    """Заказ отправлен — можно откатить отметку, оформить повтор или возврат."""
    from handlers.refunds import refund_rows
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Отменить отметку об отправке",
                              callback_data=f"order_unship:{order_id}")],
        [InlineKeyboardButton(text="🔁 Повторить заказ",
                              callback_data=f"order_repeat:{order_id}")],
        *(refund_rows(order) if order else []),
    ])


def order_repeat_confirm_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, создать бесплатный повтор",
                              callback_data=f"order_repeat_confirm:{order_id}")],
        [InlineKeyboardButton(text="◀️ Отмена",
                              callback_data=f"order_repeat_cancel:{order_id}")],
    ])


# Ники админов для строки «ждём печать». Заполняется при первой синхронизации.
_ADMIN_NAMES: dict[int, str] = {}


async def _ensure_admin_names():
    await _refresh_stock_ids()
    if _ADMIN_NAMES:
        return
    for uid in config.ADMIN_IDS:
        admin = await db.get_admin_by_id(uid)
        if admin and admin.get("username"):
            _ADMIN_NAMES[uid] = "@" + admin["username"]


def _printer_name(order: dict, uid: int) -> str:
    """Имя печатающего — из кэша ников либо из подписи исполнителя."""
    if uid in _ADMIN_NAMES:
        return _ADMIN_NAMES[uid]
    if uid == order.get("assignee_id") and order.get("assignee_name"):
        return order["assignee_name"]
    return f"id:{uid}"


def _order_keyboard(order: dict, viewer_id: int | None = None,
                    prints: list | None = None) -> InlineKeyboardMarkup:
    """Кнопки заказа. «Взял заказ» нет: исполнителем становится тот,
    кто первым отметит печать или отправку.

    Кнопки печати — по позициям смотрящего: в разделённом заказе одну
    позицию могли отпечатать, а другую ещё нет.
    """
    if order.get("shipped_at"):
        return order_shipped_keyboard(order["id"], order)
    if order.get("refund_full"):
        # Полный возврат до отправки — заказ отменён, печатать нечего
        from handlers.refunds import refund_rows
        return InlineKeyboardMarkup(inline_keyboard=refund_rows(order))
    return order_assigned_keyboard(order, viewer_id, prints)


def _pos_list(positions: list) -> str:
    """[1,3] -> 'поз.1, поз.3'."""
    return ", ".join(f"поз.{p}" for p in positions)


_PRODUCT_WORDS = {config.MIC_PRODUCT_ID: "микрофон", config.CASE_PRODUCT_ID: "кейс"}


def _round_products(order: dict, total: int) -> list[int]:
    """Товар каждой позиции заказа — как считает сам бот (см. unpack_round_products)."""
    try:
        rounds = json.loads(order.get("rounds_json") or "[]")
    except Exception:
        rounds = []
    rp = db.unpack_round_products(order.get("round_products_json"), rounds, order["product_id"])
    if not rp:
        rp = [order["product_id"]] * total
    return rp


def _products_label(order: dict, positions: list, total: int) -> str:
    """' (кейс)' / ' (кейс, микрофон)' — какой товар в этих позициях. Два физтовара
    в очереди легко перепутать по одному только "Напечатать"/"Отправить" в списке."""
    rp = _round_products(order, total)
    words = dict.fromkeys(
        _PRODUCT_WORDS[rp[p - 1]] for p in positions
        if p - 1 < len(rp) and rp[p - 1] in _PRODUCT_WORDS
    )
    return f" ({', '.join(words)})" if words else ""


def _order_num(order: dict) -> str:
    """Номер заказа цифрами: malimabi-store-075 → 075.

    В списке важен именно номер, а не полный код: он короткий, его видно
    с одного взгляда и им же заказ называют вслух.
    """
    nums = re.findall(r"\d+", order.get("order_code") or "")
    return nums[-1] if nums else str(order.get("id", ""))


def _list_item_text(order: dict, index: int, has_card: bool,
                    viewer_id: int | None = None, prints: list | None = None,
                    mode: str = "my") -> str:
    """Строка пункта списка — не этап, а что с заказом нужно сделать.

    mode="my"   — действие смотрящего (/myorders);
    mode="all"  — действие того, за кем заказ, с ником, кого ждём (/allorders);
    mode="sent" — архив: когда и кем отправлен (/sentorders).
    """
    prints = prints or []
    total = _order_positions(order)
    printed = _print_map(order, prints)
    waiting = [p for p in range(1, total + 1) if p not in printed]

    if mode == "sent":
        when = _msk(order.get("shipped_at"))
        action = "✅ Отправлено" + _products_label(order, list(range(1, total + 1)), total)
        action += f" · {when}" if when else ""
        if order.get("shipped_by_name"):
            action += f" · {order['shipped_by_name']}"
        if has_card and order.get("cdek_number"):
            action += f"\n📦 СДЭК {order['cdek_number']}"   # без карточки трек и так ниже
    elif order.get("shipped_at"):
        action = "✅ <s>Отправлено</s>" + _products_label(order, list(range(1, total + 1)), total)
    elif not waiting:
        action = "📦 Отправить" + _products_label(order, list(range(1, total + 1)), total)
    elif total == 1:
        w = _words(order)
        action = f"{w['icon']} {w['do']}" + _products_label(order, [1], total)
    elif mode == "my":
        mine = [p for p in _my_positions(order, viewer_id) if p in waiting]
        w = _words(order, mine)
        action = (f"{w['icon']} {w['do']} {_pos_list(mine)}" + _products_label(order, mine, total)
                  if mine else "⏳ Ждём вторую часть")
    else:
        whos = db.order_routing(order)
        by_who: dict[str, list] = {}
        for p in waiting:
            who = (whos[p - 1] if p <= len(whos) else None) or "не определён"
            by_who.setdefault(who, []).append(p)
        action = " · ".join(
            f"{_words(order, ps)['icon']} {_words(order, ps)['do']} {_pos_list(ps)}"
            f"{_products_label(order, ps, total)} — {who}"
            for who, ps in by_who.items())

    if order.get("refunded_at"):
        from handlers.refunds import _rub
        action += f" · 💸 возврат {_rub(order['refund_amount'])}"
    text = f"<b>{index}.</b> №{_order_num(order)} · {action}"
    if not has_card:
        text += f"\n{_order_line(order, prints)}"
    return text


def _log_edit_fail(what: str, where, e: Exception):
    """Не смогли перерисовать сообщение — это надо видеть в логе.

    Карточка тогда врёт: в базе одно, на экране другое. Единственное
    безобидное исключение — «текст не изменился».
    """
    if "not modified" in str(e):
        return
    logger.warning(f"{what} edit failed ({where}): {type(e).__name__}: {e}")


async def _sync_list_items(bot, order: dict, prints: list | None = None):
    """Обновляет пункты /myorders, относящиеся к этому заказу."""
    for chat_id, msg_id, index, has_card, mode in _LIST_ITEMS.get(order["id"], []):
        try:
            await bot.edit_message_text(
                _list_item_text(order, index, has_card, chat_id, prints, mode),
                chat_id=chat_id, message_id=msg_id, parse_mode="HTML",
                reply_markup=(order_shipped_keyboard(order["id"], order)
                              if mode == "sent" else None),
            )
        except Exception as e:
            _log_edit_fail("list item", f"{chat_id}/{msg_id}", e)


async def _sync_order_messages(bot_or_callback, order: dict):
    """Обновляет все копии сообщения заказа у всех админов.

    Принимает и CallbackQuery, и сам Bot — карточку обновляем не только
    по нажатию кнопки, но и после отметки заказов номерами.
    """
    bot = getattr(bot_or_callback, "bot", bot_or_callback)
    await _ensure_admin_names()
    prints = await db.get_order_prints(order["id"])
    text = _order_text(order, prints)
    for m in await db.get_order_messages(order["id"]):
        try:
            # Кнопка своя для каждого: карточка лежит в личке админа,
            # значит chat_id — это он и есть
            await bot.edit_message_text(
                text, chat_id=m["chat_id"], message_id=m["message_id"],
                parse_mode="HTML",
                reply_markup=_order_keyboard(order, m["chat_id"], prints),
            )
        except Exception as e:
            _log_edit_fail("order card", f"{m['chat_id']}/{m['message_id']}", e)
    await _sync_list_items(bot, order, prints)


async def refresh_open_orders(bot):
    """Перерисовывает карточки всех неотправленных заказов при старте.

    Нужно, чтобы у заказов, созданных до появления кнопки «Распечатал»,
    она тоже появилась — иначе старые карточки навсегда остались бы
    со старым набором кнопок.
    """
    import asyncio
    try:
        orders = await db.get_orders(only_unshipped=True)
    except Exception as e:
        logger.error(f"refresh_open_orders: не удалось получить заказы: {e}")
        return
    for o in orders:
        # Заказы до появления routing_json: разметку позиций достаём
        # из текста карточки, иначе позицию нельзя будет передать
        if not db.order_routing(o):
            whos = parse_routing_line(o.get("summary") or "")
            if whos:
                await db.set_order_routing(o["id"], whos)
                o = await db.get_order(o["id"])
        await _sync_order_messages(bot, o)
        await asyncio.sleep(0.15)      # бережём лимиты Telegram
    logger.info(f"Кнопки обновлены у заказов: {len(orders)}")


def _print_positions_of(order: dict, mark: dict) -> set:
    """Какие позиции закрывает отметка (старая, без номеров — всю свою часть)."""
    positions = db.print_positions(mark)
    if positions:
        return positions
    return {pos for pos, m in _print_map(order, [mark]).items()}


def _may_act(order: dict, uid: int) -> bool:
    """Может ли этот админ отмечать этапы: исполнитель или один из печатающих."""
    if not order.get("assignee_id"):
        return True
    return uid == order["assignee_id"] or uid in db.printer_ids(order)


def _actor_name(callback: CallbackQuery) -> str:
    u = callback.from_user
    return f"@{u.username}" if u.username else (u.first_name or f"id:{u.id}")


async def _load(callback: CallbackQuery) -> dict | None:
    """Проверка прав + загрузка заказа."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return None
    order_id = int(callback.data.split(":")[1])
    order = await db.get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return None
    await _refresh_stock_ids()
    return order


def _replacement_summary(source: dict, new_code: str) -> str:
    """Карточка повтора: сохраняет ТЗ, но явно убирает новую оплату."""
    text = source.get("summary") or ""
    old_code = source.get("order_code") or ""
    if old_code:
        text = text.replace(old_code, new_code, 1)
    text = text.replace("🧾 <b>Новый заказ</b>", "🔁 <b>Повтор заказа</b>", 1)
    # Если повторяют уже повтор, заголовок остаётся один, без наслоения.
    if "🔁 <b>Повтор заказа</b>" not in text:
        text = "🔁 <b>Повтор заказа</b> " + html.escape(new_code) + "\n" + text
    text = re.sub(r"(?m)^💵[^\n]*$", "💵 0 ₽ — клиент повторно не платит", text, count=1)
    source_label = html.escape(old_code or f"#{source['id']}")
    note = (
        f"\n\n🔗 <b>Повтор заказа:</b> <code>{source_label}</code>\n"
        "💳 Без повторной оплаты · возврат товара не требуется\n"
        "🚚 Доставку СДЭК оплачиваем мы"
    )
    return text + note


async def _replacement_products(order: dict) -> tuple[list, list[int], dict[int, dict]]:
    try:
        rounds = json.loads(order.get("rounds_json") or "[]")
    except Exception:
        rounds = []
    round_products = db.unpack_round_products(
        order.get("round_products_json"), rounds, order.get("product_id"))
    if not round_products:
        round_products = [order["product_id"]]
    products = {}
    for pid in set(round_products):
        product = await db.get_product(pid)
        if product:
            products[pid] = product
    return rounds, round_products, products


async def _create_replacement_cdek(order_id: int, pending: dict,
                                   round_products: list[int], products: dict,
                                   order_code: str, client_id: int):
    """Держит отдельную сессию клиентского бота до ответа СДЭК."""
    from handlers.prodamus_webhook import _create_cdek_order
    main_bot = Bot(token=config.BOT_TOKEN)
    try:
        await _create_cdek_order(
            order_id, pending, round_products, products, order_code,
            bot=main_bot, client_id=client_id,
        )
    finally:
        await main_bot.session.close()


def _stage(order: dict, prints: list | None = None) -> tuple[str, str]:
    """Значок и подпись текущего этапа заказа."""
    if order.get("shipped_at"):
        return "📦", "отправлен"
    if order.get("printed_at"):
        w = _words(order)
        return w["icon"], w["done"]
    total = _order_positions(order)
    if total > 1 and prints:
        printed = _print_map(order, prints)
        waiting_pos = [p for p in range(1, total + 1) if p not in printed]
        if printed and waiting_pos:
            whos = db.order_routing(order)
            waiting = ", ".join(
                f"поз.{p}" + (f" ({whos[p - 1]})" if p <= len(whos) and whos[p - 1] else "")
                for p in waiting_pos)
            return _words(order)["icon"], f"часть готова, ждём {waiting}"
    if order.get("assignee_id"):
        return "🧑‍🔧", "в работе"
    return "🆕", "новый"


def _order_line(order: dict, prints: list | None = None) -> str:
    icon, stage = _stage(order, prints)
    client = f"@{order['username']}" if order.get("username") else (
        order.get("first_name") or f"id:{order.get('user_id')}")
    when = _msk(order.get("created_at"))
    line = f"{icon} {stage}\n    {client}"
    if order.get("product_name"):
        line += f" · {order['product_name']}"
    if when:
        line += (f" · повтор создан {when}" if order.get("repeat_of_order_id")
                 else f" · оплачен {when}")
    if order.get("cdek_number"):
        line += f"\n    📦 СДЭК {order['cdek_number']}"
    return line


def _forget_lists(chat_id: int):
    """Прошлый список в этом чате больше не обновляем — он устарел."""
    for oid in list(_LIST_ITEMS):
        kept = [it for it in _LIST_ITEMS[oid] if it[0] != chat_id]
        if kept:
            _LIST_ITEMS[oid] = kept
        else:
            _LIST_ITEMS.pop(oid)


async def _cards_in_chat(orders: list, chat_id: int) -> dict:
    """Где лежит карточка каждого заказа в этом чате."""
    cards = {}
    for o in orders:
        for m in await db.get_order_messages(o["id"]):
            if m["chat_id"] == chat_id:
                cards[o["id"]] = m["message_id"]
                break
    return cards


async def _send_list(message: Message, orders: list, start_index: int,
                     cards: dict, mode: str) -> tuple[int, list[int]]:
    """Шлёт пункты списка ответами на карточки заказов. Возвращает след. номер and message ids."""
    await _refresh_stock_ids()
    i = start_index
    sent_ids = []
    for o in orders:
        card_id = cards.get(o["id"])
        prints = await db.get_order_prints(o["id"])
        sent = await message.answer(
            _list_item_text(o, i, bool(card_id), message.from_user.id, prints, mode),
            parse_mode="HTML", reply_to_message_id=card_id,
            reply_markup=(order_shipped_keyboard(o["id"], o)
                          if mode == "sent" else None),
        )
        sent_ids.append(sent.message_id)
        _LIST_ITEMS.setdefault(o["id"], []).append(
            (message.chat.id, sent.message_id, i, bool(card_id), mode)
        )
        i += 1
    return i, sent_ids


async def _delete_previous_command_batch(message: Message, command: str):
    chat_id = message.chat.id
    user_id = message.from_user.id
    for msg_id in await db.get_admin_command_messages(command, chat_id, user_id):
        try:
            await message.bot.delete_message(chat_id, msg_id)
        except Exception as e:
            logger.debug(f"{command}: cannot delete {chat_id}/{msg_id}: {type(e).__name__}: {e}")
    await db.clear_admin_command_messages(command, chat_id, user_id)
    _forget_lists(chat_id)
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"{command}: cannot delete command message {chat_id}/{message.message_id}: {type(e).__name__}: {e}")


@router.message(Command("refresh"))
async def cmd_refresh(message: Message):
    """Перерисовать карточки заказов в работе.

    Правка сообщения может не пройти (перезапуск бота, сбой сети) — тогда
    в базе одно, а на карточке другое. Эта команда приводит их в согласие.
    """
    if message.from_user.id not in config.ADMIN_IDS:
        return
    await refresh_open_orders(message.bot)
    orders = await db.get_orders(only_unshipped=True)
    await message.answer(f"🔄 Карточки обновлены: {len(orders)}")


SENT_LIST_LIMIT = 15


@router.message(Command("sentorders"))
async def cmd_sent_orders(message: Message):
    """Архив отправленных — свежие сверху, ответами на карточки заказов."""
    if message.from_user.id not in config.ADMIN_IDS:
        return
    await _ensure_admin_names()

    orders = [o for o in await db.get_orders() if o.get("shipped_at")]
    if not orders:
        await message.answer("📦 Отправленных заказов пока нет")
        return
    orders.sort(key=lambda o: o.get("shipped_at") or "", reverse=True)
    shown = orders[:SENT_LIST_LIMIT]

    head = f"📦 <b>Отправленные заказы — {len(orders)}</b>"
    if len(shown) < len(orders):
        head += f"\nПоказываю последние {len(shown)}"
    await message.answer(head, parse_mode="HTML")

    _forget_lists(message.chat.id)
    cards = await _cards_in_chat(shown, message.chat.id)
    await _send_list(message, shown, 1, cards, mode="sent")


# /myorders и /allorders — прежние имена той же команды. Печатает теперь
# один человек, делить список «мои/все» стало не на что, но старые имена
# оставлены рабочими: они разосланы в переписке и висят в привычке.
@router.message(Command("orders", "myorders", "allorders"))
async def cmd_orders(message: Message):
    """Заказы в работе — ответами на исходные карточки.

    Сам заказ повторно не пересказываем: Telegram покажет процитированную
    карточку, а мы дописываем только номер и что с ним делать.
    """
    if message.from_user.id not in config.ADMIN_IDS:
        return

    await _delete_previous_command_batch(message, "orders")
    todo = await db.get_orders(only_unshipped=True)
    # Старые сверху — обрабатываем по очереди поступления
    todo.sort(key=lambda o: o.get("created_at") or "")

    if not todo:
        sent = await message.answer("📋 Заказов в работе нет — всё разослано 🎉")
        await db.replace_admin_command_messages("orders", message.chat.id, message.from_user.id,
                                                [sent.message_id])
        return

    head = await message.answer(f"📋 <b>Заказы в работе: {len(todo)}</b>", parse_mode="HTML")

    cards = await _cards_in_chat(todo, message.chat.id)
    _, sent_ids = await _send_list(message, todo, 1, cards, mode="my")
    await db.replace_admin_command_messages("orders", message.chat.id, message.from_user.id,
                                            [head.message_id, *sent_ids])



@router.callback_query(F.data.startswith("order_take:"))
async def cb_order_take(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return

    u = callback.from_user
    claimed = await db.set_order_assignee(order["id"], u.id, _actor_name(callback))
    request_sync()
    order = await db.get_order(order["id"])  # перечитываем с исполнителем

    if claimed:
        await callback.answer("Взято в работу ✅")
    elif order["assignee_id"] == u.id:
        await callback.answer("Этот заказ уже за тобой ✅")
    else:
        await callback.answer(f"Заказ уже взял {order['assignee_name']}", show_alert=True)

    await _sync_order_messages(callback, order)


def _target_positions(callback: CallbackQuery, order: dict, uid: int) -> set:
    """Каких позиций касается нажатие.

    Номер приходит в callback_data. Старые карточки в чате шлют кнопку без
    номера — тогда берём все свои позиции, а если их нет, весь заказ.
    """
    parts = callback.data.split(":")
    if len(parts) > 2 and parts[2].isdigit():
        return {int(parts[2])}
    total = _order_positions(order)
    return set(_my_positions(order, uid)) or set(range(1, total + 1))


@router.callback_query(F.data.startswith("order_printed:"))
async def cb_order_printed(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return

    uid = callback.from_user.id
    if not _may_act(order, uid):
        await callback.answer(
            f"Этот заказ {_words(order)['doing']} {order['assignee_name']} — "
            f"отметить может только он.",
            show_alert=True,
        )
        return
    # Свободный заказ забираем на себя: раз печатаешь — он твой
    if not order.get("assignee_id"):
        await db.force_set_order_assignee(order["id"], uid, _actor_name(callback))

    name = _actor_name(callback)
    total = _order_positions(order)
    targets = _target_positions(callback, order, uid)
    if not targets:
        await callback.answer("Нечего отмечать: позиция не найдена", show_alert=True)
        return

    prints = await db.get_order_prints(order["id"])
    mine = next((p for p in prints if p["user_id"] == uid), None)
    have = _print_positions_of(order, mine) if mine else set()
    marked = bool(targets - have)
    await db.set_order_print_positions(order["id"], uid, name, have | targets)

    prints = await db.get_order_prints(order["id"])
    waiting = [p for p in range(1, total + 1) if p not in _print_map(order, prints)]
    if not waiting:
        await db.set_order_printed(order["id"], uid, name)
        w = _words(order)
        note = (f"Отмечено: {w['all_done'].lower()} целиком {w['icon']}" if marked
                else f"Уже отмечено {w['icon']}")
        # В финансовый лист «Печатал» проставляем только теперь — берём
        # фактически отметивших (order_prints), а не того, за кем заказ числится
        if order.get("order_code"):
            printers = ", ".join(sorted({p["user_name"] for p in prints if p["user_name"]}))
            credits = await db.order_print_credits(order, prints, config.ADMIN_IDS)
            danya_positions = credits.get(config.PARTNER_ID, 0)
            request_finance_printer_update(order["order_code"], printers, danya_positions)
    elif total > 1:
        icon = _words(order, sorted(targets))["icon"]
        note = (f"Отмечено: {_pos_list(sorted(targets))} {icon} Осталось: {_pos_list(waiting)}"
                if marked else f"Эта позиция уже отмечена {icon}")
    else:
        w = _words(order)
        note = (f"Отмечено: {w['all_done'].lower()} {w['icon']}" if marked
                else f"Уже отмечено {w['icon']}")

    request_sync()
    await callback.answer(note)
    await _sync_order_messages(callback, await db.get_order(order["id"]))


@router.callback_query(F.data.startswith("order_unprint:"))
async def cb_order_unprint(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return

    uid = callback.from_user.id
    if not _may_act(order, uid):
        await callback.answer(
            f"Откатить может только {order.get('printed_by_name') or order.get('assignee_name')}.",
            show_alert=True,
        )
        return

    # Снимаем отметку с указанных позиций — заказ перестаёт быть
    # распечатанным целиком
    targets = _target_positions(callback, order, uid)
    prints = await db.get_order_prints(order["id"])
    for p in prints:
        # Позицию мог отметить и напарник: снимаем у того, чья отметка
        kept = _print_positions_of(order, p) - targets
        if kept != _print_positions_of(order, p):
            await db.set_order_print_positions(order["id"], p["user_id"],
                                               p["user_name"], kept)
    await db.clear_order_printed(order["id"])
    request_sync()
    note = ("Отметка снята ↩️" if _order_positions(order) == 1
            else f"Снята отметка: {_pos_list(sorted(targets))} ↩️")
    await callback.answer(note)
    await _sync_order_messages(callback, await db.get_order(order["id"]))


@router.callback_query(F.data.startswith("order_barcode:"))
async def cb_order_barcode(callback: CallbackQuery):
    """Присылает наклейку ШК-места из СДЭК — её клеят на коробку.

    Наклейка не меняется, поэтому первый присланный файл запоминаем:
    жать кнопку будут повторно, а гонять СДЭК ради того же PDF незачем.
    """
    order = await _load(callback)
    if not order:
        return

    name = (order.get("order_code") or order.get("prodamus_order_id")
            or f"order-{order['id']}")
    caption = f"🏷 Штрихкод СДЭК · {name}"

    if order.get("cdek_barcode_file_id"):
        await callback.answer()
        # Ответом на карточку заказа — чтобы наклейка была привязана к нему
        # в чате так же, как пункты списка /myorders
        sent = await callback.message.reply_document(order["cdek_barcode_file_id"],
                                                     caption=caption,
                                                     allow_sending_without_reply=True)
        await _remember_for_orders_cleanup(callback, sent)
        return

    if not order.get("cdek_uuid"):
        await callback.answer(
            "У этого заказа нет накладной СДЭК — распечатайте из кабинета.",
            show_alert=True,
        )
        return

    from services.cdek_accounts import client_for_order
    if not client_for_order(order):
        await callback.answer("Не подключён договор СДЭК этой накладной.", show_alert=True)
        return

    if order["id"] in _BARCODE_BUSY:
        await callback.answer("Наклейка уже готовится — подождите.", show_alert=True)
        return

    await callback.answer("Готовлю наклейку — пришлю сюда, как СДЭК её отдаст.")
    # Не держим обработчик: СДЭК собирает форму от пары секунд до минут,
    # а бывает, что их очередь печати встаёт совсем. Ждём в фоне, чтобы
    # остальные кнопки в это время работали.
    asyncio.create_task(_deliver_barcode(callback, order, name, caption))


_BARCODE_BUSY: set[int] = set()


async def _remember_for_orders_cleanup(callback: CallbackQuery, sent: Message):
    """Наклейки СДЭК убираются из чата вместе со списком при следующем /orders."""
    await db.add_admin_command_message("orders", sent.chat.id, callback.from_user.id,
                                       sent.message_id)


async def _deliver_barcode(callback: CallbackQuery, order: dict, name: str, caption: str):
    from services.cdek_accounts import client_for_order

    _BARCODE_BUSY.add(order["id"])
    try:
        client = client_for_order(order)
        pdf = (await client.get_barcode_pdf(order["cdek_uuid"],
                                           fmt=config.CDEK_BARCODE_FORMAT)
               if client else None)
    except Exception as e:
        logger.error(f"barcode {name}: {type(e).__name__}: {e}")
        pdf = None
    finally:
        _BARCODE_BUSY.discard(order["id"])

    if not pdf:
        sent = await callback.message.answer(
            f"⚠️ СДЭК не отдал наклейку по заказу {name} — у них зависла очередь "
            f"печати форм. Нажмите позже или распечатайте из кабинета СДЭК "
            f"(накладная {order.get('cdek_number') or '—'})."
        )
        await _remember_for_orders_cleanup(callback, sent)
        return

    sent = await callback.message.reply_document(
        BufferedInputFile(pdf, filename=f"{name}.pdf"), caption=caption,
        allow_sending_without_reply=True,
    )
    await _remember_for_orders_cleanup(callback, sent)
    if sent.document:
        await db.set_order_barcode_file(order["id"], sent.document.file_id)


@router.callback_query(F.data.startswith("order_repeat:"))
async def cb_order_repeat(callback: CallbackQuery):
    """Показывает подтверждение — повтор создаёт реальную накладную СДЭК."""
    order = await _load(callback)
    if not order:
        return
    # Повтор — для уже отправленного заказа; на старых карточках кнопка
    # могла остаться и до отправки
    if not order.get("shipped_at"):
        await callback.answer("Повторить можно после «Заказ отправил».", show_alert=True)
        await _sync_order_messages(callback, order)
        return
    existing = await db.get_direct_replacement(order["id"])
    if existing:
        code = existing.get("order_code") or f"#{existing['id']}"
        await callback.answer(f"Повтор уже создан: {code}", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_reply_markup(
        reply_markup=order_repeat_confirm_keyboard(order["id"])
    )


@router.callback_query(F.data.startswith("order_repeat_cancel:"))
async def cb_order_repeat_cancel(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return
    await callback.answer("Отменено")
    prints = await db.get_order_prints(order["id"])
    await callback.message.edit_reply_markup(
        reply_markup=_order_keyboard(order, callback.from_user.id, prints)
    )


@router.callback_query(F.data.startswith("order_repeat_confirm:"))
async def cb_order_repeat_confirm(callback: CallbackQuery):
    """Создаёт бесплатный заказ-копию и новую доставку за счёт бизнеса."""
    source = await _load(callback)
    if not source:
        return

    existing = await db.get_direct_replacement(source["id"])
    if existing:
        code = existing.get("order_code") or f"#{existing['id']}"
        await callback.answer(f"Повтор уже создан: {code}", show_alert=True)
        await _sync_order_messages(callback, source)
        return

    await callback.answer("Создаю повтор…")
    order_code = await db.next_order_code()
    delivery_cost = await db.get_order_delivery_cost(source["id"])
    summary = _replacement_summary(source, order_code)
    new_id, created = await db.create_replacement_order(
        source["id"], order_code, callback.from_user.id, _actor_name(callback),
        summary, delivery_cost,
    )
    if not created:
        replacement = await db.get_order(new_id)
        code = (replacement or {}).get("order_code") or f"#{new_id}"
        await callback.message.answer(f"ℹ️ Повтор уже был создан: <code>{code}</code>",
                                      parse_mode="HTML")
        await _sync_order_messages(callback, source)
        return

    replacement = await db.get_order(new_id)
    rounds, round_products, products = await _replacement_products(replacement)

    # Новая карточка и вложения появляются только в админском боте.
    from handlers.prodamus_webhook import _send_order_notify
    main_bot = Bot(token=config.BOT_TOKEN)
    try:
        await _send_order_notify(
            new_id, _order_text(replacement), main_bot=main_bot,
            rounds=rounds, order_number=order_code,
        )
    finally:
        await main_bot.session.close()

    # Нулевая выручка + стоимость доставки: повтор не выглядит как новая
    # оплата, но СДЭК уменьшает прибыль и попадает в финансовую таблицу.
    from services.gsheets import request_finance_append
    counts = {pid: round_products.count(pid) for pid in dict.fromkeys(round_products)}
    goods = ", ".join(
        (products.get(pid) or {}).get("name", f"id:{pid}")
        + (f" ×{counts[pid]}" if counts[pid] > 1 else "")
        for pid in counts
    )
    credits = await db.order_print_credits(replacement, [], config.ADMIN_IDS)
    date_msk = (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    request_finance_append(
        order_code, date_msk, 0, delivery_cost,
        comment=f"Повтор без оплаты: {goods}", goods_type="Физический",
        printer_positions=credits.get(config.PARTNER_ID, 0),
    )
    request_sync()

    missing = [label for label, value in (
        ("ПВЗ", replacement.get("pvz_code")),
        ("ФИО", replacement.get("recipient_name")),
        ("телефон", replacement.get("recipient_phone")),
    ) if not value]
    if config.CDEK_AUTO_ORDER and not missing:
        pending = {
            "pvz_code": replacement.get("pvz_code"),
            "recipient_name": replacement.get("recipient_name"),
            "recipient_phone": replacement.get("recipient_phone"),
            "delivery_str": replacement.get("summary") or "",
        }
        asyncio.create_task(_create_replacement_cdek(
            new_id, pending, round_products, products, order_code,
            replacement["user_id"],
        ))
        delivery_note = "Новая накладная СДЭК создаётся автоматически."
    elif missing:
        delivery_note = ("Накладную нужно создать вручную: не хватает данных — "
                         + ", ".join(missing) + ".")
    else:
        delivery_note = "Автосоздание СДЭК выключено — заведите накладную вручную."

    await callback.message.answer(
        f"✅ Создан бесплатный повтор <code>{order_code}</code>\n"
        f"Клиент не платит и не возвращает товар. {delivery_note}",
        parse_mode="HTML",
    )
    await _sync_order_messages(callback, source)


@router.callback_query(F.data.startswith("order_shipped:"))
async def cb_order_shipped(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return

    if order.get("shipped_at"):
        await callback.answer("Заказ уже отмечен как отправленный ✅")
        await _sync_order_messages(callback, order)
        return

    if not _may_act(order, callback.from_user.id):
        await callback.answer(
            f"Этот заказ у {order['assignee_name']} — отметить отправку может только он.",
            show_alert=True,
        )
        return
    # Кнопка появляется только после печати (сборки), но на старой
    # карточке она может остаться — отправку раньше времени не отмечаем
    if not _all_printed(order, await db.get_order_prints(order["id"])):
        await callback.answer(f"Сначала отметь «{_words(order)['did']}» ✋", show_alert=True)
        await _sync_order_messages(callback, order)
        return
    if not order.get("assignee_id"):
        await db.force_set_order_assignee(order["id"], callback.from_user.id,
                                          _actor_name(callback))

    await db.set_order_shipped(order["id"], callback.from_user.id, _actor_name(callback))
    request_sync()
    await callback.answer("Отмечено: заказ отправлен 📦")

    # Расходники (коробка/поп-фильтр) списываем в момент отправки — именно
    # тогда товар реально уходит в коробку, а не когда его напечатали.
    # Со счёта того, кто нажал кнопку — он и паковал своими запасами.
    # Предупреждать о нехватке здесь уже поздно — это делается при приходе
    # заказа (consumables.order_alerts из _send_order_notify)
    from handlers import consumables
    total = _order_positions(order)
    await consumables.apply_stock_delta(
        callback.from_user.id, order, set(range(1, total + 1)), -1)

    # Пуш с просьбой оставить отзыв — отсчёт от отправки, не от оплаты:
    # заказ может ждать печати неделями, и «через 5 дней» должно значить
    # 5 дней после того, как товар реально уехал к клиенту. В заказе может
    # быть несколько разных товаров («Добавить другой товар» в опросе) —
    # ставим свой пуш на каждый (дедуп по паре заказ+товар — см. database.py)
    try:
        rounds = json.loads(order.get("rounds_json") or "[]")
    except Exception:
        rounds = []
    round_products = db.unpack_round_products(
        order.get("round_products_json"), rounds, order["product_id"])
    if not round_products:
        round_products = [order["product_id"]]
    for pid in dict.fromkeys(round_products):
        product = await db.get_product(pid)
        if product and product.get("review_push_delay"):
            await db.enqueue_review_push(order["user_id"], pid,
                                         product["review_push_delay"],
                                         order_id=order["id"])

    order = await db.get_order(order["id"])
    await _sync_order_messages(callback, order)


@router.callback_query(F.data.startswith("order_unship:"))
async def cb_order_unship(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return

    if not order.get("shipped_at"):
        await callback.answer("Отметки об отправке нет.")
        await _sync_order_messages(callback, order)
        return

    allowed = {order.get("shipped_by_id"), order.get("assignee_id")} - {None}
    if callback.from_user.id not in allowed:
        await callback.answer(
            f"Откатить может только {order.get('shipped_by_name') or order.get('assignee_name')}.",
            show_alert=True,
        )
        return

    # Возвращаем тому, кто изначально списал — тот, кто отправлял, а не
    # обязательно тот, кто сейчас откатывает отметку
    from handlers import consumables
    total = _order_positions(order)
    refund_to = order.get("shipped_by_id") or callback.from_user.id
    await consumables.apply_stock_delta(refund_to, order, set(range(1, total + 1)), +1)

    await db.clear_order_shipped(order["id"])
    request_sync()
    await callback.answer("Отметка об отправке снята ↩️")
    order = await db.get_order(order["id"])
    await _sync_order_messages(callback, order)


@router.callback_query(F.data.regexp(r"^order_(reassign|reassign_cancel|takeover|takepos|unassign):"))
async def cb_order_removed_button(callback: CallbackQuery):
    """Смены исполнителя больше нет, но старые карточки в чате ещё могут
    показывать её кнопки — перерисовываем карточку без них."""
    await callback.answer("Этой кнопки больше нет — обновил карточку.")
    order = await _load(callback)
    if order:
        await _sync_order_messages(callback, order)
