"""Циклы опроса Starvell: новые заказы, новые сообщения, сроки аренды, лоты."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from core.engine import Engine
from core.log import get_logger
from core.starvell import StarvellAPI, StarvellAuthError, StarvellError
from core.storage import Store

logger = get_logger("poller")


class Poller:
    def __init__(self, store: Store, starvell: StarvellAPI, engine: Engine, smm: Any = None) -> None:
        self.store = store
        self.sv = starvell
        self.engine = engine
        self.smm = smm            # SmmEngine: SMM-лоты (OptSMM) обрабатываются им
        self._tasks: List[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._primed_orders = False
        self._primed_chats = False
        # заказы без привязки: не дёргаем их детали каждый цикл
        self._deferred: Dict[str, float] = {}
        self._defer_seconds = 600.0
        self.last_error: Optional[str] = None
        self.started_at = time.time()
        self.stats = {"orders": 0, "messages": 0, "errors": 0}

    # ---------- запуск ----------

    async def start(self) -> None:
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._loop_orders(), name="orders"),
            asyncio.create_task(self._loop_chats(), name="chats"),
            asyncio.create_task(self._loop_rentals(), name="rentals"),
            asyncio.create_task(self._loop_keepalive(), name="keepalive"),
        ]
        if self.smm is not None:
            self._tasks.append(asyncio.create_task(self._loop_smm(), name="smm"))
        logger.info("поллер запущен")

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("поллер остановлен")

    async def _sleep(self, seconds: float) -> bool:
        """Сон с возможностью прерывания. False — пора выходить."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True

    def _enabled(self) -> bool:
        return bool(self.store.get("enabled", True))

    # ---------- заказы ----------

    async def _loop_orders(self) -> None:
        while not self._stop.is_set():
            interval = float(self.store.get("orders_poll_interval", 20))
            try:
                if self._enabled():
                    await self._poll_orders()
            except StarvellAuthError as exc:
                self.last_error = str(exc)
                self.stats["errors"] += 1
                logger.error("авторизация Starvell: %s", exc)
                await self.engine.admin(
                    "🔴 Session cookie Starvell недействителен — бот не видит заказы. "
                    "Обновите его в панели."
                )
                interval = max(interval, 120)
            except Exception as exc:
                self.last_error = str(exc)
                self.stats["errors"] += 1
                logger.warning("опрос заказов: %s", exc)
            if not await self._sleep(interval):
                return

    async def _poll_orders(self) -> None:
        orders = await self.sv.get_orders(status="CREATED")

        if not self._primed_orders:
            # первый запуск: помечаем всё существующее как обработанное,
            # чтобы не выдавать аккаунты по старым заказам
            for order in orders:
                self.store.mark_handled(str(order.get("id")))
            self._primed_orders = True
            logger.info("стартовая синхронизация: %d активных заказов помечены", len(orders))
            return

        now = time.time()
        for order in orders:
            order_id = str(order.get("id") or "")
            if not order_id or self.store.is_handled(order_id):
                continue
            if now - self._deferred.get(order_id, 0.0) < self._defer_seconds:
                continue
            self.stats["orders"] += 1
            if self.smm is not None and await self.smm.handle_order(order):
                continue          # SMM-лот — обработан модулем OptSMM
            await self.engine.handle_order(order)
            if not self.store.is_handled(order_id):
                # заказ не наш (лот не привязан) — вернёмся к нему позже
                self._deferred[order_id] = now

        if len(self._deferred) > 500:
            self._deferred = {
                k: v for k, v in self._deferred.items()
                if now - v < self._defer_seconds
            }

    # ---------- сообщения ----------

    async def _loop_chats(self) -> None:
        while not self._stop.is_set():
            interval = float(self.store.get("chats_poll_interval", 8))
            try:
                if self._enabled():
                    await self._poll_chats()
            except StarvellAuthError as exc:
                self.last_error = str(exc)
                logger.error("авторизация Starvell (чаты): %s", exc)
                interval = max(interval, 120)
            except Exception as exc:
                self.last_error = str(exc)
                self.stats["errors"] += 1
                logger.debug("опрос чатов: %s", exc)
            if not await self._sleep(interval):
                return

    async def _poll_chats(self) -> None:
        """Новые сообщения покупателей.

        Список чатов даёт только последнее сообщение, поэтому для изменившихся
        чатов бот дочитывает историю (messages/list-v2) — иначе два сообщения,
        пришедшие между опросами, превращались в одно, а первое сообщение
        нового чата вообще терялось.
        """
        chats = await self.sv.get_chats()
        my_id = str(self.sv.my_user_id or "")
        seen = self.store.state.setdefault("last_message_ids", {})
        dirty = False

        for chat in chats:
            chat_id = chat.get("id")
            if not chat_id:
                continue
            last = _last_message(chat)
            last_id = str(last.get("id") or "")
            if not last_id or seen.get(chat_id) == last_id:
                continue

            known = seen.get(chat_id)
            seen[chat_id] = last_id
            dirty = True
            if not self._primed_chats:
                continue       # переписки, которые были до запуска, не трогаем

            new_chat = known is None
            for msg in await self._fresh_messages(chat_id, known, last):
                author_id = str(msg.get("authorId") or (msg.get("author") or {}).get("id") or "")
                if not author_id or author_id == my_id:
                    continue
                if str(msg.get("type") or "DEFAULT").upper() != "DEFAULT":
                    continue   # системные: заказ, отзыв, возврат…
                content = (msg.get("content") or msg.get("text") or "").strip()
                if not content:
                    continue
                self.stats["messages"] += 1
                if self.smm is not None and await self.smm.handle_message(
                        chat_id, author_id, content, offer=msg.get("offer")):
                    new_chat = False
                    continue      # ссылка/вопрос по SMM-заказу
                await self.engine.handle_message(
                    chat_id, author_id, content,
                    offer=msg.get("offer"), new_chat=new_chat,
                )
                new_chat = False

        if dirty:
            self.store.save_state()
        if not self._primed_chats:
            self._primed_chats = True
            logger.info("стартовая синхронизация чатов: %d", len(chats))

    async def _fresh_messages(self, chat_id: str, known: Optional[str],
                              last: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            history = await self.sv.list_messages(chat_id, limit=30)
        except Exception as exc:
            logger.debug("история чата %s: %s", chat_id, exc)
            history = []
        if not history:
            return [last]
        if known is None:
            return history                      # новый чат — всё, что в нём есть
        ids = [str(m.get("id")) for m in history]
        if known in ids:
            return history[ids.index(known) + 1:]
        return history[-3:]                     # очень много сообщений сразу — берём свежие

    # ---------- сроки аренды ----------

    async def _loop_rentals(self) -> None:
        while not self._stop.is_set():
            try:
                await self.engine.check_rentals()
            except Exception as exc:
                logger.debug("контроль аренд: %s", exc)
            if not await self._sleep(float(self.store.get("rentals_poll_interval", 60))):
                return

    # ---------- SMM: баланс, статусы OptSMM, напоминания ----------

    async def _loop_smm(self) -> None:
        while not self._stop.is_set():
            try:
                if self._enabled():
                    await self.smm.tick()
            except Exception as exc:
                logger.warning("SMM: %s", exc)
            if not await self._sleep(float(self.store.get("smm_poll_seconds", 45))):
                return

    # ---------- поддержание сессии ----------

    async def _loop_keepalive(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sv.keep_alive()
            except Exception as exc:
                logger.debug("keep-alive: %s", exc)
            if not await self._sleep(300):
                return

    # ---------- статус ----------

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled(),
            "uptime_seconds": int(time.time() - self.started_at),
            "orders_seen": self.stats["orders"],
            "messages_seen": self.stats["messages"],
            "errors": self.stats["errors"],
            "last_error": self.last_error,
            "active_rentals": sum(
                len(self.store.active_rentals(k)) for k in self.store.rentals
            ),
            "mappings": len(self.store.mappings),
        }


def _last_message(chat: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("lastMessage", "last_message", "latestMessage"):
        value = chat.get(key)
        if isinstance(value, dict):
            return value
    msgs = chat.get("messages")
    if isinstance(msgs, list) and msgs:
        return msgs[-1] if isinstance(msgs[-1], dict) else {}
    return {}
