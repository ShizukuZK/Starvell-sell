"""Модуль SMM: заказ на Starvell -> ссылка от покупателя -> заказ в OptSMM.

Работает рядом с автоарендой в одном процессе. Поллер сначала отдаёт каждый
новый заказ и сообщение этому модулю; если лот не SMM (не подходит ни одно
правило), всё уходит в движок аренды как раньше.

Правило связывает лот Starvell (по словам из заголовка — offerId площадка
продавцу не отдаёт) с услугой OptSMM:
    {"id": "a1b2", "name": "TG подписчики", "match": "подписчик, telegram",
     "service": 123, "per_unit": 100, "link": "tg", "enabled": true}
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from core.log import get_logger
from core.optsmm import DONE, FAILED, PARTIAL, OptSmmAPI, OptSmmError
from core.starvell import StarvellAPI, StarvellError
from core.storage import DATA_DIR, Store

logger = get_logger("smm")

SMM_FILE = os.path.join(DATA_DIR, "smm.json")
_LOCK = threading.RLock()

# тип ссылки -> (regex допустимой ссылки, префикс для @username, подсказка)
LINK_TYPES: Dict[str, tuple] = {
    "tg": (r"(t\.me|telegram\.me)/", "https://t.me/",
           "Нужна ссылка на открытый канал/группу/пост: https://t.me/... или @username"),
    "ig": (r"instagram\.com/", "https://instagram.com/",
           "Нужна ссылка на открытый профиль или пост Instagram"),
    "vk": (r"vk\.(com|ru)/", "https://vk.com/", "Нужна ссылка на страницу, группу или пост ВКонтакте"),
    "tt": (r"tiktok\.com/", "https://www.tiktok.com/@", "Нужна ссылка на профиль или видео TikTok"),
    "yt": (r"(youtube\.com|youtu\.be)/", "", "Нужна ссылка на канал или видео YouTube"),
    "twitch": (r"twitch\.tv/", "https://twitch.tv/", "Нужна ссылка на канал Twitch"),
    "any": (r"", "", "Пришлите ссылку"),
}
LINK_TITLES = {"tg": "Telegram", "ig": "Instagram", "vk": "VK", "tt": "TikTok",
               "yt": "YouTube", "twitch": "Twitch", "any": "любая"}

_LINK_RE = re.compile(
    r"(https?://\S+|(?:t\.me|telegram\.me|vk\.com|vk\.ru|instagram\.com|tiktok\.com|"
    r"youtube\.com|youtu\.be|twitch\.tv)/\S+|@[A-Za-z0-9_.]{3,32})", re.I)

ACTIVE_STATES = ("ASKED", "QUEUED", "WAIT_BALANCE", "PLACED")
STATE_ICON = {"ASKED": "⏳", "QUEUED": "🕒", "WAIT_BALANCE": "💸", "PLACED": "🚀", "DONE": "✅",
              "PARTIAL": "⚠️", "FAILED": "❌", "REFUNDED": "↩️", "MANUAL": "✋", "CANCELLED": "🚫"}
STATE_TITLE = {"ASKED": "ждём ссылку", "QUEUED": "в очереди", "WAIT_BALANCE": "ждём баланс OptSMM",
               "PLACED": "выполняется", "DONE": "выполнен", "PARTIAL": "выполнен частично",
               "FAILED": "ошибка", "REFUNDED": "возврат", "MANUAL": "нужна ручная обработка",
               "CANCELLED": "отменён"}


def extract_link(text: str, link_type: str = "any") -> Optional[str]:
    pattern, at_prefix, _ = LINK_TYPES.get(link_type, LINK_TYPES["any"])
    for m in _LINK_RE.finditer(text or ""):
        link = m.group(1).rstrip(".,;!?)»\"'")
        if link.startswith("@"):
            if not at_prefix:
                continue
            link = at_prefix + link[1:]
        elif not link.lower().startswith("http"):
            link = "https://" + link
        if pattern and not re.search(pattern, link, re.I):
            continue
        return link
    return None


def rule_matches(rule: Dict[str, Any], title: str) -> bool:
    match = str(rule.get("match") or "").strip()
    if not match or not rule.get("enabled", True):
        return False
    text = (title or "").lower().replace("ё", "е")
    if match.lower().startswith("re:"):
        try:
            return bool(re.search(match[3:].strip(), title or "", re.I))
        except re.error:
            return False
    words = [w.strip().lower().replace("ё", "е") for w in match.split(",") if w.strip()]
    return bool(words) and all(w in text for w in words)


def order_title(order: Dict[str, Any]) -> str:
    od = order.get("offerDetails") or {}
    descr = (od.get("descriptions") or {}).get("rus") or {}
    return str(descr.get("briefDescription") or od.get("title") or "").strip()


def offer_title(offer: Optional[Dict[str, Any]]) -> str:
    if not offer:
        return ""
    return str(offer.get("briefDescription")
               or ((offer.get("descriptions") or {}).get("rus") or {}).get("briefDescription")
               or "")


class SmmEngine:
    def __init__(self, store: Store, sv: StarvellAPI, api: OptSmmAPI, *,
                 notify_admin: Optional[Callable[[str], Any]] = None,
                 stats: Any = None, path: str = SMM_FILE) -> None:
        self.store = store
        self.sv = sv
        self.api = api
        self.stats = stats
        self.path = path
        self._notify_admin = notify_admin
        self.rules: List[Dict[str, Any]] = []
        self.orders: Dict[str, Dict[str, Any]] = {}
        self._early: Dict[str, tuple] = {}         # chat_id -> (текст, ts) ссылки до обнаружения заказа
        self._last_hint: Dict[str, float] = {}     # chat_id -> ts последней подсказки «нет ссылки»
        self._bal_warned = 0.0
        self._lock = asyncio.Lock()
        self.last_error: Optional[str] = None
        self._load()

    # ---------- хранение ----------

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        self.rules = data.get("rules") or []
        self.orders = data.get("orders") or {}

    def save(self) -> None:
        with _LOCK:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            # храним не больше 2000 завершённых заказов
            if len(self.orders) > 2000:
                done = sorted((o for o in self.orders.values() if o["state"] not in ACTIVE_STATES),
                              key=lambda o: o.get("created", 0))
                for o in done[:len(self.orders) - 2000]:
                    self.orders.pop(o["sv_id"], None)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"rules": self.rules, "orders": self.orders}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    # ---------- утилиты ----------

    @property
    def enabled(self) -> bool:
        return bool(self.store.get("smm_enabled", True)) and self.api.configured and bool(self.rules)

    def dry(self) -> bool:
        return bool(self.store.get("dry_run", False))

    async def admin(self, text: str) -> None:
        logger.info("ADMIN: %s", text.replace("\n", " ")[:300])
        if self._notify_admin:
            try:
                result = self._notify_admin(text)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning("не удалось уведомить админа: %s", exc)

    def t(self, key: str, **kw: Any) -> str:
        from core.texts import render
        return render(self.store.texts.get(key, ""), **kw)

    async def say(self, chat_id: Optional[str], text: str, order: bool = True) -> None:
        if not chat_id or not text:
            return
        if order and self.dry():
            logger.info("[ТЕСТ] SMM в чат %s: %s", chat_id, text.replace("\n", " ")[:160])
            return
        try:
            await self.sv.send_message(chat_id, text)
        except StarvellError as exc:
            logger.warning("SMM: не отправлено в чат %s: %s", chat_id, exc)

    def to_rub(self, amount: float, currency: str) -> float:
        if (currency or "").upper() in ("USD", "$"):
            return amount * float(self.store.get("smm_usd_rate", 90) or 90)
        return amount

    def rule_for(self, title: str) -> Optional[Dict[str, Any]]:
        for rule in self.rules:
            if rule_matches(rule, title):
                return rule
        return None

    def rule_by_id(self, rid: str) -> Optional[Dict[str, Any]]:
        return next((r for r in self.rules if r.get("id") == rid), None)

    def _rule_of(self, o: Dict[str, Any]) -> Dict[str, Any]:
        return self.rule_by_id(o.get("rule_id") or "") or {"link": o.get("link_type") or "any"}

    def _hint(self, o_or_rule: Dict[str, Any]) -> str:
        lt = o_or_rule.get("link") or o_or_rule.get("link_type") or "any"
        return o_or_rule.get("hint") or LINK_TYPES.get(lt, LINK_TYPES["any"])[2]

    def by_state(self, *states: str) -> List[Dict[str, Any]]:
        return sorted((o for o in self.orders.values() if o["state"] in states),
                      key=lambda o: o.get("created", 0))

    def _set(self, o: Dict[str, Any], **changes: Any) -> None:
        o.update(changes, updated=time.time())
        self.save()

    # ---------- правила ----------

    def add_rule(self, match: str, service: int, per_unit: int, link: str, name: str = "") -> Dict[str, Any]:
        rule = {"id": uuid.uuid4().hex[:8], "name": name or match, "match": match,
                "service": int(service), "per_unit": max(1, int(per_unit)),
                "link": link if link in LINK_TYPES else "any", "enabled": True}
        self.rules.append(rule)
        self.save()
        return rule

    def delete_rule(self, rid: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.get("id") != rid]
        self.save()
        return len(self.rules) != before

    # ---------- вход: заказ ----------

    async def handle_order(self, order: Dict[str, Any]) -> bool:
        """True — заказ SMM и обработан здесь; False — отдать движку аренды."""
        if not self.enabled:
            return False
        title = order_title(order)
        rule = self.rule_for(title)
        if not rule:
            return False
        order_id = str(order.get("id") or "")
        if not order_id:
            return False
        async with self._lock:
            if order_id in self.orders or self.store.is_handled(order_id):
                self.store.mark_handled(order_id)
                return True
            try:
                await self._new_order(order, rule, title)
            except Exception as exc:
                logger.exception("SMM: ошибка заказа %s", order_id)
                await self.admin(f"⚠️ SMM: ошибка обработки заказа {order_id}: {exc}")
        return True

    async def _new_order(self, order: Dict[str, Any], rule: Dict[str, Any], title: str) -> None:
        order_id = str(order["id"])
        short_id = order.get("shortId") or order_id[:8]
        details = await self.sv.get_order(order_id)
        info = details.get("order") or {}
        chat_id = str((details.get("chat") or {}).get("id") or "")
        buyer_id = str(info.get("buyerId") or order.get("buyerId") or (order.get("user") or {}).get("id") or "")
        buyer = ((info.get("buyer") or {}).get("username")
                 or (order.get("user") or {}).get("username") or "покупатель")
        units = int(order.get("quantity") or 1)
        qty = units * int(rule.get("per_unit", 1))
        revenue = float(order.get("basePrice") or order.get("totalPrice") or 0) / 100
        paid = float(order.get("totalPrice") or 0) / 100

        self.store.mark_handled(order_id)
        o = {"sv_id": order_id, "short": short_id, "state": "ASKED", "rule_id": rule["id"],
             "rule_name": rule.get("name"), "service": int(rule["service"]),
             "link_type": rule.get("link", "any"), "hint": rule.get("hint", ""),
             "units": units, "qty": qty, "chat_id": chat_id, "buyer_id": buyer_id, "buyer": buyer,
             "title": title, "revenue": revenue, "cost": 0.0, "link": "", "smm_id": None,
             "smm_status": "", "reminded": 0, "created": time.time(), "asked": time.time(),
             "updated": time.time(), "note": ""}
        self.orders[order_id] = o
        self.save()

        if self.store.get("notify_sales", True):
            await self.admin(
                f"📣 SMM-продажа {short_id}\n{rule.get('name')}: {qty} шт → услуга #{rule['service']}\n"
                f"Покупатель: {buyer} · {paid:.2f} ₽\nhttps://starvell.com/order/{order_id}")

        # проверка лимитов услуги
        svc = await self._service(o)
        if svc is None and o["state"] == "MANUAL":
            return
        smin = int((svc or {}).get("min") or 1)
        smax = int((svc or {}).get("max") or qty)
        if svc and (qty < smin or qty > smax):
            reason = f"{qty} шт вне лимитов услуги ({smin}–{smax})"
            await self.say(chat_id, self.t("smm_limits", qty=qty, min=smin, max=smax))
            if self.store.get("smm_auto_refund", True) and qty < smin:
                await self._refund(o, reason)
            else:
                self._set(o, state="MANUAL", note=reason)
                await self.admin(f"✋ SMM {short_id}: {reason}. Решите вручную.")
            return

        busy = any(x["chat_id"] == chat_id and x["sv_id"] != order_id
                   for x in self.by_state("ASKED")) if chat_id else False
        if busy:
            self._set(o, state="QUEUED")
            return

        # ссылку могли прислать ещё до того, как бот увидел заказ
        early = self._early.pop(chat_id, None)
        if early and time.time() - early[1] < 900:
            link = extract_link(early[0], o["link_type"])
            if link:
                self._set(o, link=link)
                await self.place(o)
                return
        await self._ask(o)

    async def _ask(self, o: Dict[str, Any]) -> None:
        self._set(o, state="ASKED", asked=time.time())
        await self.say(o["chat_id"], self.t("smm_ask", qty=o["qty"], hint=self._hint(o),
                                            name=o.get("rule_name") or ""))

    async def _service(self, o: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            svc = await self.api.service(o["service"])
        except OptSmmError as exc:
            self.last_error = str(exc)
            await self.admin(f"⚠️ SMM {o['short']}: OptSMM недоступен ({exc}). Повторю позже.")
            return None
        if not svc:
            self._set(o, state="MANUAL", note="услуга не найдена")
            await self.admin(f"❌ SMM {o['short']}: услуги #{o['service']} нет в OptSMM. "
                             f"Проверьте правило «{o.get('rule_name')}».")
        return svc

    # ---------- вход: сообщение ----------

    async def handle_message(self, chat_id: str, author_id: Any, content: str, *,
                             offer: Optional[Dict[str, Any]] = None) -> bool:
        """True — сообщение относится к SMM и уже обработано."""
        if not self.enabled:
            return False
        author = str(author_id)
        text = (content or "").strip()
        pending = [o for o in self.by_state("ASKED") if o["chat_id"] == chat_id and o["buyer_id"] == author]
        if pending:
            o = pending[0]
            link = extract_link(text, o["link_type"])
            if link:
                self._set(o, link=link)
                await self.place(o)
            elif time.time() - self._last_hint.get(chat_id, 0) > 60:
                self._last_hint[chat_id] = time.time()
                await self.say(chat_id, self.t("smm_bad_link", hint=self._hint(o)))
            return True

        # ссылка до того, как поллер заказов увидел оплату
        if _LINK_RE.search(text):
            self._early[chat_id] = (text, time.time())
            if any(extract_link(text, r.get("link", "any")) and r.get("link") != "any"
                   for r in self.rules if r.get("enabled", True)):
                return True       # это SMM-ссылка — приветствие аренды не нужно

        # сообщение со страницы SMM-лота — своё приветствие вместо арендного
        rule = self.rule_for(offer_title(offer))
        if rule:
            greeted = self.store.state.setdefault("smm_greeted", {})
            cooldown = float(self.store.get("greeting_cooldown_hours", 24)) * 3600
            if time.time() - float(greeted.get(author) or 0) > cooldown:
                greeted[author] = time.time()
                self.store.save_state()
                await self.say(chat_id, self.t("smm_greeting", hint=self._hint(rule),
                                               name=rule.get("name") or ""), order=False)
            return True
        return False

    # ---------- запуск в OptSMM ----------

    async def place(self, o: Dict[str, Any]) -> bool:
        svc = await self._service(o)
        if not svc:
            return False
        rate = float(svc.get("rate") or 0)
        cost = rate * int(o["qty"]) / 1000
        try:
            balance, currency = await self.api.balance()
        except OptSmmError as exc:
            self.last_error = str(exc)
            self._set(o, state="WAIT_BALANCE", note=str(exc))
            return False
        if balance < cost:
            if o["state"] != "WAIT_BALANCE":
                self._set(o, state="WAIT_BALANCE")
                await self.say(o["chat_id"], self.t("smm_wait"))
            if time.time() - self._bal_warned > 1800:
                self._bal_warned = time.time()
                await self.admin(f"💸 Не хватает баланса OptSMM: {balance:.2f} {currency}, "
                                 f"нужно {cost:.2f}. Пополните — заказ {o['short']} запустится сам.")
            return False
        if self.dry():
            logger.info("[ТЕСТ] OptSMM add service=%s qty=%s link=%s", o["service"], o["qty"], o["link"])
            self._set(o, state="DONE", note="тестовый режим", cost=self.to_rub(cost, currency))
            return True
        try:
            smm_id = await self.api.add(o["service"], o["link"], int(o["qty"]))
        except OptSmmError as exc:
            self._set(o, state="MANUAL", note=str(exc))
            await self.admin(f"❌ OptSMM отклонил заказ {o['short']}: {exc}\nСсылка: {o['link']}\n"
                             f"Исправить: /smm_link {o['sv_id']} &lt;ссылка&gt;")
            await self.say(o["chat_id"], self.t("smm_failed"))
            return False
        self._set(o, state="PLACED", smm_id=smm_id, cost=self.to_rub(cost, currency), note="")
        await self.say(o["chat_id"], self.t("smm_started", smm_id=smm_id, link=o["link"], qty=o["qty"]))
        await self.admin(f"🚀 SMM {o['short']} → OptSMM #{smm_id} ({o['qty']} шт, ~{cost:.2f} {currency})")
        await self._promote_queue(o["chat_id"])
        return True

    async def _promote_queue(self, chat_id: str) -> None:
        for q in self.by_state("QUEUED"):
            if q["chat_id"] == chat_id:
                await self._ask(q)
                return

    async def _refund(self, o: Dict[str, Any], reason: str) -> bool:
        if self.dry():
            logger.info("[ТЕСТ] SMM возврат %s (%s)", o["sv_id"], reason)
            self._set(o, state="REFUNDED", note=reason)
            return True
        try:
            await self.sv.refund_order(o["sv_id"])
        except StarvellError as exc:
            self._set(o, state="FAILED", note=f"{reason}; возврат не прошёл: {exc}")
            await self.admin(f"⚠️ SMM {o['short']}: возврат не прошёл ({exc}). Причина: {reason}")
            return False
        self._set(o, state="REFUNDED", note=reason)
        await self.say(o["chat_id"], self.t("smm_refunded"))
        await self.admin(f"↩️ SMM {o['short']}: возврат. Причина: {reason}")
        self._record(o, "refund")
        return True

    def _record(self, o: Dict[str, Any], kind: str) -> None:
        if self.stats is None or self.dry():
            return
        try:
            self.stats.record(order_id=o["sv_id"], game=f"SMM: {o.get('rule_name') or o['service']}",
                              quantity=int(o.get("units") or 1), hours=0,
                              revenue_rub=float(o.get("revenue") or 0),
                              cost_rub=float(o.get("cost") or 0), kind=kind, buyer=o.get("buyer", ""))
        except Exception as exc:
            logger.debug("статистика SMM: %s", exc)

    # ---------- фоновые проверки ----------

    async def tick(self) -> None:
        if not self.api.configured:
            return
        async with self._lock:
            for o in self.by_state("WAIT_BALANCE"):
                if not await self.place(o):
                    break
            await self._track()
            await self._reminders()

    async def _track(self) -> None:
        placed = [o for o in self.by_state("PLACED") if o.get("smm_id")]
        if not placed:
            return
        try:
            statuses = await self.api.status_many([int(o["smm_id"]) for o in placed])
        except OptSmmError as exc:
            self.last_error = str(exc)
            logger.warning("SMM статусы: %s", exc)
            return
        self.last_error = None
        for o in placed:
            st = statuses.get(int(o["smm_id"]))
            if not isinstance(st, dict):
                continue
            status = str(st.get("status") or "")
            changes: Dict[str, Any] = {}
            if st.get("charge") not in (None, ""):
                try:
                    changes["cost"] = self.to_rub(float(st["charge"]), str(st.get("currency") or ""))
                except (TypeError, ValueError):
                    pass
            if status == o.get("smm_status") and not changes:
                continue
            changes["smm_status"] = status
            if status in DONE:
                self._set(o, state="DONE", **changes)
                self._record(o, "smm")
                await self.say(o["chat_id"], self.t("smm_done"))
                profit = float(o.get("revenue") or 0) - float(o.get("cost") or 0)
                await self.admin(f"✅ SMM {o['short']} выполнен · прибыль ≈ {profit:.2f} ₽")
            elif status in PARTIAL:
                self._set(o, state="PARTIAL", **changes)
                self._record(o, "smm")
                await self.say(o["chat_id"], self.t("smm_partial", remains=st.get("remains")))
                await self.admin(f"⚠️ SMM {o['short']}: выполнен частично, не докручено "
                                 f"{st.get('remains')}. https://starvell.com/order/{o['sv_id']}")
            elif status in FAILED:
                self._set(o, **changes)
                if self.store.get("smm_auto_refund", True):
                    await self._refund(o, f"OptSMM: {status}")
                else:
                    self._set(o, state="FAILED")
                    await self.say(o["chat_id"], self.t("smm_failed"))
                    await self.admin(f"❌ SMM {o['short']}: OptSMM {status}. Нужна ручная обработка.")
            else:
                self._set(o, **changes)

    async def _reminders(self) -> None:
        now = time.time()
        remind = float(self.store.get("smm_remind_minutes", 60)) * 60
        timeout = float(self.store.get("smm_link_timeout_hours", 24)) * 3600
        for o in self.by_state("ASKED"):
            age = now - float(o.get("asked") or now)
            if age > remind and not o.get("reminded"):
                self._set(o, reminded=1)
                await self.say(o["chat_id"], self.t("smm_remind", hint=self._hint(o)))
            elif age > timeout and o.get("reminded") == 1:
                self._set(o, reminded=2)
                await self.admin(f"⏰ SMM {o['short']}: покупатель {o['buyer']} не прислал ссылку "
                                 f"{int(age // 3600)} ч.\nЗадать вручную: /smm_link {o['sv_id']} &lt;ссылка&gt;")

    # ---------- ручные действия из панели ----------

    def find(self, ref: str) -> Optional[Dict[str, Any]]:
        ref = (ref or "").strip()
        if ref in self.orders:
            return self.orders[ref]
        return next((o for o in self.orders.values()
                     if str(o.get("short", "")).lower() == ref.lower() or ref and o["sv_id"].startswith(ref)), None)

    async def manual_link(self, ref: str, link: str) -> str:
        o = self.find(ref)
        if not o:
            return "заказ не найден"
        if o["state"] in ("PLACED", "DONE", "PARTIAL", "REFUNDED"):
            return f"заказ уже {STATE_TITLE[o['state']]}"
        async with self._lock:
            self._set(o, link=link.strip())
            ok = await self.place(o)
        return "🚀 запущен" if ok else f"не запущен: {STATE_TITLE.get(o['state'], o['state'])} {o.get('note') or ''}"

    async def manual_refund(self, ref: str) -> str:
        o = self.find(ref)
        if not o:
            return "заказ не найден"
        async with self._lock:
            ok = await self._refund(o, "вручную из панели")
        return "↩️ возврат оформлен" if ok else "возврат не прошёл"

    def summary(self) -> Dict[str, Any]:
        items = list(self.orders.values())
        done = [o for o in items if o["state"] in ("DONE", "PARTIAL")]
        return {
            "active": len([o for o in items if o["state"] in ACTIVE_STATES]),
            "asked": len(self.by_state("ASKED", "QUEUED")),
            "running": len(self.by_state("PLACED")),
            "wait_balance": len(self.by_state("WAIT_BALANCE")),
            "manual": len(self.by_state("MANUAL", "FAILED")),
            "done": len(done),
            "revenue": round(sum(float(o.get("revenue") or 0) for o in done), 2),
            "cost": round(sum(float(o.get("cost") or 0) for o in done), 2),
        }
