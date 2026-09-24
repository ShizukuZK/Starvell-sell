"""Движок автовыдачи: заказ на Starvell -> аренда в KOSell -> выдача в чат.

Модель лота на Starvell: количество штук в заказе = часы аренды
(настройка `hours_per_unit`). ID лота площадка продавцу не отдаёт
(`offerId` всегда null), поэтому товар KOSell определяется по названию
игры в заголовке лота.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.kosell import KosellAPI, human_error
from core.log import get_logger
from core.starvell import StarvellAPI, StarvellError
from core.storage import Store, game_key
from core.texts import fmt_expires, parse_ts, render

logger = get_logger("engine")

_NOISE = re.compile(
    r"(⚡|🎮|🛒|✅|⭐|🚀|⏱️|24/7|АВТОВЫДАЧА|АРЕНДА|ОТ\s*\d+\s*ЧАС\w*|"
    r"УКАЖИТЕ[^|]*|\(.*?\)|\[|\])", re.I
)


def _cost(data: Optional[Dict[str, Any]]) -> float:
    """Сколько списал KOSell (в рублях, если есть)."""
    if not data:
        return 0.0
    for key in ("price_rub", "price_paid"):
        try:
            value = float(data.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value:
            return value
    return 0.0


_COMMANDS = {
    "код": "code", "code": "code", "guard": "code", "гуард": "code",
    "наличие": "stock", "налич": "stock", "наличия": "stock", "свободен": "stock",
    "свободно": "stock", "stock": "stock",
    "прод": "extend", "продлить": "extend", "продление": "extend", "extend": "extend",
    "друг": "friend", "friend": "friend",
    "мои": "my", "my": "my", "аренды": "my",
    "помощь": "help", "help": "help", "команды": "help", "start": "help",
}
_STOCK_QUESTION = re.compile(r"налич|свобод|занят", re.I)
_FILLER = {"", "есть", "ли", "сейчас", "щас", "пж", "плз", "пожалуйста", "а", "у", "вас"}


def parse_command(text: str) -> Optional[Tuple[str, str]]:
    """«!код логин» -> ("code", "логин"). Понимает и без «!»: «наличие», «код».

    Без «!» командой считается только короткое сообщение — чтобы обычная
    фраза, где встретилось слово «код», не вызывала команду.
    """
    raw = (text or "").strip()
    bang = raw[:1] in ("!", "/")
    body = raw.lstrip("!/").strip()
    if not body:
        return None
    word, _, arg = body.partition(" ")
    cmd = _COMMANDS.get(word.lower().strip(".,?!:;"))
    words = len(body.split())
    if cmd and (bang or words <= 3):
        arg = arg.strip()
        if cmd == "stock" and all(w.strip(".,?!") in _FILLER for w in arg.lower().split()):
            arg = ""                       # «наличие есть?» — это вопрос, а не название игры
        return cmd, arg
    if not bang and words <= 6 and _STOCK_QUESTION.search(body):
        return "stock", ""                  # «есть в наличии?», «свободен сейчас?»
    return None


def extract_game(title: str, patterns: List[str]) -> Optional[str]:
    """Достаёт название игры из заголовка лота."""
    text = (title or "").strip()
    if not text:
        return None
    for raw in patterns:
        try:
            m = re.search(raw, text, re.I)
        except re.error:
            logger.warning("некорректный шаблон названия: %s", raw)
            continue
        if m:
            try:
                value = m.group("game")
            except IndexError:
                value = m.group(1) if m.groups() else None
            if value:
                value = _NOISE.sub(" ", value)
                value = " ".join(value.split()).strip(" -–—|•:")
                if len(value) >= 2:
                    return value
    return None


class Engine:
    def __init__(
        self,
        store: Store,
        starvell: StarvellAPI,
        kosell: KosellAPI,
        notify_admin: Optional[Callable[[str], Any]] = None,
        stats: Any = None,
    ) -> None:
        self.store = store
        self.sv = starvell
        self.ks = kosell
        self.stats = stats
        self._notify_admin = notify_admin
        self._order_locks: Dict[str, asyncio.Lock] = {}
        self._products_cache: List[Dict[str, Any]] = []
        self._products_ts: float = 0.0

    # ---------- утилиты ----------

    async def admin(self, text: str) -> None:
        logger.info("ADMIN: %s", text.replace("\n", " ")[:300])
        if self._notify_admin:
            try:
                result = self._notify_admin(text)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning("не удалось уведомить админа: %s", exc)

    def t(self, key: str, **kwargs: Any) -> str:
        return render(self.store.texts.get(key, ""), **kwargs)

    def _tz(self) -> int:
        return int(self.store.get("tz_offset_hours", 3))

    def dry(self) -> bool:
        return bool(self.store.get("dry_run", False))

    async def say(self, chat_id: str, text: str, order: bool = False) -> bool:
        """Сообщение покупателю.

        order=True — сообщение про заказ (выдача, продление, возврат). В
        тестовом режиме такие только пишутся в лог. Справочные ответы
        (приветствие, !наличие, !помощь) уходят всегда — они ничего не стоят.
        """
        if not chat_id or not text:
            return False
        if order and self.dry():
            logger.info("[ТЕСТ] в чат %s: %s", chat_id, text.replace("\n", " ")[:160])
            return True
        try:
            await self.sv.send_message(chat_id, text)
            return True
        except StarvellError as exc:
            logger.warning("не отправлено в чат %s: %s", chat_id, exc)
            return False

    async def products(self, force: bool = False, max_age: float = 600) -> List[Dict[str, Any]]:
        if force or not self._products_cache or time.time() - self._products_ts > max_age:
            data = await self.ks.products(currency="RUB")
            if data is not None:
                self._products_cache = data
                self._products_ts = time.time()
        return self._products_cache

    # ---------- сопоставление игры с товаром KOSell ----------

    async def resolve_product(self, game: str) -> Optional[Dict[str, Any]]:
        """Ищет товар KOSell по названию игры и запоминает привязку."""
        key = game_key(game)
        if not key:
            return None

        mapping = self.store.find_mapping(key)
        if mapping and mapping.get("enabled", True) and mapping.get("product_id"):
            return {
                "id": int(mapping["product_id"]),
                "name": mapping.get("product_name") or game,
                "_mapping": mapping,
            }
        if mapping and not mapping.get("enabled", True):
            return None
        if not self.store.get("auto_map_by_name", True):
            return None

        for product in await self.products():
            if game_key(product.get("name", "")) == key:
                self.store.upsert_mapping({
                    "key": key,
                    "game": game,
                    "product_id": int(product["id"]),
                    "product_name": product.get("name"),
                    "enabled": True,
                    "auto": True,
                })
                logger.info("автопривязка: «%s» -> KOSell #%s", game, product["id"])
                return product

        found = await self.ks.products(search=game, currency="RUB") or []
        for product in found:
            if game_key(product.get("name", "")) == key:
                self.store.upsert_mapping({
                    "key": key,
                    "game": game,
                    "product_id": int(product["id"]),
                    "product_name": product.get("name"),
                    "enabled": True,
                    "auto": True,
                })
                logger.info("автопривязка (поиск): «%s» -> KOSell #%s", game, product["id"])
                return product
        return None

    # ---------- обработка заказа ----------

    def _lock_for(self, order_id: str) -> asyncio.Lock:
        return self._order_locks.setdefault(str(order_id), asyncio.Lock())

    async def handle_order(self, order: Dict[str, Any]) -> None:
        order_id = str(order.get("id") or "")
        if not order_id or self.store.is_handled(order_id):
            return
        async with self._lock_for(order_id):
            if self.store.is_handled(order_id):
                return
            try:
                await self._handle_order_inner(order)
            except Exception as exc:
                logger.exception("ошибка обработки заказа %s", order_id)
                await self.admin(f"⚠️ Ошибка обработки заказа {order_id}: {exc}")

    async def _handle_order_inner(self, order: Dict[str, Any]) -> None:
        order_id = str(order["id"])
        short_id = order.get("shortId") or order_id[:8]
        status = str(order.get("status") or "")
        if status and status != "CREATED":
            return

        offer_details = order.get("offerDetails") or {}
        sub = (offer_details.get("subCategory") or {}).get("name") or ""
        descr = (offer_details.get("descriptions") or {}).get("rus", {}) or {}
        title = descr.get("briefDescription") or ""
        full_text = f"{title}\n{descr.get('description') or ''}"

        game = extract_game(title, self.store.get("game_title_patterns") or []) \
            or extract_game(full_text, self.store.get("game_title_patterns") or [])
        if not game:
            logger.debug("заказ %s: игра не распознана (%s)", short_id, title[:60])
            return

        product = await self.resolve_product(game)
        if not product:
            logger.info("заказ %s: нет товара KOSell для «%s»", short_id, game)
            return

        quantity = int(order.get("quantity") or 1)
        revenue = float(order.get("basePrice") or order.get("totalPrice") or 0) / 100
        per_unit = max(1, int(self.store.get("hours_per_unit", 1)))
        min_quantity = int(self.store.get("min_quantity", 3))
        hours = quantity * per_unit

        # детали заказа нужны ради chat_id и покупателя
        details = await self.sv.get_order(order_id)
        info = details.get("order") or {}
        chat_id = (details.get("chat") or {}).get("id") or ""
        buyer_id = info.get("buyerId") or order.get("buyerId")
        buyer_name = ((info.get("buyer") or {}).get("username")
                      or (order.get("user") or {}).get("username") or "покупатель")
        buyer_key = str(buyer_id)

        self.store.mark_handled(order_id)

        if self.store.get("notify_sales", True):
            await self.admin(
                f"💰 Новая продажа {short_id}\n"
                f"Игра: {game} ({sub or '—'})\n"
                f"Покупатель: {buyer_name}\n"
                f"{quantity} шт = {hours} ч · {float(order.get('totalPrice') or 0) / 100:.2f} ₽\n"
                f"https://starvell.com/order/{order_id}"
            )

        if quantity < min_quantity:
            await self.say(chat_id, order=True, text=self.t("below_min", minimum=min_quantity, ordered=quantity,
            ))
            if self.store.get("auto_refund_below_min", True):
                await self._refund(order_id, f"заказ {quantity} шт < минимума {min_quantity}",
                                   game=game, revenue=revenue)
            return

        currency = self.store.get("currency", "RUB")
        product_id = int(product["id"])
        product_name = product.get("name") or game

        # повторная оплата той же игры -> продление, а не новый аккаунт
        existing = [
            r for r in self.store.active_rentals(buyer_key)
            if int(r.get("product_id", -1)) == product_id
        ]
        if existing and not self.store.friend_active(buyer_key):
            if len(existing) == 1:
                await self._extend(existing[0], chat_id, hours, currency, order_id,
                                   revenue=revenue, quantity=quantity, buyer=buyer_name)
            else:
                logins = "\n".join(f"• {r['login']}" for r in existing)
                self._pending_extend(buyer_key, hours, currency, order_id)
                await self.say(chat_id, order=True, text=self.t("ask_which_account", game=product_name, logins=logins,
                ))
            return

        if self.store.friend_active(buyer_key):
            self.store.friend_clear(buyer_key)

        ok, err = await self._rent_and_deliver(
            product_id=product_id, product_name=product_name, hours=hours,
            currency=currency, buyer_key=buyer_key, chat_id=chat_id,
            order_id=order_id, idempotency_key=order_id,
            revenue=revenue, quantity=quantity, buyer=buyer_name,
        )
        if ok:
            if self.store.get("auto_confirm_order", False) and not self.dry():
                try:
                    await self.sv.confirm_order(order_id)
                except StarvellError as exc:
                    logger.warning("не удалось подтвердить заказ %s: %s", short_id, exc)
            return

        await self.say(chat_id, order=True, text=self.t("problem"))
        await self.admin(
            f"❌ Заказ {short_id} ({product_name}, {hours} ч) не выдан: "
            f"{human_error(err or 'unknown')}\nhttps://starvell.com/order/{order_id}"
        )
        if self.store.get("auto_refund", True):
            await self._refund(order_id, human_error(err or "unknown"),
                               game=product_name, revenue=revenue)

    async def _rent_and_deliver(
        self, *, product_id: int, product_name: str, hours: int, currency: str,
        buyer_key: str, chat_id: str, order_id: str, idempotency_key: str,
        revenue: float = 0.0, quantity: int = 1, buyer: str = "",
    ) -> Tuple[bool, Optional[str]]:
        if self.dry():
            logger.info(
                "[ТЕСТ] аренда: товар #%s (%s) на %d ч, заказ %s — запрос не отправлен",
                product_id, product_name, hours, order_id,
            )
            await self.say(chat_id, order=True, text=self.t("delivery", game=product_name, login="TEST_LOGIN",
                password="TEST_PASSWORD", hours=hours,
                expires=fmt_expires(time.time() + hours * 3600, self._tz()),
            ))
            return True, None

        data, err = await self.ks.rent(product_id, hours, currency, idempotency_key)
        if not data:
            logger.warning("аренда не удалась (%s): %s", product_name, err)
            return False, err

        rental_uid = data.get("rental_uid")
        login = data.get("steam_login") or ""
        password = data.get("steam_password") or ""
        if not password and rental_uid:
            creds = await self.ks.credentials(rental_uid)
            if creds:
                login = creds.get("steam_login") or login
                password = creds.get("steam_password") or ""
        if not password:
            await self.admin(
                f"❌ Аренда {rental_uid} оформлена, но пароль не получен (заказ {order_id})."
            )
            return False, "no_credentials"

        expires_ts = parse_ts(data.get("expires_at")) or (time.time() + hours * 3600)
        self.store.add_rental(buyer_key, {
            "rental_uid": rental_uid,
            "product_id": product_id,
            "product_name": data.get("product_name") or product_name,
            "login": login,
            "hours": hours,
            "currency": currency,
            "started_ts": time.time(),
            "expires_ts": expires_ts,
            "order_id": order_id,
            "chat_id": chat_id,
            "notified_end": False,
            "notified_soon": False,
        })

        await self.say(chat_id, order=True, text=self.t("delivery", game=data.get("product_name") or product_name,
            login=login, password=password, hours=hours,
            expires=fmt_expires(expires_ts, self._tz()),
        ))
        logger.info("выдан %s (%s, %d ч) по заказу %s", login, product_name, hours, order_id)
        self._record(order_id, product_name, quantity, hours, revenue,
                     _cost(data), "rent", buyer)
        return True, None

    async def _extend(
        self, rental: Dict[str, Any], chat_id: str,
        hours: int, currency: str, order_id: str,
        revenue: float = 0.0, quantity: int = 0, buyer: str = "",
    ) -> None:
        if self.dry():
            logger.info(
                "[ТЕСТ] продление %s на %d ч (заказ %s) — запрос не отправлен",
                rental.get("login"), hours, order_id,
            )
            return

        data, err = await self.ks.extend(rental["rental_uid"], hours, currency)
        if not data:
            await self.say(chat_id, order=True, text=self.t("problem"))
            await self.admin(
                f"❌ Продление {rental.get('login')} (заказ {order_id}) не удалось: "
                f"{human_error(err or 'unknown')}"
            )
            if self.store.get("auto_refund", True):
                await self._refund(order_id, human_error(err or "unknown"))
            return

        new_expires = parse_ts(data.get("new_expires_at")) or (
            rental.get("expires_ts", time.time()) + hours * 3600
        )
        rental["expires_ts"] = new_expires
        rental["notified_end"] = False
        rental["notified_soon"] = False
        self.store.save_rentals()

        await self.say(chat_id, order=True, text=self.t("extension", login=rental.get("login"), game=rental.get("product_name"),
            hours=hours, expires=fmt_expires(new_expires, self._tz()),
        ))
        logger.info("продлена аренда %s на %d ч. (заказ %s)", rental.get("login"), hours, order_id)
        self._record(order_id, rental.get("product_name") or "", quantity or hours, hours,
                     revenue, _cost(data), "extend", buyer)

    def _record(self, order_id: str, game: str, quantity: int, hours: int,
                revenue: float, cost: float, kind: str, buyer: str = "") -> None:
        if self.stats is None or self.dry():
            return
        try:
            self.stats.record(order_id=order_id, game=game, quantity=quantity,
                              hours=hours, revenue_rub=revenue, cost_rub=cost,
                              kind=kind, buyer=buyer)
        except Exception as exc:
            logger.debug("статистика: %s", exc)

    async def _refund(self, order_id: str, reason: str = "", *,
                      game: str = "", revenue: float = 0.0) -> None:
        if self.dry():
            logger.info("[ТЕСТ] возврат по заказу %s (%s) — запрос не отправлен",
                        order_id, reason or "—")
            return
        try:
            await self.sv.refund_order(order_id)
            await self.admin(f"↩️ Возврат по заказу {order_id}. Причина: {reason or '—'}")
            self._record(order_id, game, 0, 0, revenue, 0.0, "refund")
        except StarvellError as exc:
            await self.admin(f"⚠️ Возврат по заказу {order_id} не прошёл: {exc}")

    # ---------- отложенные продления ----------

    def _pending_extend(self, buyer_key: str, hours: int, currency: str, order_id: str) -> None:
        self.store.state.setdefault("pending_extend", {})[str(buyer_key)] = {
            "hours": hours, "currency": currency, "order_id": order_id, "ts": time.time(),
        }
        self.store.save_state()

    def _take_pending_extend(self, buyer_key: str) -> Optional[Dict[str, Any]]:
        pending = self.store.state.get("pending_extend", {}).pop(str(buyer_key), None)
        if pending:
            self.store.save_state()
        return pending

    # ---------- команды покупателя ----------

    async def handle_message(
        self, chat_id: str, author_id: Any, content: str, *,
        offer: Optional[Dict[str, Any]] = None, new_chat: bool = False,
    ) -> None:
        """Сообщение покупателя: команда, вопрос о наличии или приветствие.

        offer — лот, со страницы которого написано сообщение (если есть).
        """
        text = (content or "").strip()
        if not text:
            return
        buyer_key = str(author_id)
        parsed = parse_command(text)
        try:
            if parsed is None:
                await self._maybe_greet(chat_id, buyer_key, offer, new_chat)
                return
            cmd, arg = parsed
            if cmd == "code":
                await self._cmd_code(chat_id, buyer_key, arg)
            elif cmd == "stock":
                await self._cmd_stock(chat_id, arg, buyer_key=buyer_key, offer=offer)
            elif cmd == "extend":
                await self._cmd_extend(chat_id, buyer_key, arg)
            elif cmd == "friend":
                await self._cmd_friend(chat_id, buyer_key)
            elif cmd == "my":
                await self._cmd_my(chat_id, buyer_key)
            elif cmd == "help":
                await self.say(chat_id, self.t("help"))
            self._mark_greeted(buyer_key)     # после команды приветствие уже не нужно
        except Exception:
            logger.exception("ошибка обработки сообщения от %s", buyer_key)

    # ---------- приветствие

    def _mark_greeted(self, buyer_key: str) -> None:
        self.store.state.setdefault("greeted", {})[buyer_key] = time.time()
        self.store.save_state()

    async def _maybe_greet(self, chat_id: str, buyer_key: str,
                           offer: Optional[Dict[str, Any]], new_chat: bool) -> None:
        if not self.store.get("greeting_enabled", True):
            return
        greeted = float(self.store.state.get("greeted", {}).get(buyer_key) or 0)
        cooldown = float(self.store.get("greeting_cooldown_hours", 24)) * 3600
        if time.time() - greeted < cooldown:
            return
        if self.store.active_rentals(buyer_key):
            return             # у покупателя идёт аренда — ему не нужна инструкция
        game = await self._context_game(buyer_key, offer)
        stock = await self._stock_line(game) if game else ""
        per_unit = max(1, int(self.store.get("hours_per_unit", 1)))
        min_q = int(self.store.get("min_quantity", 1))
        await self.say(chat_id, self.t(
            "greeting",
            unit="1 час" if per_unit == 1 else f"{per_unit} ч.",
            min_line=(f"\n• Минимальный заказ — {min_q} шт." if min_q > 1 else ""),
            stock_line=(f"\n\n{stock}" if stock else ""),
        ))
        self._mark_greeted(buyer_key)

    # ---------- контекст: какую игру смотрит покупатель

    async def _context_game(self, buyer_key: str, offer: Optional[Dict[str, Any]]) -> Optional[str]:
        """Игра из лота, с которого пришло сообщение, или из лота, открытого сейчас."""
        patterns = self.store.get("game_title_patterns") or []
        for source in (offer, None):
            if source is None:
                try:
                    source = await self.sv.viewed_offer(buyer_key)
                except Exception:
                    source = None
            if not source:
                continue
            brief = (source.get("briefDescription")
                     or ((source.get("descriptions") or {}).get("rus") or {}).get("briefDescription")
                     or "")
            game = extract_game(brief, patterns)
            if game:
                return game
        return None

    async def _stock_line(self, game: str, fresh_data: bool = False) -> str:
        """Одна строка о наличии.

        fresh_data=True — прямой вопрос покупателя: только свежие данные, иначе
        сразу после чужой аренды бот ответил бы «свободен». Для приветствия
        хватает кэша полминутной давности.
        """
        product = await self.resolve_product(game)
        if not product:
            return ""
        catalog = await (self.products(force=True) if fresh_data else self.products(max_age=30))
        fresh = next((p for p in catalog if int(p["id"]) == int(product["id"])), None)
        if not fresh:
            return ""
        free = int(fresh.get("available_accounts") or 0)
        name = fresh.get("name") or game
        if free > 0:
            return self.t("stock_free", game=name, count=free)
        next_ts = parse_ts(fresh.get("next_available"))
        if next_ts:
            return self.t("stock_busy", game=name, minutes=max(1, int((next_ts - time.time()) // 60)))
        return self.t("stock_busy_unknown", game=name)

    async def _cmd_stock(self, chat_id: str, arg: str, *, buyer_key: str = "",
                         offer: Optional[Dict[str, Any]] = None) -> None:
        """!наличие [игра]. Без названия — по лоту, который смотрит покупатель."""
        game = arg.strip() or await self._context_game(buyer_key, offer)
        if not game:
            await self.say(chat_id, self.t("stock_ask_game"))
            return
        line = await self._stock_line(game, fresh_data=True)
        await self.say(chat_id, line or self.t("stock_unknown", game=game))

    async def _cmd_code(self, chat_id: str, buyer_key: str, arg: str) -> None:
        cooldown = int(self.store.get("code_cooldown_seconds", 20))
        last = float(self.store.state.get("last_code_request", {}).get(buyer_key, 0))
        left = int(last + cooldown - time.time())
        if left > 0:
            await self.say(chat_id, self.t("code_cooldown", seconds=left))
            return

        rentals = self.store.active_rentals(buyer_key)
        if not rentals:
            await self.say(chat_id, self.t("code_not_found"))
            return

        target = self._pick_rental(rentals, arg)
        if target is None:
            logins = "\n".join(f"• {r['login']}" for r in rentals)
            await self.say(chat_id, self.t("code_ask_login", logins=logins))
            return

        self.store.state.setdefault("last_code_request", {})[buyer_key] = time.time()
        self.store.save_state()

        data = await self.ks.guard_code(target["rental_uid"])
        if not data or not data.get("code"):
            await self.say(chat_id, self.t("code_not_found"))
            await self.admin(
                f"⚠️ Guard-код для {target.get('login')} не получен: "
                f"{(data or {}).get('error', 'нет ответа')}"
            )
            return

        await self.say(chat_id, self.t(
            "code", login=target["login"], code=data["code"],
            ttl=data.get("expires_in") or 30,
        ))

    async def _cmd_extend(self, chat_id: str, buyer_key: str, arg: str) -> None:
        pending = self.store.state.get("pending_extend", {}).get(buyer_key)
        if not pending:
            await self.say(chat_id, self.t("extend_no_pending"))
            return

        rentals = self.store.active_rentals(buyer_key)
        target = self._pick_rental(rentals, arg)
        if target is None:
            logins = "\n".join(f"• {r['login']}" for r in rentals)
            await self.say(chat_id, order=True, text=self.t("ask_which_account",
                game=rentals[0].get("product_name") if rentals else "",
                logins=logins,
            ))
            return

        self._take_pending_extend(buyer_key)
        await self._extend(
            target, chat_id, int(pending["hours"]),
            pending.get("currency", "RUB"), pending["order_id"],
        )

    async def _cmd_friend(self, chat_id: str, buyer_key: str) -> None:
        minutes = int(self.store.get("friend_minutes", 10))
        if self.store.friend_active(buyer_key):
            await self.say(chat_id, self.t("friend_already"))
            return
        self.store.friend_set(buyer_key, minutes)
        await self.say(chat_id, self.t("friend_activated", minutes=minutes))

    async def _cmd_my(self, chat_id: str, buyer_key: str) -> None:
        rentals = self.store.active_rentals(buyer_key)
        if not rentals:
            await self.say(chat_id, self.t("my_rentals_empty"))
            return
        items = "\n".join(
            f"• {r['login']} ({r.get('product_name')}) — до "
            f"{fmt_expires(r.get('expires_ts'), self._tz())}"
            for r in rentals
        )
        await self.say(chat_id, self.t("my_rentals", items=items))

    @staticmethod
    def _pick_rental(rentals: List[Dict[str, Any]], arg: str) -> Optional[Dict[str, Any]]:
        if not rentals:
            return None
        if arg:
            needle = arg.strip().lower()
            for r in rentals:
                if str(r.get("login", "")).lower() == needle:
                    return r
            for r in rentals:
                if needle in str(r.get("login", "")).lower():
                    return r
            return None
        return rentals[0] if len(rentals) == 1 else None

    # ---------- контроль сроков ----------

    async def check_rentals(self) -> None:
        if not self.store.get("notify_rental_end", True):
            self.store.cleanup_rentals()
            return

        now = time.time()
        changed = False
        for items in list(self.store.rentals.values()):
            for r in items:
                expires = float(r.get("expires_ts") or 0)
                chat_id = r.get("chat_id")
                if not chat_id:
                    continue
                left = expires - now
                if 0 < left <= 600 and not r.get("notified_soon"):
                    r["notified_soon"] = True
                    changed = True
                    await self.say(chat_id, order=True, text=self.t("rental_soon_end", login=r.get("login"),
                        game=r.get("product_name"), minutes=max(1, int(left // 60)),
                    ))
                elif left <= 0 and not r.get("notified_end"):
                    r["notified_end"] = True
                    changed = True
                    await self.say(chat_id, order=True, text=self.t("rental_ended", game=r.get("product_name"), login=r.get("login"),
                    ))
        if changed:
            self.store.save_rentals()
        self.store.cleanup_rentals()
