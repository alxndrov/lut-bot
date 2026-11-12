"""Конструктор физтовара: варианты выбираются кнопками в виджете.

Каталог — обычными кнопками. При выборе физтовара бот присылает фото
товара с описанием (catalog.cb_product), а под ним — этот виджет: модель,
цвет кружками, ручка, логотип… По «Оформить заказ» ответы записываются в
опрос так же, как если бы клиент набрал их сам («1», «10», «нет»), и дальше
всё идёт прежним путём (handlers/brief_handler.py): то, что кнопкой не
выбрать (файл логотипа, свой никнейм, своя модель), бот спрашивает в чате,
потом «добавить ещё?», доставка, оплата.

Варианты берутся из services/answer_labels.py (те же номера, что на
картинках-палитрах); вопрос узнаётся по слову в тексте. Вопрос, которого
конструктор не знает, задаётся в чате как раньше.

Стиль — цвета мини-приложения (макет Миши): чёрный, #141414, красный
#e10600; не один в один с макетом, а удобно и современно.

Кому показывать — /productwidget admins|all|off (по умолчанию admins).
"""
import json
import logging
import re

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import InputMediaPhoto, Message, User

import config
import database as db
from handlers.order_widget import COLORS as HEX, api, _cut, _fits
from services.answer_labels import COLORS, MODELS, TEXTURES

logger = logging.getLogger(__name__)
router = Router()

SETTING = "product_widget"
MODES = ("admins", "all", "off")
ASK = "?"      # «спросим в чате»: файл логотипа, свой текст, своя модель


async def _mode() -> str:
    m = await db.get_setting(SETTING)
    return m if m in MODES else "admins"


async def enabled_for(user_id: int) -> bool:
    m = await _mode()
    return m == "all" or (m == "admins" and user_id in config.ADMIN_IDS)


# ── варианты по вопросам ─────────────────────────────────────────────

def section(q: dict) -> dict:
    """Раздел конструктора для вопроса: {"t": подпись, "o": [[текст, ответ, #цвет?]], "n": подсказка}.

    Ответ ASK — вопрос зададим в чате (нужен файл или свой текст).
    Без вариантов ("o" пуст) — вопрос целиком уходит в чат."""
    low = (q.get("text") or "").lower()
    if "цвет" in low:
        opts = [[name.capitalize(), str(n), HEX.get(name, "#888888")] for n, name in COLORS.items()]
        note = "Поп-фильтр — только чёрный" if "поп-фильтр" in low else ""
        return {"t": "Цвет", "o": opts, "n": note, "c": 1}
    if "модел" in low:
        return {"t": "Модель", "o": [[name, str(n)] for n, name in MODELS.items()]
                + [["Другая", ASK]], "n": ""}
    if "логотип" in low:
        return {"t": "Логотип", "o": [["Без логотипа", "нет"], ["С логотипом", ASK]],
                "n": "Файл логотипа пришлёте следующим сообщением"}
    if "никнейм" in low or "надпис" in low:
        return {"t": "Надпись на крышке", "o": [["Без надписи", "нет"], ["С никнеймом", ASK]],
                "n": "Никнейм напишете следующим сообщением"}
    # После логотипа: в его вопросе есть «на нижнем краю ручки»
    if "ручк" in low or "текстур" in low:
        return {"t": "Ручка", "o": [[name.capitalize(), str(n)] for n, name in TEXTURES.items()],
                "n": ""}
    return {"t": _cut(q.get("text") or "Вопрос", 40), "o": [], "n": "Спросим в чате"}


def answers_from(questions: list[dict], picks: list[int]) -> dict[int, str] | None:
    """Выбор в виджете (номер варианта по каждому вопросу) → готовые ответы
    {номер вопроса: текст}. Вопросы с ASK и без вариантов в словарь не
    попадают — их бот задаст в чате. None — выбор не сходится с вопросами."""
    if len(picks) != len(questions):
        return None
    out = {}
    for i, (q, k) in enumerate(zip(questions, picks)):
        opts = section(q)["o"]
        if not opts:
            continue
        if not 0 <= k < len(opts):
            return None
        if opts[k][1] != ASK:
            out[i] = opts[k][1]
    return out


# ── страница ─────────────────────────────────────────────────────────

_CSS = """*{box-sizing:border-box;margin:0}
body{background:#0a0a0a;color:#f2f2f2;font:14px/1.35 -apple-system,system-ui,'Segoe UI',Roboto,sans-serif;padding:14px 14px 8px}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.eb{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#e10600}
h1{font-size:19px;font-weight:800;margin-top:4px;line-height:1.15}
.pr{border:1px solid #262626;border-radius:99px;padding:5px 12px;font-weight:700;white-space:nowrap}
.sc{margin-top:12px}
.lb{display:flex;justify-content:space-between;font-size:12px;color:#7a7a7a;margin-bottom:7px}
.lb span{text-transform:uppercase;letter-spacing:.12em}.lb b{color:#f2f2f2;font-weight:600;letter-spacing:0}
.ch{display:flex;flex-wrap:wrap;gap:6px}
.c{flex:1 1 auto;text-align:center;border:1px solid #262626;background:#141414;border-radius:12px;padding:8px 10px;font-weight:600;font-size:13px;cursor:pointer}
.c.on{background:#f2f2f2;border-color:#f2f2f2;color:#0a0a0a}
.sw{display:grid;grid-template-columns:repeat(10,1fr);gap:5px}
.s{aspect-ratio:1;border-radius:50%;border:2px solid #0a0a0a;box-shadow:0 0 0 1px #3a3a3a;cursor:pointer;justify-self:center;width:100%;max-width:34px}
.s.on{box-shadow:0 0 0 2px #e10600}
.nt{font-size:12px;color:#7a7a7a;margin-top:6px}
.ph{color:#a3a3a3;text-decoration:underline;cursor:pointer}
.go{margin-top:14px;border-radius:99px;padding:13px;text-align:center;font-weight:700;font-size:15px;background:#e10600;color:#fff;cursor:pointer}
.go.off{background:#262626;color:#7a7a7a}
.dn{margin-top:16px;border-radius:14px;padding:12px;text-align:center;background:#141414;border:1px solid #262626;color:#a3a3a3}
#t{font-size:12px;text-align:center;color:#7a7a7a;min-height:16px;margin-top:8px}"""

# P — выбор по разделам (-1 — не выбрано); разделы без вариантов — 0.
# chat_data {"d":1} — заказ уже оформлен из этого виджета: кнопки гасим
_JS = r"""var W=window.TelegramWidget,P=D.s.map(function(s){return s.o.length?-1:0}),busy=0;
function e(x){return String(x).replace(/[&<>"]/g,function(c){return"&#"+c.charCodeAt(0)+";"})}
function H(f,a){try{W.HapticFeedback[f](a)}catch(_){}}
function done(){try{return JSON.parse(W.chatData||"{}").d}catch(_){return 0}}
function R(){var h='<div class="top"><div><div class="eb">Соберите свой</div><h1>'+e(D.n)+'</h1></div><div class="pr">'+e(D.p)+'</div></div>',miss="",dn=done();
D.s.forEach(function(s,i){var k=P[i],v=k>=0&&s.o.length?s.o[k][0]:"";h+='<div class="sc"><div class="lb"><span>'+e(s.t)+'</span><b>'+e(v)+'</b></div>';
if(!s.o.length)h+='<div class="nt">'+e(s.n)+'</div>';
else if(s.c){h+='<div class="sw">';s.o.forEach(function(o,j){h+='<div class="s'+(k==j?' on':'')+'" style="background:'+o[2]+'" onclick="S('+i+','+j+')"></div>'});h+='</div>';
if(s.n)h+='<div class="nt">'+e(s.n)+(D.f[i]?' · <span class="ph" onclick="F('+i+')">палитра на фото</span>':'')+'</div>';else if(D.f[i])h+='<div class="nt"><span class="ph" onclick="F('+i+')">Палитра на фото</span></div>'}
else{h+='<div class="ch">';s.o.forEach(function(o,j){h+='<div class="c'+(k==j?' on':'')+'" onclick="S('+i+','+j+')">'+e(o[0])+'</div>'});h+='</div>';
if(k>=0&&s.o[k][1]=="?"&&s.n)h+='<div class="nt">'+e(s.n)+'</div>'}
h+='</div>';if(k<0&&!miss)miss=s.t});
h+=dn?'<div class="dn">Заказ оформляется — продолжение в чате ниже</div>':'<div class="go'+(miss?' off':'')+'" onclick="G()">'+(miss?'Выберите: '+e(miss.toLowerCase()):'Оформить заказ')+'</div>';
document.getElementById("L").innerHTML=h}
function S(i,j){if(done())return;P[i]=j;H("selectionChanged");M("");R()}
function M(t){document.getElementById("t").textContent=t}
function Q(d,ok){if(!W||busy)return;busy=1;M("…");W.request(d).then(function(a){busy=0;var r={};try{r=JSON.parse(a.result)}catch(_){}M(r.t||"");ok&&ok(r)},function(er){busy=0;M("Не вышло: "+er.code+". Попробуйте ещё раз")})}
function G(){if(done()||P.indexOf(-1)>=0){H("notificationOccurred","warning");return}H("impactOccurred","medium");Q("sv:"+D.i+":"+P.join("."))}
function F(i){H("impactOccurred","light");Q("ph:"+D.i+":"+i)}
R();if(W){W.onEvent&&W.onEvent("chatDataChanged",R);W.ready&&W.ready()}"""


async def build_html(product: dict, questions: list[dict]) -> str:
    price = await db.get_effective_price(product)
    name = re.sub(r"^[^\wА-Яа-яЁё]+", "", product.get("name") or "").strip()
    data = {"i": product["id"], "n": _cut(name, 40), "p": f"{price} ₽",
            "s": [section(q) for q in questions],
            # У вопроса есть фото (палитра, модели) — можно попросить его в чат
            "f": [int(bool(db.question_photos(q))) for q in questions]}
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return (f"<style>{_CSS}</style><div id=\"L\"></div><div id=\"t\"></div>"
            f"<script>var D={payload};{_JS}</script>")


async def send(target: Message, product: dict) -> bool:
    """Конструктор под карточкой товара. False — не наш случай или не принял."""
    if not await enabled_for(target.chat.id) or db.is_stock_product(product):
        return False
    questions = await db.get_product_questions(product["id"])
    # Конструктор нужен, только если хоть что-то выбирается кнопками
    if not questions or not any(section(q)["o"] for q in questions):
        return False
    page = await build_html(product, questions)
    if not _fits(page):
        logger.warning(f"product widget {product['id']}: не влезает в 4 КБ")
        return False
    try:
        await api(target.bot, "sendWidget", chat_id=target.chat.id, html=page,
                  chat_data="{}", allow_queries=True)
        return True
    except Exception as e:
        logger.warning(f"product widget sendWidget {product['id']}: {e}")
        return False


# ── нажатия ──────────────────────────────────────────────────────────

async def handle_query(bot, q: dict, storage):
    """sv:<товар>:<выбор.через.точку> — оформить; ph:<товар>:<вопрос> — фото вопроса."""
    from handlers.catalog_widget import _widget_message
    note, warn = "", 0
    try:
        user = User.model_validate(q["from"])
        chat = q["chat"]
        kind, pid, rest = ((q.get("data") or "") + "::").split(":")[:3]
        product = await db.get_product(int(pid)) if pid.isdigit() else None
        questions = await db.get_product_questions(product["id"]) if product else []
        msg = _widget_message(bot, chat, q["message_id"])
        if not questions:
            note, warn = "Товар не найден — откройте каталог заново: /catalog", 1
        elif kind == "ph":
            i = int(rest) if rest.isdigit() else -1
            photos = db.question_photos(questions[i]) if 0 <= i < len(questions) else []
            if len(photos) > 1:
                await bot.send_media_group(chat["id"], [InputMediaPhoto(media=p) for p in photos[:10]])
            elif photos:
                await bot.send_photo(chat["id"], photos[0])
            note = "Фото — в чате ниже" if photos else "Фото нет"
        elif kind == "sv":
            picks = [int(x) for x in rest.split(".")] if re.fullmatch(r"\d+(\.\d+)*", rest) else []
            answers = answers_from(questions, picks)
            state = FSMContext(storage=storage, key=StorageKey(
                bot_id=bot.id, chat_id=chat["id"], user_id=user.id))
            from handlers.brief_handler import submit_widget_answers
            if answers is None:
                note, warn = "Вопросы товара изменились — откройте товар заново", 1
            elif not await submit_widget_answers(msg, state, bot, user, product["id"], answers):
                note, warn = "Этот выбор уже неактуален — откройте товар заново: /catalog", 1
            else:
                note = "Готово — продолжение в чате"
                try:
                    await api(bot, "editMessageWidgetData", chat_id=chat["id"],
                              message_id=q["message_id"], chat_data='{"d":1}')
                except Exception as e:
                    logger.debug(f"product widget lock: {e}")
    except Exception as e:
        logger.exception(f"product widget_query {q.get('data')}: {e}")
        note, warn = "Не получилось — попробуйте ещё раз", 1
    try:
        await api(bot, "answerWidgetQuery", widget_query_id=q["id"],
                  data=json.dumps({"t": _cut(note, 200), "a": warn}, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"answerWidgetQuery: {e}")


@router.message(Command("productwidget"))
async def cmd_product_widget(message: Message, command: CommandObject):
    """/productwidget admins|all|off — кому выбирать варианты физтовара кнопками."""
    if message.from_user.id not in config.ADMIN_IDS:
        return
    arg = (command.args or "").strip().lower()
    if arg in MODES:
        await db.set_setting(SETTING, arg)
    names = {"admins": "только админам", "all": "всем покупателям", "off": "никому"}
    await message.answer(
        f"Конструктор физтоваров (варианты кнопками): {names[await _mode()]}.\n"
        "Сменить: /productwidget admins | all | off")
