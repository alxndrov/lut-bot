"""
aiohttp-обработчик вебхуков от Prodamus.
Подпись — в HTTP-заголовке Sign.
order_id в URL → order_num в вебхуке (сквозной идентификатор).
"""
import asyncio
import json
import logging
from functools import partial
from typing import Optional
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiogram import Bot
from aiogram.types import BufferedInputFile

import config
import database as db
from services.prodamus import verify_webhook

logger = logging.getLogger(__name__)

# Сообщение клиенту после оплаты физтовара. {order} → номер заказа.
# Редактируется в админке: 📋 Опрос → «💬 После оплаты».
DEFAULT_POST_PAYMENT = (
    "✅ Оплата прошла! Спасибо за заказ 🎉\n\n"
    "Номер заказа: {order}\n"
    "В течение нескольких рабочих дней я отправлю вам заказ."
)


async def handle_webhook(request: web.Request, secret: str | list = "") -> web.Response:
    bot: Bot = request.app["bot"]

    try:
        raw = dict(await request.post())
    except Exception as e:
        logger.error(f"prodamus webhook: ошибка разбора тела: {e}")
        return web.Response(text="bad request", status=400)

    logger.info(f"prodamus webhook [{request.path}]: headers.Sign={request.headers.get('Sign')!r}, body={raw}")

    # Проверяем подпись из заголовка Sign. Секретов может быть несколько:
    # при переезде на новую платёжную страницу старая ещё какое-то время
    # шлёт уведомления (у клиента могла остаться открытой ссылка на неё),
    # и её подпись считается СТАРЫМ секретом. Принимаем оба, иначе такая
    # оплата отвалится по «неверная подпись» и заказ потеряется.
    secrets = [s for s in (secret if isinstance(secret, list) else [secret]) if s]
    if secrets:
        sign_header = request.headers.get("Sign", "")
        if not any(verify_webhook(dict(raw), sign_header, s) for s in secrets):
            logger.warning(f"prodamus webhook [{request.path}]: неверная подпись "
                          f"(проверено секретов: {len(secrets)}, "
                          f"domain={raw.get('domain')!r})")
            return web.Response(text="invalid sign", status=403)

    # Неуспешный платёж — уведомляем покупателя если знаем его user_id
    if raw.get("payment_status") != "success":
        order_num = raw.get("order_num", "")
        if order_num:
            try:
                parts = order_num.split("_", 2)
                user_id = int(parts[1])
                await bot.send_message(
                    user_id,
                    "😔 К сожалению, оплата не прошла.\n\n"
                    "Попробуйте ещё раз — вернитесь в каталог и нажмите «Купить».",
                    reply_markup=_back_to_catalog_keyboard(),
                )
            except Exception:
                pass
        return web.Response(text="ok")

    # Сквозной параметр order_num: "{order_type}_{user_id}_{product_id}"
    order_num = raw.get("order_num", "")
    if not order_num:
        logger.warning(
            f"prodamus webhook: нет order_num — ручной платёж из дашборда? "
            f"(order_id={raw.get('order_id')}, payment_init={raw.get('payment_init')})"
        )
        return web.Response(text="ok")

    try:
        parts = order_num.split("_", 2)
        order_type = parts[0]        # "d" или "p"
        user_id = int(parts[1])
        product_id = int(parts[2])
    except Exception:
        # Тест из кабинета Prodamus или ручной платёж — привязать не к кому.
        # Отвечаем 200, чтобы Prodamus не считал URL «отвечающим ошибкой».
        logger.warning(f"prodamus webhook: order_num не распознан ({order_num!r}) — тест/ручной платёж, пропускаю")
        return web.Response(text="ok")

    prodamus_order_id = raw.get("order_id", "")
    try:
        amount = int(float(raw.get("sum") or raw.get("payment_sum") or 0))
    except Exception:
        amount = 0

    ok = await provision_payment(bot, user_id, product_id, order_type,
                                 prodamus_order_id, amount)
    if ok is False:
        return web.Response(text="product not found", status=404)
    return web.Response(text="ok")


async def provision_payment(bot: Bot, user_id: int, product_id: int, order_type: str,
                            prodamus_order_id: str, amount: int) -> bool | None:
    """Проводит оплаченный заказ: покупка, уведомление админам, выдача товара.

    Вызывается вебхуком Prodamus, а также вручную из админки, если вебхук
    не дошёл (перезапуск бота, сбой сети). Повторный вызов безопасен —
    уже проведённый платёж отсекается по prodamus_order_id.
    """
    # Идемпотентность: если этот платёж уже обработан (Prodamus повторил вебхук или
    # уведомление пришло и из кабинета, и по urlNotification из ссылки) — не выдаём повторно.
    if prodamus_order_id:
        already = await db.get_purchase_by_payment_id(prodamus_order_id)
        if already:
            logger.info(
                f"prodamus: платёж {prodamus_order_id} уже обработан "
                f"(purchase id={already.get('id')}) — пропускаю повторную выдачу"
            )
            return None

    product = await db.get_product(product_id)
    if not product:
        logger.error(f"prodamus: товар {product_id} не найден")
        return False

    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    # Имя/юзернейм из таблицы users
    username, first_name = None, "—"
    try:
        import aiosqlite
        async with aiosqlite.connect(db.DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT username, first_name FROM users WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    username = row["username"]
                    first_name = row["first_name"] or "—"
    except Exception as e:
        logger.warning(f"prodamus webhook: не удалось получить данные юзера: {e}")

    username_str = f"@{username}" if username else f"id:{user_id}"

    if order_type == "d":
        category = product.get("category", "digital")

        purchase_num = await db.add_purchase(
            user_id=user_id, username=username, product_id=product_id,
            telegram_payment_id=prodamus_order_id, amount=amount,
        )
        await db.cancel_funnel_for_user(user_id, product_id)

        # Строка в финансовый лист — приход без доставки/СДЭК (это цифровой
        # товар); своего сквозного номера у таких заказов нет, берём id
        # платежа Prodamus. Комиссия/Налог считаются формулой, как у физтоваров.
        from services.gsheets import request_finance_append
        order_date_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
        request_finance_append(prodamus_order_id, order_date_msk, amount, 0.0,
                               comment=product["name"], goods_type="Цифровой")

        await _send_notify(bot, (
            f"💰 <b>Новая покупка</b>\n\n"
            f"👤 {first_name} {username_str}\n"
            f"🛍 {product['name']}\n"
            f"💵 {amount} ₽\n"
            f"🕐 {now}"
        ))

        await bot.send_message(user_id, "✅ Оплата прошла! Спасибо за покупку 🎉")

        if category == "infobiz":
            await _deliver_infobiz(bot, user_id, product, purchase_num,
                                   username=username, first_name=first_name)
        else:
            await _deliver_digital(bot, user_id, product)

        # Ставим пуш отзыва в очередь (если настроен)
        if product.get("review_push_delay"):
            await db.enqueue_review_push(user_id, product_id, product["review_push_delay"])

    elif order_type == "p":
        pending = await db.get_pending_order(user_id, product_id) or {}
        delivery_info = pending.get("delivery_str") or "не указан"
        rounds = []
        if pending.get("survey_json"):
            try:
                rounds = json.loads(pending["survey_json"])
            except Exception as e:
                logger.error(f"prodamus webhook: не разобрать survey_json: {e}")
        # Товар каждого раунда — если через «Добавить другой товар» в заказ
        # попали разные физтовары. Без этого поля (заказ одного товара) —
        # все раунды считаются product_id
        round_products = db.unpack_round_products(
            pending.get("round_products_json"), rounds, product_id)
        if not round_products:
            # Товар без настроенного опроса (свободное ТЗ) — pending_deliveries
            # не заполнялась, rounds пуст; заказ всё равно на одну позицию
            round_products = [product_id]
        products_by_id = {product_id: product}
        for pid in set(round_products):
            if pid not in products_by_id:
                products_by_id[pid] = await db.get_product(pid) or product
        await db.delete_pending_delivery(user_id, product_id)

        # Доставка внутри суммы — транзит, в выручку не идёт. Разные товары
        # заказа — отдельной строкой purchases на каждый (для отчёта «по
        # товарам» и доли партнёра); доставка целиком уходит в первую
        # строку, чтобы не задвоить её в общем СДЭК-балансе.
        delivery_amt = int(pending.get("delivery_amount") or 0)
        delivery_cost_val = float(pending.get("delivery_cost") or 0)
        goods_amount_total = max(0, amount - delivery_amt)
        order_ids = list(dict.fromkeys(round_products))
        counts = {pid: round_products.count(pid) for pid in order_ids}
        weights = {pid: products_by_id[pid]["price"] * counts[pid] for pid in order_ids}
        weight_total = sum(weights.values()) or 1
        remaining_goods = goods_amount_total
        for i, pid in enumerate(order_ids):
            if i == len(order_ids) - 1:
                row_goods = remaining_goods
            else:
                row_goods = round(goods_amount_total * weights[pid] / weight_total)
                remaining_goods -= row_goods
            await db.add_purchase(
                user_id=user_id, username=username, product_id=pid,
                telegram_payment_id=prodamus_order_id,
                amount=row_goods + (delivery_amt if i == 0 else 0),
                delivery_amount=delivery_amt if i == 0 else 0,
                delivery_cost=delivery_cost_val if i == 0 else 0.0,
                quantity=counts[pid],
            )

        # Свой сквозной номер заказа: malimabi-store-001, -002, ...
        order_code = await db.next_order_code()

        # Полное уведомление о заказе — только сейчас, после успешной оплаты.
        # Идёт в админский бот (malimadmins) с кнопкой «Взял заказ».
        whos = await _routing_whos(round_products, products_by_id, rounds)
        summary = _format_order(
            first_name, username_str, product["name"], amount,
            delivery_info, rounds, now, order_number=order_code,
            routing_line=_routing_line(whos),
            round_products=round_products, products_by_id=products_by_id,
        )
        order_row_id = await db.create_order(
            user_id, product_id, prodamus_order_id, summary,
            rounds_json=json.dumps(rounds, ensure_ascii=False),
            order_code=order_code,
            recipient_name=pending.get("recipient_name"),
            recipient_phone=pending.get("recipient_phone"),
            pvz_code=pending.get("pvz_code"),
            round_products_json=json.dumps(round_products, ensure_ascii=False),
        )

        # Заказ сразу за тем, кто его печатает — ничейных заказов быть не должно.
        # Печатающих может быть несколько: заказ покажется каждому из них.
        # Разметку позиций храним отдельно: её потом можно менять по одной
        if whos:
            await db.set_order_routing(order_row_id, whos)
        printers = await _printer_admins(whos)
        if printers:
            await db.set_order_printers(order_row_id,
                                        [p["user_id"] for p in printers])
            await db.force_set_order_assignee(order_row_id, printers[0]["user_id"],
                                              f"@{printers[0]['username']}")
            from handlers.order_actions import _order_text
            summary = _order_text(await db.get_order(order_row_id))

        # Строка в финансовый лист Google Таблицы — по заказу целиком, не
        # по позициям (доставка/комиссия/налог считаются от полной суммы).
        # Комиссия/Налог/К выплате там — формулами, дописываются вместе со
        # строкой (см. services/gsheets.py). После разметки/назначения
        # исполнителя (см. выше) — «Позиций напечатал Даня» считается той
        # же db.order_print_credits, что и «Взаиморасчёт» в самом боте, и
        # ей нужны routing/assignee, чтобы сразу дать то же число, что
        # покажет бот прямо сейчас (печати ещё не было — счёт по тому, за
        # кем заказ числится; уточнится по факту в cb_order_printed).
        from services.gsheets import request_finance_append
        from services.payout import delivery_out
        order_date_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
        # Точного счёта СДЭК может не быть (интеграция выключена, адрес
        # собран свободным текстом) — тогда оцениваем как и везде в боте
        sheet_delivery_cost = delivery_out(
            delivery_cost_val, delivery_amt if delivery_cost_val == 0 else 0,
            config.PRODAMUS_FEE_PERCENT)
        goods_comment = ", ".join(
            products_by_id[pid]["name"] + (f" ×{counts[pid]}" if counts[pid] > 1 else "")
            for pid in order_ids
        )
        fresh_order = await db.get_order(order_row_id)
        credits = await db.order_print_credits(fresh_order, [], config.ADMIN_IDS)
        request_finance_append(order_code, order_date_msk, amount, sheet_delivery_cost,
                               comment=goods_comment, goods_type="Физический",
                               printer_positions=credits.get(config.PARTNER_ID, 0))

        await _send_order_notify(order_row_id, summary, main_bot=bot, rounds=rounds,
                                 order_number=order_code)

        # Накладная СДЭК — отдельной задачей: создание асинхронное, ждать его
        # внутри вебхука нельзя, Prodamus ждёт быстрый ответ 200.
        if config.CDEK_AUTO_ORDER:
            asyncio.create_task(_create_cdek_order(
                order_row_id, pending, round_products, products_by_id,
                order_code, bot, user_id,
            ))

        # Таблица заказов обновится сама через несколько секунд
        from services.gsheets import request_sync
        request_sync()

        paid_text = (product.get("post_payment_text") or DEFAULT_POST_PAYMENT)
        paid_text = paid_text.replace("{order}", order_code)
        await bot.send_message(user_id, paid_text)

        # Пуш отзыва ставится не здесь: для физтовара отсчёт должен идти
        # от отправки, а не от оплаты — заказ может ждать печати неделями.
        # Ставится при отметке «отправлено» — см. cb_order_shipped.
    else:
        logger.error(f"prodamus: неизвестный order_type={order_type!r}")

    return True


def norm_question(text: str) -> str:
    """Текст вопроса для сравнения: без разницы в пробелах и пустых строках.

    Ответы клиента хранятся вместе с текстом вопроса на момент заказа, а в
    настройках товара текст живёт отдельно. Стоит поправить в вопросе
    лишнюю пустую строку — и точное сравнение перестаёт находить ответ:
    распределение молча выдаёт «печатающий не определён» (так вышло с
    вопросом-распределителем кейса).
    """
    return " ".join((text or "").split()).casefold()


def find_answer(answers: list, question_text: str) -> str | None:
    """Ответ клиента на этот вопрос: сначала точно, затем без учёта пробелов."""
    for a in answers:
        if a.get("q") == question_text:
            return a.get("text")
    target = norm_question(question_text)
    for a in answers:
        if norm_question(a.get("q")) == target:
            return a.get("text")
    return None


def _parse_routing(text: str) -> dict:
    """'Даня: 1,2,3\\nПартнёр: 4,5' -> {'1':'Даня','2':'Даня',...,'4':'Партнёр',...}"""
    import re
    result = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        name, nums = line.split(":", 1)
        name = name.strip()
        for n in re.findall(r"\d+", nums):
            result[n] = name
    return result


async def _create_cdek_order(order_row_id: int, pending: dict, round_products: list[int],
                             products_by_id: dict, order_number: str,
                             bot: Bot = None, client_id: int = 0):
    """Заводит накладную в СДЭК после оплаты и сообщает результат админам.

    round_products — товар каждой позиции заказа (может быть смешанным).
    Если позиций несколько, их коробки склеиваются по узкой стороне в
    одно грузовое место (см. db.combine_packages); все товары попадают
    в накладную этого единственного места.
    Ошибка здесь не ломает заказ — деньги уже получены, заявка админам ушла.
    Админ просто заведёт отправление в кабинете СДЭК руками.
    """
    from handlers.delivery import CDEK_CLIENT

    pvz_code = (pending or {}).get("pvz_code")
    name = (pending or {}).get("recipient_name")
    phone = (pending or {}).get("recipient_phone")

    missing = [n for n, v in (("ПВЗ", pvz_code), ("ФИО", name), ("телефон", phone)) if not v]
    if not CDEK_CLIENT or missing:
        reason = "СДЭК не настроен" if not CDEK_CLIENT else f"не хватает данных: {', '.join(missing)}"
        logger.warning(f"CDEK order {order_number}: пропускаю — {reason}")
        await _send_notify(None, (
            f"⚠️ Заказ <code>{order_number}</code>: накладная СДЭК не создана "
            f"({reason}). Заведите отправление вручную."
        ))
        return

    items, packages = [], []
    for pid in round_products:
        p = products_by_id.get(pid) or {}
        pkg = db.product_package(p)
        items.append({
            "name": p.get("name", "?"),
            "ware_key": str(p.get("id", pid)),
            "cost": p.get("price", 0),
            "amount": 1,
            "weight": pkg["weight"],
        })
        packages.append(pkg)
    # Коробки склеиваются по узкой стороне в одно грузовое место —
    # так удобнее в отправке, чем несколько отдельных коробок.
    if len(packages) > 1:
        packages = [db.combine_packages(packages)]
    uuid = await CDEK_CLIENT.create_order(
        number=order_number,
        shipment_point=config.CDEK_SHIPMENT_POINT,
        delivery_point=pvz_code,
        recipient_name=name,
        recipient_phone=phone,
        tariff_code=config.CDEK_TARIFF_PVZ,
        items=items,
        packages=packages,
    )
    if not uuid:
        await _send_notify(None, (
            f"⚠️ Заказ <code>{order_number}</code>: СДЭК не принял накладную. "
            f"Заведите отправление вручную, подробности в логах."
        ))
        return

    await db.set_order_cdek(order_row_id, uuid)

    # Создание асинхронное — ждём результат, но недолго
    for _ in range(6):
        await asyncio.sleep(5)
        info = await CDEK_CLIENT.get_order_info(uuid)
        if not info:
            continue
        if info["state"] == "SUCCESSFUL":
            track = info.get("cdek_number")
            await db.set_order_cdek(order_row_id, uuid, track)
            logger.info(f"CDEK order {order_number}: создан, трек {track}")
            # трек-номер появился — подтянем его в таблицу
            from services.gsheets import request_sync
            request_sync()
            await _send_track_to_client(bot, client_id, order_number, track,
                                        pending.get("delivery_str"))
            # Не отдельным сообщением — дописываем трек в саму карточку
            # заказа (см. _order_text), она и так уже открыта у админов
            from handlers.order_actions import _sync_order_messages
            notify_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
            try:
                await _sync_order_messages(notify_bot, await db.get_order(order_row_id))
            finally:
                await notify_bot.session.close()
            return
        if info["state"] == "INVALID":
            errs = "; ".join(e.get("message", "") for e in info.get("errors", []))
            logger.error(f"CDEK order {order_number}: отклонён — {errs}")
            await _send_notify(None, (
                f"⚠️ Заказ <code>{order_number}</code>: СДЭК отклонил накладную.\n"
                f"<i>{errs}</i>\nЗаведите отправление вручную."
            ))
            return

    logger.warning(f"CDEK order {order_number}: статус не подтвердился за 30 сек, uuid={uuid}")
    await _send_notify(None, (
        f"⏳ Заказ <code>{order_number}</code>: накладная СДЭК отправлена, "
        f"но подтверждение не пришло за 30 секунд. Проверьте кабинет СДЭК."
    ))


async def _send_track_to_client(bot: Bot, client_id: int, order_number: str,
                                track: str, delivery_str: str = None):
    """Сообщает покупателю трек-номер, как только накладная СДЭК создана."""
    if not bot or not client_id or not track:
        return
    # Из строки доставки берём только адрес пункта, без служебных пометок
    pvz = ""
    for line in (delivery_str or "").splitlines():
        if "пункт выдачи" in line.lower():
            pvz = line.split(":", 1)[-1].split("(код ПВЗ")[0].strip()
            break
    text = (
        f"📦 <b>Заказ {order_number} передан в СДЭК</b>\n\n"
        f"Трек-номер: <code>{track}</code>\n"
    )
    if pvz:
        text += f"Пункт выдачи: {pvz}\n"
    text += (
        f"\n<a href=\"https://www.cdek.ru/ru/tracking?order_id={track}\">"
        "Отследить посылку</a>\n\n"
        "Я напишу, когда посылка приедет в пункт выдачи."
    )
    try:
        await bot.send_message(client_id, text, parse_mode="HTML",
                               disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"CDEK: не отправить трек клиенту {client_id}: {e}")


async def _routing_whos(round_products: list[int], products_by_id: dict, rounds: list) -> list:
    """Кто печатает каждую позицию — по номеру цвета из вопроса-распределителя
    ЕЁ СОБСТВЕННОГО товара (в смешанном заказе у разных товаров могут быть
    разные распределители и разные списки печатающих)."""
    import re
    # Ни у одного товара заказа не настроена печать по разметке — не
    # показываем строку «Печатает» вовсе (как раньше для обычного товара)
    if not any((products_by_id.get(pid) or {}).get("order_routing_text")
              for pid in set(round_products)):
        return []
    questions_cache: dict[int, list] = {}
    rmap_cache: dict[int, dict] = {}

    async def who(pid: int, answers: list):
        product = products_by_id.get(pid)
        routing = product.get("order_routing_text") if product else None
        if not routing:
            return None
        if pid not in questions_cache:
            questions_cache[pid] = await db.get_product_questions(pid)
        router_q = next((q for q in questions_cache[pid] if q.get("is_router")), None)
        if not router_q:
            return None
        if pid not in rmap_cache:
            rmap_cache[pid] = _parse_routing(routing)
        val = find_answer(answers, router_q["text"]) or ""
        nums = re.findall(r"\d+", val)
        return rmap_cache[pid].get(nums[0]) if nums else None

    return [await who(pid, answers) for pid, answers in zip(round_products, rounds)]


def _routing_line(whos: list) -> str:
    """Строка «кто печатает» — рисуется там же, где перерисовывается карточка."""
    from handlers.order_actions import _routing_render
    return _routing_render(whos)


async def _printer_admins(whos: list) -> list[dict]:
    """Все администраторы, печатающие этот заказ, в порядке позиций.

    Если позиции печатают разные люди, заказ должен быть виден у каждого,
    иначе чужая позиция потеряется.
    """
    found, seen = [], set()
    for name in whos:
        if not name:
            continue
        admin = await db.get_admin_by_username(name, config.ADMIN_IDS)
        if not admin:
            logger.warning(f"routing: не нашёл админа по нику {name!r}")
            continue
        if admin["user_id"] not in seen:
            seen.add(admin["user_id"])
            found.append(admin)
    return found


def _format_order(first_name: str, username_str: str, product_name: str, amount: int,
                  delivery_info: str, rounds: list, now: str, order_number: str = "",
                  routing_line: str = "", round_products: list[int] | None = None,
                  products_by_id: dict | None = None) -> str:
    """Уведомление о заказе: номер, кто печатает, покупатель, товар, ответы, доставка."""
    multi = len(rounds) > 1
    mixed = bool(round_products) and len(set(round_products)) > 1
    head = "🧾 <b>Новый заказ</b>"
    if order_number:
        head += f" <code>{order_number}</code>"
    lines = ["<b>ТАЦ!</b> 🪿", head]
    if routing_line:
        lines.append(routing_line)
    lines += ["", f"👤 {first_name} {username_str}"]
    if mixed and products_by_id:
        order_ids = list(dict.fromkeys(round_products))
        counts = {pid: round_products.count(pid) for pid in order_ids}
        goods_line = ", ".join(
            (products_by_id[pid]["name"] if products_by_id.get(pid) else f"id:{pid}")
            + (f" ×{counts[pid]}" if counts[pid] > 1 else "")
            for pid in order_ids
        )
        lines.append(f"🛍 {goods_line}")
    else:
        lines.append(f"🛍 {product_name}" + (f"  ×{len(rounds)}" if multi else ""))
    lines += [f"💵 {amount} ₽", f"🕐 {now}"]
    for ri, answers in enumerate(rounds, 1):
        if multi:
            pos_title = f"Позиция {ri}"
            if mixed and round_products and products_by_id and ri - 1 < len(round_products):
                p = products_by_id.get(round_products[ri - 1])
                if p:
                    pos_title += f" ({p['name']})"
            lines.append(f"\n— <b>{pos_title}</b> —")
        elif answers:
            lines.append("")
        for i, a in enumerate(answers, 1):
            ans = a.get("text") or ("📷 фото" if a.get("photo")
                                    else ("📎 файл" if a.get("doc") else "—"))
            lines.append(f"<b>{i}. {a.get('q', '')}</b>\n{ans}")
    lines.append(f"\n🚚 <b>Доставка:</b>\n{delivery_info}")
    return "\n".join(lines)


async def _send_order_notify(order_id: int, summary: str, main_bot: Bot = None,
                             rounds: list = None, order_number: str = ""):
    """Шлёт заказ в админский бот (malimadmins) с кнопками работы над ним,
    сохраняя message_id каждой копии для синхронной простановки исполнителя.
    Следом — фото/файлы из ответов клиента (перезаливкой, см. _fetch_media)."""
    from handlers.order_actions import order_assigned_keyboard, _ensure_admin_names
    await _ensure_admin_names()
    order = await db.get_order(order_id)
    try:
        notify_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
    except Exception as e:
        logger.error(f"order notify: не создать бота: {e}")
        return
    for admin_id in config.ADMIN_IDS:
        try:
            # Кнопки печати свои у каждого: показываем его позиции
            msg = await notify_bot.send_message(
                admin_id, summary, parse_mode="HTML",
                reply_markup=order_assigned_keyboard(order, admin_id, [])
            )
            await db.add_order_message(order_id, admin_id, msg.message_id)
        except Exception as e:
            logger.error(f"order notify to {admin_id} failed: {e}")

    if main_bot and rounds:
        try:
            await _send_order_media(main_bot, notify_bot, rounds, order_number)
        except Exception as e:
            logger.error(f"order media failed: {e}")
    try:
        await notify_bot.session.close()
    except Exception:
        pass


async def _fetch_media(main_bot: Bot, file_id: str):
    """Скачивает файл из основного бота: file_id одного бота не годится для другого,
    поэтому перезаливаем байтами. -> (bytes, filename) | None"""
    try:
        f = await main_bot.get_file(file_id)
        buf = await main_bot.download_file(f.file_path)
        name = (f.file_path or "file").split("/")[-1]
        return buf.read(), name
    except Exception as e:
        logger.error(f"order media download failed ({file_id}): {e}")
        return None


async def _send_order_media(main_bot: Bot, notify_bot: Bot, rounds: list, order_number: str = ""):
    """Фото/файлы из ответов — в админский бот, следом за карточкой заказа."""
    multi = len(rounds) > 1
    head = f"№{order_number} · " if order_number else ""
    for ri, answers in enumerate(rounds, 1):
        prefix = f"Поз.{ri} · " if multi else ""
        for i, a in enumerate(answers, 1):
            file_id = a.get("photo") or a.get("doc")
            if not file_id:
                continue
            fetched = await _fetch_media(main_bot, file_id)
            if not fetched:
                continue
            data, name = fetched
            caption = f"{head}{prefix}Ответ {i}: {str(a.get('q', ''))[:180]}"
            for admin_id in config.ADMIN_IDS:
                try:
                    file = BufferedInputFile(data, filename=name)
                    if a.get("photo"):
                        await notify_bot.send_photo(admin_id, file, caption=caption)
                    else:
                        await notify_bot.send_document(admin_id, file, caption=caption)
                except Exception as e:
                    logger.error(f"Order media to {admin_id} failed: {e}")


async def _deliver_digital(bot: Bot, user_id: int, product: dict):
    """Отправляет файл цифрового товара."""
    if not product.get("file_id"):
        await bot.send_message(user_id, "Файл недоступен. Напиши мне в личку.")
        return

    await bot.send_document(
        chat_id=user_id,
        document=product["file_id"],
        caption=f"<b>{product['name']}</b> — пресет",
        parse_mode="HTML",
    )

    if product.get("instruction_file_id"):
        if product.get("instruction_type") == "photo":
            await bot.send_photo(
                chat_id=user_id, photo=product["instruction_file_id"],
                caption="📄 <b>Инструкция по применению</b>", parse_mode="HTML",
            )
        else:
            await bot.send_document(
                chat_id=user_id, document=product["instruction_file_id"],
                caption="📄 <b>Инструкция по применению</b>", parse_mode="HTML",
            )

    if product.get("video_url"):
        await bot.send_message(
            user_id, f"🎬 <b>Видео-урок:</b> {product['video_url']}", parse_mode="HTML",
        )

    await bot.send_message(user_id, "Удачи! 🌟")


async def _deliver_infobiz(bot: Bot, user_id: int, product: dict, purchase_num: int = 0,
                           username: str | None = None, first_name: str = "",
                           test_mode: bool = False):
    """Выдаёт ссылку с заявкой на вступление — бот одобрит только этого пользователя."""
    channel_id = product.get("channel_id")
    invite_link = product.get("channel_invite_link")

    if channel_id and invite_link:
        if not test_mode:
            # Записываем право доступа — бот одобрит заявку в channel_access handler
            await db.grant_channel_access(user_id, str(channel_id))
        await bot.send_message(
            user_id,
            f"{'🧪 [ТЕСТ] ' if test_mode else ''}"
            f"🔐 <b>Доступ к закрытому каналу</b>\n\n"
            f"Нажми на ссылку ниже и отправь заявку на вступление — "
            f"бот одобрит её автоматически:\n\n"
            f"{invite_link}\n\n"
            f"Ссылку можно использовать повторно (если выйдешь и захочешь вернуться).",
            parse_mode="HTML",
        )
    else:
        logger.warning(f"infobiz product {product['id']}: channel_id или invite_link не заданы")
        await bot.send_message(
            user_id,
            f"{'🧪 [ТЕСТ] ' if test_mode else ''}"
            "Оплата прошла! Доступ к каналу будет открыт в ближайшее время.",
        )

    # Бонус для первых N покупателей — персональный разбор
    bonus_limit = product.get("bonus_limit")
    if bonus_limit and purchase_num and purchase_num <= bonus_limit:
        if not test_mode:
            # Записываем победителя только при реальной покупке
            await db.add_bonus_winner(
                user_id=user_id,
                username=username,
                first_name=first_name,
                product_id=product["id"],
            )
        bonus_text = product.get("bonus_text") or (
            f"🎁 <b>Ты попал в число первых {bonus_limit} покупателей!</b>\n\n"
            f"Ты получаешь персональный разбор своего ролика 🎬\n\n"
            f"Пришли ссылку на свой ролик в любое удобное время — "
            f"я посмотрю и напишу тебе разбор в личку."
        )
        prefix = "🧪 [ТЕСТ] — бонус сработал:\n\n" if test_mode else ""
        await bot.send_message(user_id, prefix + bonus_text, parse_mode="HTML")
        logger.info(
            f"infobiz bonus {'(test) ' if test_mode else ''}user {user_id} "
            f"winner #{purchase_num}/{bonus_limit}, product {product['id']}"
        )

    # Дополнительные материалы (если есть)
    if product.get("file_id"):
        await bot.send_document(
            chat_id=user_id,
            document=product["file_id"],
            caption=f"<b>{product['name']}</b> — материалы",
            parse_mode="HTML",
        )

    if product.get("instruction_file_id"):
        if product.get("instruction_type") == "photo":
            await bot.send_photo(
                chat_id=user_id, photo=product["instruction_file_id"],
                caption="📄 <b>Инструкция</b>", parse_mode="HTML",
            )
        else:
            await bot.send_document(
                chat_id=user_id, document=product["instruction_file_id"],
                caption="📄 <b>Инструкция</b>", parse_mode="HTML",
            )

    if product.get("video_url"):
        await bot.send_message(
            user_id, f"🎬 <b>Видео-урок:</b> {product['video_url']}", parse_mode="HTML",
        )


async def _send_notify(bot: Bot, text: str):
    try:
        notify_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
        for admin_id in config.ADMIN_IDS:
            try:
                await notify_bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Notify admin {admin_id} failed: {e}")
        await notify_bot.session.close()
    except Exception as e:
        logger.error(f"Failed to create notify bot: {e}")


def _back_to_catalog_keyboard():
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ В каталог", callback_data="catalog")]
    ])


def create_app(bot: Bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    # Основной (цифровой) магазин
    app.router.add_post("/prodamus/webhook", partial(handle_webhook, secret=config.PRODAMUS_SECRET))
    # Отдельный магазин для физических товаров (свой секрет; при откате — тот же
    # секрет). Вторым идёт секрет прошлой платёжной страницы: пока она не
    # отключена, оплата по оставшейся у клиента старой ссылке тоже должна пройти
    app.router.add_post(
        "/prodamus/webhook/physical",
        partial(handle_webhook, secret=[config.PRODAMUS_SECRET_PHYSICAL,
                                        config.PRODAMUS_SECRET_PHYSICAL_OLD]),
    )
    return app
