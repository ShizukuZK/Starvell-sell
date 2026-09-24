"""Обслуживание живых лотов: привязки, остатки, поднятие."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Set

from core.kosell import KosellAPI
from core.log import get_logger
from core.starvell import StarvellAPI, StarvellError, human_error
from core.storage import Store, game_key, mapping_id
from lots.autocreate import DEFAULT_CATEGORY_ID, offer_game, offer_title, units_cap

logger = get_logger("sync")

MAX_AVAILABILITY = 999


def known_categories(store: Store) -> Set[int]:
    """Категории, где могут быть наши лоты аренды."""
    cats = {DEFAULT_CATEGORY_ID}
    for m in store.mappings:
        if m.get("category_id"):
            cats.add(int(m["category_id"]))
    return cats


class OffersCache:
    """Кэш своих лотов: экран «Лоты» и фоновые задачи не дёргают Starvell зря."""

    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = ttl
        self.items: List[Dict[str, Any]] = []
        self.ts: float = 0.0

    def fresh(self) -> bool:
        return bool(self.items or self.ts) and time.time() - self.ts < self.ttl

    async def get(self, store: Store, sv: StarvellAPI, force: bool = False) -> List[Dict[str, Any]]:
        if force or not self.fresh():
            self.items = await sv.get_all_my_offers(sorted(known_categories(store)))
            self.ts = time.time()
        return self.items

    def invalidate(self) -> None:
        self.ts = 0.0

    def patch(self, offer_id: Any, **fields: Any) -> None:
        for offer in self.items:
            if offer.get("id") == offer_id:
                offer.update(fields)

    def remove(self, offer_id: Any) -> None:
        self.items = [o for o in self.items if o.get("id") != offer_id]


def match_offer(store: Store, offer: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    mapping = store.find_mapping_by_offer(offer.get("id"), offer.get("publicId"))
    if mapping:
        return mapping
    game = offer_game(offer, store.get("game_title_patterns") or [])
    return store.find_mapping(game) if game else None


async def sync_mappings(
    store: Store, products: List[Dict[str, Any]], offers: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Связывает каждый живой лот с товаром KOSell и запоминает его id.

    Нужен, если лоты создавались вручную, на другом компьютере или старой
    версией бота: без этого бот не знает, какой лот чему соответствует.
    """
    by_key = {game_key(p.get("name", "")): p for p in products}
    patterns = store.get("game_title_patterns") or []
    linked, updated, unknown = 0, 0, []

    for offer in offers:
        game = offer_game(offer, patterns)
        if not game:
            continue
        key = game_key(game)
        mapping = store.find_mapping_by_offer(offer.get("id"), offer.get("publicId")) \
            or store.find_mapping(key)
        product = None
        if mapping and mapping.get("product_id"):
            product_id = int(mapping["product_id"])
            product = next((p for p in products if int(p["id"]) == product_id), None)
        product = product or by_key.get(key)
        if not product:
            unknown.append(game)
            continue

        record = {
            "key": game_key(product.get("name", "")) or key,
            "title_key": key,
            "game": product.get("name") or game,
            "product_id": int(product["id"]),
            "product_name": product.get("name"),
            "offer_id": offer.get("id"),
            "offer_public_id": offer.get("publicId"),
            "offer_title": offer_title(offer),
            "category_id": offer.get("categoryId"),
            "sub_category_id": offer.get("subCategoryId"),
            "game_id": offer.get("gameId"),
            "price_rub": round(float(offer.get("price") or 0), 2),
            "offer_created": True,
        }
        if mapping:
            before = (mapping.get("offer_id"), mapping.get("product_id"))
            mapping.update({k: v for k, v in record.items() if v is not None})
            mapping.setdefault("enabled", True)
            if before != (record["offer_id"], record["product_id"]):
                updated += 1
        else:
            store.mappings.append({**record, "id": mapping_id(record["key"]),
                                   "enabled": True, "auto": True})
            linked += 1

    store.save_mappings()
    return {"offers": len(offers), "linked": linked, "updated": updated, "unknown": unknown}


async def sync_stock(
    store: Store, sv: StarvellAPI, products: List[Dict[str, Any]],
    offers: List[Dict[str, Any]], *, dry_run: bool = False,
) -> Dict[str, Any]:
    """Сверяет лоты с KOSell.

    • Наличие = сколько штук (часов) можно купить за раз — максимум KOSell.
    • 0 свободных аккаунтов и включено автоскрытие -> лот снимается с продажи
      (бот запоминает, что снял его сам). Аккаунты вернулись -> лот возвращается.
    • Лоты, которые вы сняли вручную, бот не трогает.
    """
    by_id = {int(p["id"]): p for p in products}
    auto_hide = bool(store.get("auto_hide_no_stock", True))
    per_unit = max(1, int(store.get("hours_per_unit", 1)))
    changed, hidden, restored, failed = 0, 0, 0, 0
    errors: List[str] = []

    for offer in offers:
        mapping = match_offer(store, offer)
        if not mapping or not mapping.get("enabled", True):
            continue
        product = by_id.get(int(mapping.get("product_id") or 0))
        if not product:
            continue

        free = int(product.get("available_accounts") or 0)
        current_avail = int(offer.get("availability") or 0)
        active = bool(offer.get("isActive", True))
        hidden_by_bot = bool(mapping.get("hidden_by_bot"))

        changes: Dict[str, Any] = {}
        if free > 0:
            target = units_cap(product, per_unit)
            if current_avail != target:
                changes["availability"] = target
            if not active and hidden_by_bot:
                changes["isActive"] = True
        elif auto_hide and active:
            changes["isActive"] = False

        if not changes:
            continue
        if dry_run:
            logger.info("[ТЕСТ] лот %s: %s", offer.get("id"), changes)
            continue
        try:
            await sv.partial_update_offer(offer, **changes)
            offer.update(changes)
            changed += 1
            if changes.get("isActive") is False:
                mapping["hidden_by_bot"] = True
                hidden += 1
            elif changes.get("isActive") is True:
                mapping["hidden_by_bot"] = False
                restored += 1
        except StarvellError as exc:
            failed += 1
            errors.append(f"{mapping.get('game')}: {human_error(exc)}")

    if hidden or restored:
        store.save_mappings()
    return {"changed": changed, "hidden": hidden, "restored": restored,
            "failed": failed, "errors": errors}


async def bump_all(sv: StarvellAPI, offers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Поднимает лоты: по одному запросу на игру со всеми её категориями."""
    groups: Dict[int, Set[int]] = {}
    for offer in offers:
        if offer.get("isActive") and offer.get("gameId") and offer.get("categoryId"):
            groups.setdefault(int(offer["gameId"]), set()).add(int(offer["categoryId"]))
    ok, cooldown, failed = 0, 0, 0
    errors: List[str] = []
    for game_id, cats in groups.items():
        try:
            await sv.bump_offers(game_id, sorted(cats))
            ok += 1
        except StarvellError as exc:
            if exc.code == "OFFERS_BUMP_COOLDOWN" or "cooldown" in (exc.message or "").lower():
                cooldown += 1
            else:
                failed += 1
                errors.append(human_error(exc))
    return {"games": len(groups), "ok": ok, "cooldown": cooldown,
            "failed": failed, "errors": errors}
