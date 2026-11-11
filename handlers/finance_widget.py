"""
«Финансы» в админском боте — сводкой-виджетом (Bot API 10.4) над прежним меню.

/finance присылает виджет с тремя вкладками:
- 💳 Платежи — сегодня / 7 дней / 30 дней / всё время: продажи, брутто,
  комиссия, НДС, чистыми (то же, что «Принятые платежи»);
- 🤝 Расчёт — взаиморасчёт с прошлого расчёта: из чего сложилось «чистыми»
  и сколько осталось выплатить Мише и Дане (как «Взаиморасчёт»);
- 💰 Касса — на счету по учёту, резервы СДЭК и НПД, ещё не пришедшее
  от Prodamus и свободно к выплате.
«🔄 Обновить» пересчитывает всё и перерисовывает виджет.

Цифры лежат в chat_data сообщения (до 1024 байт), подписи — в HTML.
Действия (выплаты, расчёт, НПД, СДЭК, расходы, сводки) остаются обычным
меню кнопками следом за виджетом — там ввод сумм и шаги, в виджет они
не переносятся. Telegram виджет не принял — только меню, как раньше.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import config
import database as db
from handlers.order_widget import api, _cut

logger = logging.getLogger(__name__)

PREFIX = "fin:"
BATCH = "finance"


def _r(x) -> float | int:
    x = round(float(x or 0), 2)
    return int(x) if x == int(x) else x


async def snapshot() -> dict:
    """Все цифры сводки — компактно, для chat_data."""
    from handlers import finance as fin
    from services.daily_report import fetch_payments_summary

    now_utc = datetime.now(timezone.utc)
    today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    def iso(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    periods = await asyncio.gather(
        fetch_payments_summary(iso(today), iso(now_utc)),
        fetch_payments_summary(iso(now_utc - timedelta(days=7)), iso(now_utc)),
        fetch_payments_summary(iso(now_utc - timedelta(days=30)), iso(now_utc)),
        fetch_payments_summary("2020-01-01T00:00:00.000Z", iso(now_utc)),
        return_exceptions=True)
    pay = []
    for p in periods:
        if isinstance(p, Exception) or not p:
            logger.error(f"finance widget: платежи: {p}")
            pay.append(None)
        else:
            pay.append([p["count"], _r(p["gross"]), _r(p["fee"]), _r(p["vat"]), _r(p["net"])])

    out = {"ts": datetime.now(fin.MSK).strftime("%d.%m %H:%M"), "p": pay, "s": None, "c": None}

    s, last, *_ = await fin._settle_split()
    cash = await fin._free_cash()
    if s:
        since = ""
        if last:
            y, m, d = last["settled_at"][:10].split("-")
            since = f"{d}.{m}"
        st = {"since": since,
              "n": s["count"], "g": _r(s["gross"]), "ph": _r(s["physical"]),
              "dg": _r(s["digital"]), "dl": _r(s["delivery"]), "rf": _r(s.get("refunds")),
              "fe": _r(s["fee"]), "np": _r(s["npd"]), "do": _r(s.get("delivery_out")),
              "ex": _r(s["expenses"]), "nt": _r(s["net"]),
              "ol": _r(s.get("owner_left", s["owner"])), "pl": _r(s.get("partner_left", s["partner"]))}
        if cash is not None and (st["ol"] > 0 or st["pl"] > 0):
            po, pp = fin._payable(s, cash["free"])
            st["po"], st["pp"] = _r(po), _r(pp)
        out["s"] = st
    if cash is not None:
        out["c"] = {"oa": _r(cash["on_account"]), "cr": _r(cash["cdek_reserve"]),
                    "tr": _r(cash["tax_reserve"]), "pd": _r(cash.get("pending")),
                    "fr": _r(cash["free"])}
    return out


def _data(snap: dict) -> str:
    raw = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode()) > 1024:      # не влезло — жертвуем «всё время»
        snap = dict(snap, p=snap["p"][:3])
        raw = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
    return raw


_CSS = """*{box-sizing:border-box;margin:0}
body{font:15px/1.35 system-ui,sans-serif;color:var(--tg-theme-text-color);padding:10px 10px 4px}
.hd{display:flex;justify-content:space-between;align-items:center;margin:0 2px 8px}
h3{font-size:16px}.ts{font-size:12px;color:var(--tg-theme-hint-color)}
.tb{display:flex;gap:6px;margin-bottom:8px}
.tab{flex:1;text-align:center;padding:7px 4px;border-radius:10px;font-size:14px;background:var(--tg-theme-secondary-bg-color)}
.tab.on{background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color)}
.ch{display:flex;gap:4px;margin-bottom:8px}
.ch div{flex:1;text-align:center;font-size:12px;padding:4px 2px;border-radius:8px;color:var(--tg-theme-hint-color)}
.ch div.on{background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-text-color);font-weight:600}
.card{background:var(--tg-theme-secondary-bg-color);border-radius:14px;padding:10px 12px;margin-bottom:8px}
.big{font-size:24px;font-weight:700}.lbl{font-size:12px;color:var(--tg-theme-hint-color)}
.row{display:flex;justify-content:space-between;font-size:14px;margin-top:4px}
.row span:first-child{color:var(--tg-theme-hint-color)}
.neg{color:var(--tg-theme-destructive-text-color)}.ok{color:#2e9e44}
.two{display:flex;gap:8px}.two .card{flex:1}
.b{padding:8px;border-radius:10px;text-align:center;font-size:13px;background:var(--tg-theme-bg-color);color:var(--tg-theme-link-color)}
#t{font-size:12px;text-align:center;color:var(--tg-theme-hint-color);min-height:16px;margin-top:4px}"""

_JS = r"""var W=window.TelegramWidget,tab=0,per=2,busy=0;
var PL=["Сегодня","7 дней","30 дней","Всё время"];
function e(x){return String(x).replace(/[&<>"]/g,function(c){return"&#"+c.charCodeAt(0)+";"})}
function F(x){x=+x||0;var s=Math.abs(x).toFixed(2).replace(/\.00$/,""),p=s.split(".");p[0]=p[0].replace(/\B(?=(\d{3})+(?!\d))/g," ");return(x<0?"−":"")+p.join(",")+" ₽"}
function Rw(l,v,c){return'<div class="row"><span>'+e(l)+'</span><span'+(c?' class="'+c+'"':'')+'>'+v+'</span></div>'}
function M2(v){return v?"−"+F(v):F(0)}
function R(){var d={};try{d=JSON.parse((W&&W.chatData)||"{}")}catch(_){}
if(!d.ts){document.getElementById("L").innerHTML='<div class="hd"><h3>💳 Финансы</h3></div><div class="card lbl">Считаю цифры…</div>';return}
var h='<div class="hd"><h3>💳 Финансы</h3><span class="ts">'+e(d.ts||"")+' МСК</span></div><div class="tb">';
["💳 Платежи","🤝 Расчёт","💰 Касса"].forEach(function(t,i){h+='<div class="tab'+(i==tab?' on':'')+'" onclick="T('+i+')">'+t+'</div>'});h+='</div>';
if(tab==0){h+='<div class="ch">';PL.forEach(function(t,i){if(d.p&&i<d.p.length)h+='<div class="'+(i==per?'on':'')+'" onclick="P('+i+')">'+t+'</div>'});h+='</div>';
var p=d.p&&d.p[per];if(!p)h+='<div class="card lbl">Нет данных за период</div>';else if(!p[0])h+='<div class="card"><div class="big">0 ₽</div><div class="lbl">Продаж не было</div></div>';
else h+='<div class="card"><div class="lbl">Чистыми</div><div class="big">'+F(p[4])+'</div>'+Rw("Продаж",p[0])+Rw("Брутто",F(p[1]))+Rw("Комиссия",M2(p[2]))+Rw("НДС",F(p[3]))+'</div>'}
if(tab==1){var s=d.s;if(!s)h+='<div class="card lbl">Не удалось посчитать — открой «🤝 Взаиморасчёт» в меню ниже</div>';else{
h+='<div class="card"><div class="lbl">Чистыми '+(s.since?"с "+e(s.since):"за всё время")+'</div><div class="big">'+F(s.nt)+'</div>'+Rw("Продаж · принято",s.n+" · "+F(s.g));
if(s.ph)h+=Rw("  товар",F(s.ph));if(s.dg)h+=Rw("  цифра",F(s.dg));if(s.dl)h+=Rw("  доставка",F(s.dl));if(s.rf)h+=Rw("Возвраты",M2(s.rf));
h+=Rw("Комиссия",M2(s.fe))+Rw("НПД",M2(s.np));if(s.do)h+=Rw("СДЭК",M2(s.do));if(s.ex)h+=Rw("Расходы",M2(s.ex));h+='</div><div class="two">';
[[ON,s.ol,s.po],[PN,s.pl,s.pp]].forEach(function(q){h+='<div class="card"><div class="lbl">👤 '+e(q[0])+'</div><div class="big" style="font-size:19px">'+F(q[1])+'</div>';
if(q[2]!=null&&q[2]<q[1]-0.005)h+='<div class="lbl">сейчас можно '+F(q[2])+'</div>';h+='</div>'});h+='</div>'}}
if(tab==2){var c=d.c;if(!c)h+='<div class="card lbl">Не удалось посчитать — сверься с таблицей</div>';else{
h+='<div class="card"><div class="lbl">Свободно к выплате</div><div class="big '+(c.fr>0?'ok':'neg')+'">'+F(c.fr)+'</div>'+Rw("На счету по учёту",F(c.oa))+Rw("Отложено на СДЭК",M2(c.cr))+Rw("Отложено на НПД",M2(c.tr));
if(c.pd)h+=Rw("Ещё не пришло от Prodamus",M2(c.pd));if(c.fr<=0)h+='<div class="lbl neg" style="margin-top:6px">Не хватает даже на СДЭК и налог — выплаты лучше приостановить</div>';h+='</div>'}}
h+='<div class="b" onclick="U()">🔄 Обновить</div>';document.getElementById("L").innerHTML=h}
function H(f,a){try{W.HapticFeedback[f](a)}catch(_){}}
function T(i){tab=i;H("selectionChanged");R()}
function P(i){per=i;H("selectionChanged");R()}
function M(t){document.getElementById("t").textContent=t}
function U(){if(!W||busy)return;busy=1;M("Считаю…");W.request("fin:r").then(function(a){busy=0;var r={};try{r=JSON.parse(a.result)}catch(_){}M(r.t||"")},function(er){busy=0;M("Не вышло: "+er.code)})}
if(W){W.onEvent("chatDataChanged",R);W.ready&&W.ready()}R();"""


def build_html() -> str:
    names = json.dumps([config.OWNER_NAME, config.PARTNER_NAME], ensure_ascii=False)
    return (f"<style>{_CSS}</style><div id=\"L\"></div><div id=\"t\"></div>"
            f"<script>var ON={names}[0],PN={names}[1];{_JS}</script>")


async def send(message) -> bool:
    """Сводка виджетом вместо прошлой. False — Telegram не принял."""
    chat, uid = message.chat.id, message.from_user.id
    for mid in await db.get_admin_command_messages(BATCH, chat, uid):
        try:
            await message.bot.delete_message(chat, mid)
        except Exception:
            pass
    await db.clear_admin_command_messages(BATCH, chat, uid)
    # Открывается сразу, цифры — следом: расчёт идёт секунд 5–10
    # (Prodamus, таблица), а меню «Финансы» должно появляться мгновенно
    try:
        sent = await api(message.bot, "sendWidget", chat_id=chat, html=build_html(),
                         chat_data="{}", allow_queries=True)
    except Exception as e:
        logger.warning(f"finance sendWidget: {e}")
        return False
    await db.replace_admin_command_messages(BATCH, chat, uid, [sent["message_id"]])
    asyncio.create_task(_fill(message.bot, chat, sent["message_id"]))
    return True


async def _fill(bot, chat_id: int, message_id: int):
    try:
        await api(bot, "editMessageWidgetData", chat_id=chat_id, message_id=message_id,
                  chat_data=_data(await snapshot()))
    except Exception as e:
        logger.warning(f"finance widget fill: {e}")


async def handle_query(bot, q: dict):
    note = ""
    try:
        if q["from"]["id"] not in config.ADMIN_IDS:
            note = "Только для администраторов."
        elif q.get("data") == PREFIX + "r" and q.get("chat"):
            try:
                await api(bot, "editMessageWidgetData", chat_id=q["chat"]["id"],
                          message_id=q["message_id"], chat_data=_data(await snapshot()))
                note = "Обновлено"
            except Exception as e:
                note = "Уже актуально" if "not modified" in str(e) else "Не обновилось"
                if "not modified" not in str(e):
                    logger.warning(f"finance widget refresh: {e}")
    except Exception as e:
        logger.exception(f"finance widget_query: {e}")
        note = "Ошибка — посмотри в меню ниже"
    try:
        await api(bot, "answerWidgetQuery", widget_query_id=q["id"],
                  data=json.dumps({"t": _cut(note, 100)}, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"answerWidgetQuery: {e}")
