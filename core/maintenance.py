"""Фоновое обслуживание: остатки лотов, автоподнятие, контроль баланса."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from core.engine import Engine
from core.log import get_logger
from core.starvell import StarvellAPI, StarvellAuthError
from core.storage import Store
from lots import autocreate
from lots.sync import OffersCache, bump_all, sync_mappings, sync_stock

logger = get_logger("maintenance")


class Maintenance:
    def __init__(self, store: Store, sv: StarvellAPI, engine: Engine,
                 offers: OffersCache, demand: Any = None) -> None:
        self.demand = demand
        self.last_reprice_ts: float = float(store.state.get("last_reprice_ts") or 0)
        self.last_offer_count: Optional[int] = None
        self.store = store
        self.sv = sv
        self.engine = engine
        self.offers = offers
        self._tasks: List[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.balance: Optional[Dict[str, Any]] = None
        self.balance_ts: float = 0.0
        self.last_stock: Dict[str, Any] = {}
        self.last_stock_ts: float = 0.0
        self.last_bump: Dict[str, Any] = {}
        self.last_bump_ts: float = float(store.state.get("last_bump_ts") or 0)
        self._balance_alerted = False
        self._mapped_once = False

    async def start(self) -> None:
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._loop(self.stock_tick, 60), name="stock"),
            asyncio.create_task(self._loop(self.bump_tick, 120), name="bump"),
            asyncio.create_task(self._loop(self.balance_tick, 600), name="balance"),
            asyncio.create_task(self._loop(self.demand_tick, 1800), name="demand"),
            asyncio.create_task(self._loop(self.reprice_tick, 1800), name="reprice"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self, tick, every: float) -> None:
        await asyncio.sleep(15)          # дать боту спокойно стартовать
        while not self._stop.is_set():
            try:
                await tick()
            except StarvellAuthError:
                logger.warning("обслуживание: session cookie устарел")
            except Exception as exc:
                logger.warning("обслуживание (%s): %s", tick.__name__, exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=every)
            except asyncio.TimeoutError:
                pass

    # ---------- баланс ----------

    async def get_balance(self, force: bool = False) -> Optional[Dict[str, Any]]:
        if force or not self.balance or time.time() - self.balance_ts > 60:
            data = await self.engine.ks.balance()
            if data:
                self.balance, self.balance_ts = data, time.time()
        return self.balance

    async def balance_tick(self) -> None:
        data = await self.get_balance(force=True)
        if not data:
            return
        rub = float(data.get("balance_rub") or 0)
        limit = float(self.store.get("balance_alert_rub", 50.0))
        if limit <= 0:
            return
        if rub < limit and not self._balance_alerted:
            self._balance_alerted = True
            await self.engine.admin(
                f"🪫 Баланс KOSell: {rub:.2f} ₽ — меньше порога {limit:.0f} ₽.\n"
                "Пополните, иначе новые заказы выдать не получится."
            )
        elif rub >= limit * 1.1:
            self._balance_alerted = False

    # ---------- остатки ----------

    async def stock_tick(self, force: bool = False) -> Optional[Dict[str, Any]]:
        if not force:
            if not self.store.get("stock_sync_enabled", True) or not self.store.get("enabled", True):
                return None
            minutes = max(5, int(self.store.get("stock_sync_minutes", 15)))
            if time.time() - self.last_stock_ts < minutes * 60:
                return None
        self.last_stock_ts = time.time()

        products = await self.engine.products(force=True)
        if not products:
            return None
        offers = await self.offers.get(self.store, self.sv, force=True)
        await self._watch_count(len(offers))
        if not self._mapped_once:
            await sync_mappings(self.store, products, offers)
            self._mapped_once = True

        # тестовый режим касается только выдачи заказов, а не обслуживания лотов
        result = await sync_stock(self.store, self.sv, products, offers)
        self.last_stock = result
        if result["hidden"] or result["restored"]:
            await self.engine.admin(
                "📦 Остатки обновлены: "
                f"скрыто {result['hidden']}, снова в продаже {result['restored']}, "
                f"изменено всего {result['changed']}."
            )
        return result

    # ---------- поднятие ----------

    async def bump_tick(self, force: bool = False) -> Optional[Dict[str, Any]]:
        if not force:
            if not self.store.get("auto_bump_enabled", False):
                return None
            hours = max(1.0, float(self.store.get("auto_bump_hours", 4)))
            if time.time() - self.last_bump_ts < hours * 3600:
                return None
        offers = await self.offers.get(self.store, self.sv)
        result = await bump_all(self.sv, offers)
        self.last_bump = result
        self.last_bump_ts = time.time()
        self.store.state["last_bump_ts"] = self.last_bump_ts
        self.store.save_state()
        logger.info("поднятие: %s", result)
        return result

    # ---------- пропажа лотов ----------

    async def _watch_count(self, count: int) -> None:
        """Лотов резко стало меньше без участия бота — сообщаем.

        Удаления через панель бот помнит и в расчёт не берёт.
        """
        prev = self.last_offer_count
        expected_drop = int(self.store.state.pop("deleted_by_panel", 0) or 0)
        self.last_offer_count = count
        if prev is None:
            return
        lost = prev - count - expected_drop
        if lost >= 3:
            await self.engine.admin(
                f"⚠️ Лотов стало меньше: было {prev}, сейчас {count}.\n"
                "Если удаляли сами на сайте — всё в порядке. Если нет — "
                "проверьте уведомления Starvell: лоты могла снять модерация."
            )

    # ---------- спрос ----------

    async def demand_tick(self, force: bool = False) -> Optional[Dict[str, Any]]:
        if self.demand is None or not self.store.get("demand_enabled", True):
            return None
        products = await self.engine.products()
        if not products:
            return None
        return await self.demand.refresh(products, force=force)

    async def reprice_tick(self, force: bool = False) -> Optional[Dict[str, Any]]:
        """Автопересчёт цен по спросу (по умолчанию выключен)."""
        if not force:
            if not self.store.get("auto_reprice_enabled", False):
                return None
            hours = max(1.0, float(self.store.get("auto_reprice_hours", 24)))
            if time.time() - self.last_reprice_ts < hours * 3600:
                return None
        offers = await self.offers.get(self.store, self.sv, force=True)
        changes = await autocreate.reprice_plan(self.store, self.engine.ks, offers, self.demand)
        self.last_reprice_ts = time.time()
        self.store.state["last_reprice_ts"] = self.last_reprice_ts
        self.store.save_state()
        if not changes:
            return {"updated": 0, "failed": 0, "errors": []}
        res = await autocreate.apply_prices(self.sv, changes)
        self.offers.invalidate()
        if res["updated"]:
            up = sum(1 for c in changes if c["new"] > c["old"])
            await self.engine.admin(
                f"💸 Цены пересчитаны по спросу: изменено {res['updated']} "
                f"(дороже {up}, дешевле {len(changes) - up})."
            )
        return res
