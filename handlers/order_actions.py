"""
Кнопки заказа в админском боте (malimadmins / WAITLIST_BOT_TOKEN):
«Взял заказ» и «Сменить исполнителя».
Этот бот слушает только callback_query — отдельным поллингом в bot.py.
"""
import json
import logging
import re
from datetime import datetime, timedelta

from aiogram import Router, F
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
    """Основные кнопки заказа: распечатать → отправить / сменить исполнителя.

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
            if pos in printed:
                rows.append([InlineKeyboardButton(
                    text=f"✅ Поз.{pos} напечатана — отменить",
                    callback_data=f"order_unprint:{oid}:{pos}")])
            else:
                rows.append([InlineKeyboardButton(
                    text=f"🖨 Распечатал поз.{pos}",
                    callback_data=f"order_printed:{oid}:{pos}")])
    elif printed:
        rows.append([InlineKeyboardButton(text="↩️ Отменить «распечатал»",
                                          callback_data=f"order_unprint:{oid}:1")])
    else:
        rows.append([InlineKeyboardButton(text="🖨 Распечатал",
                                          callback_data=f"order_printed:{oid}:1")])

    # Наклейка СДЭК — когда печатать больше нечего и пора клеить на коробку.
    # Без накладной печатать нечего: её заводят вручную в кабинете
    if order.get("cdek_uuid") and all(p in printed for p in range(1, total + 1)):
        rows.append([InlineKeyboardButton(text="🏷 Штрихкод СДЭК",
                                          callback_data=f"order_barcode:{oid}")])

    rows += [
        [InlineKeyboardButton(text="📦 Заказ отправил",
                              callback_data=f"order_shipped:{oid}")],
        [InlineKeyboardButton(text="🔄 Сменить исполнителя",
                              callback_data=f"order_reassign:{oid}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_reassign_keyboard(order: dict) -> InlineKeyboardMarkup:
    """Меню смены исполнителя.

    В заказе из нескольких позиций каждую можно забрать отдельно: иначе
    «передать мне» меняло только подпись, а печать позиции оставалась
    за напарником.
    """
    oid = order["id"]
    whos = db.order_routing(order)
    rows = []
    if len(whos) > 1:
        for i, w in enumerate(whos, 1):
            rows.append([InlineKeyboardButton(
                text=f"🙋 Забрать поз.{i}" + (f" (сейчас {w})" if w else ""),
                callback_data=f"order_takepos:{oid}:{i}")])
        take = "👤 Забрать весь заказ"
    else:
        take = "👤 Передать мне"
    # «Снять исполнителя» нет: ничейный заказ теряется, исполнитель
    # у заказа должен быть всегда — меняется только на другого
    rows += [
        [InlineKeyboardButton(text=take, callback_data=f"order_takeover:{oid}")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"order_reassign_cancel:{oid}")],
    ]
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


def _routing_render(whos: list) -> str:
    """Строка «кто печатает» для карточки заказа."""
    if not whos:
        return ""
    if len(whos) == 1:
        return f"🖨 <b>Печатает:</b> {whos[0] or 'не определён'}"
    if len(set(whos)) == 1 and whos[0]:
        return f"🖨 <b>Печатает:</b> {whos[0]}"   # весь заказ у одного
    parts = [f"Поз.{i} — {w or 'не определён'}" for i, w in enumerate(whos, 1)]
    return "🖨 <b>Печать:</b> " + " · ".join(parts)


# Разбор строки печати из текста заказа живёт в database — им пользуется
# и расчёт долей, которому до обработчиков дела нет
parse_routing_line = db.parse_routing_line


def _with_routing(summary: str, whos: list) -> str:
    """Подменяет в тексте заказа строку печати на актуальную разметку."""
    line = _routing_render(whos)
    if not line:
        return summary
    out, replaced = [], False
    for ln in (summary or "").split("\n"):
        if ln.startswith("🖨 <b>Печат"):
            if not replaced:
                out.append(line)
                replaced = True
            continue
        out.append(ln)
    if not replaced:
        out.insert(1, line)      # сразу под заголовком с номером заказа
    return "\n".join(out)


def _order_text(order: dict, prints: list | None = None) -> str:
    text = _with_routing(order["summary"], db.order_routing(order))
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
        text += "\n🖨 <b>Распечатано:</b> " + (
            " · ".join(_pos_line(p) for p in done) if done else "—")
        if waiting:
            text += "\n⏳ <b>Ждём печать:</b> " + " · ".join(_pos_line(p) for p in waiting)
    elif order.get("printed_at"):
        when = _msk(order["printed_at"])
        text += f"\n🖨 <b>Распечатал:</b> {order.get('printed_by_name') or ''}"
        if when:
            text += f" · {when}"
    if order.get("shipped_at"):
        when = _msk(order["shipped_at"])
        text += f"\n📦 <b>Отправлен:</b> {order.get('shipped_by_name') or ''}"
        if when:
            text += f" · {when}"
    if order.get("cdek_number"):
        text += f"\n📦 <b>Трек-номер СДЭК:</b> <code>{order['cdek_number']}</code>"
    return text


def order_shipped_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Заказ отправлен — можно откатить отметку."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Отменить отметку об отправке",
                              callback_data=f"order_unship:{order_id}")]
    ])


# Ники админов для строки «ждём печать». Заполняется при первой синхронизации.
_ADMIN_NAMES: dict[int, str] = {}


async def _ensure_admin_names():
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
        return order_shipped_keyboard(order["id"])
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
        action = "🖨 Напечатать" + _products_label(order, [1], total)
    elif mode == "my":
        mine = [p for p in _my_positions(order, viewer_id) if p in waiting]
        action = (f"🖨 Напечатать {_pos_list(mine)}" + _products_label(order, mine, total) if mine
                  else "⏳ Ждём вторую часть")
    else:
        whos = db.order_routing(order)
        by_who: dict[str, list] = {}
        for p in waiting:
            who = (whos[p - 1] if p <= len(whos) else None) or "не определён"
            by_who.setdefault(who, []).append(p)
        action = "🖨 " + " · ".join(
            f"Напечатать {_pos_list(ps)}{_products_label(order, ps, total)} — {who}"
            for who, ps in by_who.items())

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


async def _positions_by_admin(whos: list, hint: tuple | None = None) -> dict[int, set]:
    """Ники позиций → {id админа: номера его позиций}, в порядке появления.

    hint — (ник, id) того, кто прямо сейчас забирает позицию: если у него
    нет юзернейма, по подписи его не найти, а потерять печатающего нельзя.
    """
    out: dict[int, set] = {}
    for i, name in enumerate(whos):
        if not name:
            continue
        if hint and name == hint[0]:
            out.setdefault(hint[1], set()).add(i)
            continue
        admin = await db.get_admin_by_username(name, config.ADMIN_IDS)
        if not admin:
            logger.warning(f"routing: не нашёл админа по нику {name!r}")
            continue
        out.setdefault(admin["user_id"], set()).add(i)
    return out


def _owner_of(by_admin: dict[int, set], index: int) -> int | None:
    """Кто печатает позицию с этим индексом (0-based)."""
    for uid, positions in by_admin.items():
        if index in positions:
            return uid
    return None


def _print_positions_of(order: dict, mark: dict) -> set:
    """Какие позиции закрывает отметка (старая, без номеров — всю свою часть)."""
    positions = db.print_positions(mark)
    if positions:
        return positions
    return {pos for pos, m in _print_map(order, [mark]).items()}


async def _apply_routing(order: dict, whos: list, actor_id: int, actor_name: str):
    """Записывает новое распределение позиций и подтягивает под него всё
    остальное: список печатающих, отметки о печати и исполнителя."""
    oid = order["id"]
    hint = (actor_name, actor_id)
    before = await _positions_by_admin(db.order_routing(order), hint)
    await db.set_order_routing(oid, whos)
    after = await _positions_by_admin(whos, hint)
    ids = list(after)
    await db.set_order_printers(oid, ids)

    # Позиция сменила хозяина — снимаем отметку только с ЕЩЁ НЕ напечатанных:
    # новому печатать заново. Уже напечатанное трогать нельзя — это факт,
    # за который начислены 200 ₽, и терять его молча при смене исполнителя
    # нельзя. Ошиблись — есть явная кнопка «↩️ Откатить печать».
    moved = {i + 1 for i in range(len(whos))
             if _owner_of(before, i) != _owner_of(after, i)}
    order_now = await db.get_order(oid)
    prints_now = await db.get_order_prints(oid)
    printed_pos = set(_print_map(order_now, prints_now))
    to_clear = moved - printed_pos
    if to_clear:
        for p in prints_now:
            kept = _print_positions_of(order_now, p) - to_clear
            await db.set_order_print_positions(oid, p["user_id"], p["user_name"], kept)
    kept_printed = moved & printed_pos
    if kept_printed:
        logger.info(f"order {oid}: позиции {sorted(kept_printed)} сменили исполнителя, "
                    f"но отметки о печати сохранены")

    order_now = await db.get_order(oid)
    if _all_printed(order_now, await db.get_order_prints(oid)):
        await db.set_order_printed(oid, actor_id, actor_name)
    else:
        await db.clear_order_printed(oid)

    # Ничейных заказов быть не должно: если прежний исполнитель больше
    # ничего не печатает, заказ закрепляем за тем, кто позицию забрал
    if not order.get("assignee_id") or (ids and order["assignee_id"] not in ids):
        await db.force_set_order_assignee(oid, actor_id, actor_name)


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
    return order


def _stage(order: dict, prints: list | None = None) -> tuple[str, str]:
    """Значок и подпись текущего этапа заказа."""
    if order.get("shipped_at"):
        return "📦", "отправлен"
    if order.get("printed_at"):
        return "🖨", "распечатан"
    total = _order_positions(order)
    if total > 1 and prints:
        printed = _print_map(order, prints)
        waiting_pos = [p for p in range(1, total + 1) if p not in printed]
        if printed and waiting_pos:
            whos = db.order_routing(order)
            waiting = ", ".join(
                f"поз.{p}" + (f" ({whos[p - 1]})" if p <= len(whos) and whos[p - 1] else "")
                for p in waiting_pos)
            return "🖨", f"часть готова, ждём {waiting}"
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
        line += f" · оплачен {when}"
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
                     cards: dict, mode: str) -> int:
    """Шлёт пункты списка ответами на карточки заказов. Возвращает след. номер."""
    i = start_index
    for o in orders:
        card_id = cards.get(o["id"])
        prints = await db.get_order_prints(o["id"])
        sent = await message.answer(
            _list_item_text(o, i, bool(card_id), message.from_user.id, prints, mode),
            parse_mode="HTML", reply_to_message_id=card_id,
        )
        _LIST_ITEMS.setdefault(o["id"], []).append(
            (message.chat.id, sent.message_id, i, bool(card_id), mode)
        )
        i += 1
    return i


@router.message(Command("allorders"))
async def cmd_all_orders(message: Message):
    """Все заказы в работе — свои и напарника, ответами на карточки."""
    if message.from_user.id not in config.ADMIN_IDS:
        return
    await _ensure_admin_names()

    orders = await db.get_orders(only_unshipped=True)
    if not orders:
        await message.answer("📋 Неотправленных заказов нет — всё разослано 🎉")
        return
    orders.sort(key=lambda o: o.get("created_at") or "")

    by_person: dict[str, list] = {}
    for o in orders:
        by_person.setdefault(o.get("assignee_name") or "ничей", []).append(o)

    await message.answer(f"📋 <b>Все заказы в работе — {len(orders)}</b>",
                         parse_mode="HTML")
    _forget_lists(message.chat.id)
    cards = await _cards_in_chat(orders, message.chat.id)

    i = 1
    mine = _ADMIN_NAMES.get(message.from_user.id)
    for who, items in sorted(by_person.items(), key=lambda kv: -len(kv[1])):
        mark = " (ты)" if who == mine else ""
        await message.answer(f"👤 <b>{who}</b>{mark} — {len(items)}", parse_mode="HTML")
        i = await _send_list(message, items, i, cards, mode="all")


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


@router.message(Command("myorders"))
async def cmd_my_orders(message: Message):
    """Список заказов, ждущих действия — ответами на исходные карточки.

    Сам заказ повторно не пересказываем: Telegram покажет процитированную
    карточку, а мы дописываем только номер и что с ним делать.
    """
    uid = message.from_user.id
    if uid not in config.ADMIN_IDS:
        return

    orders = await db.get_orders(only_unshipped=True)
    # Показываем заказ каждому, кто его печатает: в заказе с разными
    # позициями печатают оба, и он не должен потеряться ни у кого.
    todo = [o for o in orders
            if o.get("assignee_id") == uid
            or uid in db.printer_ids(o)
            or not o.get("assignee_id")]
    # Старые сверху — обрабатываем по очереди поступления
    todo.sort(key=lambda o: o.get("created_at") or "")

    if not todo:
        await message.answer("📋 Заказов в работе нет — всё разослано 🎉")
        return

    await message.answer(f"📋 <b>Твои заказы: {len(todo)}</b>", parse_mode="HTML")

    _forget_lists(message.chat.id)
    cards = await _cards_in_chat(todo, message.chat.id)
    await _send_list(message, todo, 1, cards, mode="my")



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
            f"Этот заказ печатает {order['assignee_name']} — отметить может только он.",
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
        note = "Отмечено: распечатано целиком 🖨" if marked else "Уже отмечено 🖨"
        # В финансовый лист «Печатал» проставляем только теперь — при
        # оплате исполнитель ещё не известен, а заказ до печати мог и
        # передаться другому админу, поэтому берём фактических печатавших
        # (order_prints), а не того, за кем заказ числится
        if order.get("order_code"):
            printers = ", ".join(sorted({p["user_name"] for p in prints if p["user_name"]}))
            credits = await db.order_print_credits(order, prints, config.ADMIN_IDS)
            danya_positions = credits.get(config.PARTNER_ID, 0)
            request_finance_printer_update(order["order_code"], printers, danya_positions)
    elif total > 1:
        note = (f"Отмечено: {_pos_list(sorted(targets))} 🖨 Осталось: {_pos_list(waiting)}"
                if marked else "Эта позиция уже отмечена 🖨")
    else:
        note = "Отмечено: распечатано 🖨" if marked else "Уже отмечено 🖨"

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
        await callback.message.reply_document(order["cdek_barcode_file_id"],
                                              caption=caption,
                                              allow_sending_without_reply=True)
        return

    if not order.get("cdek_uuid"):
        await callback.answer(
            "У этого заказа нет накладной СДЭК — распечатайте из кабинета.",
            show_alert=True,
        )
        return

    from handlers.delivery import CDEK_CLIENT
    if not CDEK_CLIENT:
        await callback.answer("СДЭК не подключён.", show_alert=True)
        return

    await callback.answer("Готовлю штрихкод, секунд десять…")
    pdf = await CDEK_CLIENT.get_barcode_pdf(order["cdek_uuid"],
                                            fmt=config.CDEK_BARCODE_FORMAT)
    if not pdf:
        await callback.message.answer(
            f"⚠️ СДЭК не отдал штрихкод по заказу {name}. "
            f"Нажмите ещё раз или распечатайте из кабинета СДЭК."
        )
        return

    sent = await callback.message.reply_document(
        BufferedInputFile(pdf, filename=f"{name}.pdf"), caption=caption,
        allow_sending_without_reply=True,
    )
    if sent.document:
        await db.set_order_barcode_file(order["id"], sent.document.file_id)


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
    if not order.get("assignee_id"):
        await db.force_set_order_assignee(order["id"], callback.from_user.id,
                                          _actor_name(callback))

    await db.set_order_shipped(order["id"], callback.from_user.id, _actor_name(callback))
    request_sync()
    await callback.answer("Отмечено: заказ отправлен 📦")

    # Расходники (коробка/поп-фильтр) списываем в момент отправки — именно
    # тогда товар реально уходит в коробку, а не когда его напечатали.
    # Со счёта того, кто нажал кнопку — он и паковал своими запасами
    from handlers import consumables
    total = _order_positions(order)
    touched = await consumables.apply_stock_delta(
        callback.from_user.id, order, set(range(1, total + 1)), -1)
    for key in touched:
        note = await consumables.stock_note(callback.from_user.id, key)
        if note:
            await callback.message.answer(note, parse_mode="HTML")

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


@router.callback_query(F.data.startswith("order_reassign:"))
async def cb_order_reassign(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(
            reply_markup=order_reassign_keyboard(order)
        )
    except Exception as e:
        logger.debug(f"order reassign menu failed: {e}")


@router.callback_query(F.data.startswith("order_reassign_cancel:"))
async def cb_order_reassign_cancel(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=_order_keyboard(order))
    except Exception as e:
        logger.debug(f"order reassign cancel failed: {e}")


@router.callback_query(F.data.startswith("order_takeover:"))
async def cb_order_takeover(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return

    name = _actor_name(callback)
    whos = db.order_routing(order) or [None]
    mine = (order.get("assignee_id") == callback.from_user.id
            and all(w == name for w in whos))
    if mine:
        await callback.answer("Заказ и так за тобой ✅")
    else:
        # Забираем целиком: все позиции печатает тот, кто нажал
        await _apply_routing(order, [name] * len(whos), callback.from_user.id, name)
        request_sync()
        await callback.answer(f"Весь заказ теперь за {name} ✅")
    order = await db.get_order(order["id"])
    await _sync_order_messages(callback, order)


@router.callback_query(F.data.startswith("order_takepos:"))
async def cb_order_takepos(callback: CallbackQuery):
    """Забрать себе одну позицию разделённого заказа."""
    order = await _load(callback)
    if not order:
        return
    try:
        pos = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer()
        return

    whos = db.order_routing(order)
    if not 1 <= pos <= len(whos):
        await callback.answer("Такой позиции в заказе нет", show_alert=True)
        return

    name = _actor_name(callback)
    if whos[pos - 1] == name:
        await callback.answer(f"Поз.{pos} и так за тобой ✅")
    else:
        whos[pos - 1] = name
        await _apply_routing(order, whos, callback.from_user.id, name)
        request_sync()
        await callback.answer(f"Поз.{pos} теперь печатаешь ты ✅")
    await _sync_order_messages(callback, await db.get_order(order["id"]))


@router.callback_query(F.data.startswith("order_unassign:"))
async def cb_order_unassign(callback: CallbackQuery):
    """Кнопки больше нет, но старые карточки в чате могут её показывать."""
    await callback.answer(
        "Заказ не может быть ничейным — передай его на себя или напарнику.",
        show_alert=True,
    )
    order = await _load(callback)
    if order:
        await _sync_order_messages(callback, order)
