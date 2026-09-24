"""Автосоздание и переоценка лотов аренды на Starvell по товарам KOSell.

Модель лота: 1 штука = `hours_per_unit` часов, покупатель сам выбирает
количество, поэтому в лоте хранится цена за одну штуку.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from core.engine import extract_game
from core.kosell import KosellAPI
from core.log import get_logger
from core.starvell import StarvellAPI, StarvellError, human_error
from core.storage import Store, game_key
from core.textfit import build_description, build_title
from lots.catalog import load_catalog, rental_targets

logger = get_logger("autocreate")

FALLBACK_GAME_SLUG = "steam"   # сюда идут игры без отдельного раздела на Starvell
DEFAULT_CATEGORY_ID = 213      # Steam → Аккаунты с играми

Progress = Optional[Callable[..., Awaitable[None] | None]]


# ---------------------------------------------------------------- утилиты

def norm_name(name: str) -> str:
    return game_key(name)


def public_price(
    cost_rub: float, *, markup_percent: float, commission_percent: float,
    min_price: float, round_to: float,
) -> float:
    price = cost_rub * (1.0 + markup_percent / 100.0)
    commission = max(0.0, min(90.0, commission_percent))
    if commission:
        price = price / (1.0 - commission / 100.0)
    price = max(price, min_price)
    if round_to and round_to > 0:
        price = math.ceil(round(price / round_to, 6)) * round_to
    return round(price, 2)


def tier_floor(store: Store, tier: Optional[str]) -> float:
    """Минимальная цена за штуку для уровня спроса."""
    base = float(store.get("lots_min_price_rub", 3.0))
    if tier == "hot":
        return max(base, float(store.get("price_floor_hot", base)))
    if tier == "popular":
        return max(base, float(store.get("price_floor_popular", base)))
    return base


def price_for(store: Store, cost_rub: float, tier: Optional[str] = None) -> float:
    """Цена за штуку: наценка к себестоимости, но не ниже порога уровня спроса."""
    return public_price(
        cost_rub,
        markup_percent=float(store.get("lots_markup_percent", 60.0)),
        commission_percent=float(store.get("lots_commission_percent", 0.0)),
        min_price=tier_floor(store, tier),
        round_to=float(store.get("lots_round_to", 0.01)),
    )


def product_tier(store: Store, demand: Any, product: Dict[str, Any]) -> Optional[str]:
    if demand is None or not getattr(demand, "ready", False) or not store.get("demand_enabled", True):
        return None
    return demand.tier(product, int(store.get("demand_hot_players", 5000)),
                       int(store.get("demand_popular_players", 500)))


def rank_of(score: float, available: int) -> float:
    """Порядок выставления: спрос + немного за запас аккаунтов.

    Больше свободных аккаунтов — меньше сорванных заказов, когда покупают
    одновременно. Бонус ограничен, чтобы не перебивать спрос.
    """
    return round(score + min(10.0, 3.0 * math.log2(1 + max(0, available))), 2)


def units_cap(product: Dict[str, Any], hours_per_unit: int) -> int:
    """Сколько штук можно купить за один заказ.

    «Наличие» на Starvell — это предел штук в заказе, а у нас штука = час.
    Поэтому ставим не число свободных аккаунтов (с 2 аккаунтами лот нельзя
    было бы купить больше чем на 2 часа), а максимум часов, который KOSell
    сдаёт за раз. Свободные аккаунты решают другое — в продаже лот или нет.
    """
    max_hours = int(product.get("max_hours") or 720)
    return max(1, min(999, max_hours // max(1, int(hours_per_unit))))


def unit_cost(product: Dict[str, Any], hours_per_unit: int) -> float:
    per_hour = float(product.get("price_per_hour_rub") or product.get("price_per_hour") or 0)
    return per_hour * max(1, int(hours_per_unit))


def make_title(store: Store, game: str) -> str:
    custom = (store.get("lots_title_template") or "").strip()
    templates = [custom] if custom and "{game}" in custom else None
    if templates:
        from core.textfit import DEFAULT_TITLE_TEMPLATES
        templates = templates + DEFAULT_TITLE_TEMPLATES
    return build_title(
        game,
        hours_per_unit=int(store.get("hours_per_unit", 1)),
        min_quantity=int(store.get("min_quantity", 1)),
        templates=templates,
    )


def make_description(store: Store, game: str) -> str:
    return build_description(
        game,
        hours_per_unit=int(store.get("hours_per_unit", 1)),
        min_quantity=int(store.get("min_quantity", 1)),
        template=(store.get("lots_description_template") or "").strip() or None,
    )


def offer_title(offer: Dict[str, Any]) -> str:
    return ((offer.get("descriptions") or {}).get("rus") or {}).get("briefDescription") or ""


def offer_game(offer: Dict[str, Any], patterns: List[str]) -> Optional[str]:
    return extract_game(offer_title(offer), patterns)


async def _call(cb: Progress, *args: Any, **kwargs: Any) -> None:
    if cb is None:
        return
    res = cb(*args, **kwargs)
    if asyncio.iscoroutine(res):
        await res


# ---------------------------------------------------------------- план

async def build_plan(
    store: Store,
    ks: KosellAPI,
    *,
    live_offers: Optional[List[Dict[str, Any]]] = None,
    only_missing: bool = True,
    limit: Optional[int] = None,
    demand: Any = None,
    slots: Optional[Dict[int, int]] = None,
    products: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Какие лоты стоит создать — лучшие игры первыми.

    Порядок — по спросу в Steam (онлайн и отзывы) с небольшим бонусом за
    запас аккаунтов в KOSell. slots — свободные места по категориям
    ({category_id: сколько ещё можно}); лишнее в план не попадает.
    """
    targets = rental_targets(load_catalog())
    if not targets:
        raise RuntimeError("каталог Starvell пуст — сначала обновите его")

    by_name = {norm_name(t["game_name"]): t for t in targets}
    fallback = next((t for t in targets if t["game_slug"] == FALLBACK_GAME_SLUG), None)

    if products is None:
        products = await ks.products(currency="RUB")
    if products is None:
        raise RuntimeError("KOSell не ответил — проверьте API-ключ и связь")

    patterns = store.get("game_title_patterns") or []
    min_stock = max(0, int(store.get("lots_min_stock", 1)))
    max_cost = float(store.get("lots_max_cost_rub", 10.0) or 0)
    taken = set()
    for offer in live_offers or []:
        game = offer_game(offer, patterns)
        if game:
            taken.add(game_key(game))
    for m in store.mappings:
        if m.get("offer_id") or m.get("offer_created"):
            taken.add(m.get("key"))
            if m.get("title_key"):
                taken.add(m["title_key"])

    per_unit = max(1, int(store.get("hours_per_unit", 1)))
    plan: List[Dict[str, Any]] = []

    for product in products:
        name = product.get("name") or ""
        key = game_key(name)
        available = int(product.get("available_accounts") or 0)
        if not key or available < max(1, min_stock):
            continue
        if only_missing and key in taken:
            continue
        if int(product.get("min_hours") or 1) > per_unit * max(1, int(store.get("min_quantity", 1))):
            continue   # KOSell не сдаёт так мало часов — лот нельзя будет выдать

        target = by_name.get(key) or fallback
        if not target:
            continue
        cost = unit_cost(product, per_unit)
        if cost <= 0:
            continue
        if max_cost and cost / per_unit > max_cost:
            continue   # аномально дорогая аренда — не продастся, а место займёт

        title = make_title(store, name)
        tier = product_tier(store, demand, product)
        info = demand.info(product) if demand is not None and getattr(demand, "ready", False) else {}
        score = float(info.get("score") or 0.0)
        plan.append({
            "key": key,
            "product_id": int(product["id"]),
            "product_name": name,
            "cost_rub": round(cost, 2),
            "price_rub": price_for(store, cost, tier),
            "available": available,
            "units": units_cap(product, per_unit),
            "tier": tier,
            "score": score,
            "players": int(info.get("peak") or 0),
            "reviews": int(info.get("reviews") or 0),
            "rank": rank_of(score, available),
            "game_id": target["game_id"],
            "game_name": target["game_name"],
            "category_id": target["category_id"],
            "sub_category_id": target["sub_category_id"],
            "title": title,
            "title_key": game_key(extract_game(title, patterns) or name),
            "description": make_description(store, name),
        })

    plan.sort(key=lambda p: (-p["rank"], -p["available"], p["product_name"]))

    if slots is not None:
        left = dict(slots)
        capped = []
        for item in plan:
            cid = int(item["category_id"])
            if left.get(cid, 10 ** 6) > 0:
                capped.append(item)
                left[cid] = left.get(cid, 10 ** 6) - 1
        plan = capped
    return plan[:limit] if limit else plan


def build_plan_cap(plan: List[Dict[str, Any]], slots: Optional[Dict[int, int]]) -> List[Dict[str, Any]]:
    """Оставляет в плане столько лотов, сколько влезает по местам категорий."""
    if not slots:
        return list(plan)
    left = dict(slots)
    out = []
    for item in plan:
        cid = int(item["category_id"])
        if cid not in left:
            out.append(item)
        elif left[cid] > 0:
            out.append(item)
            left[cid] -= 1
    return out


# ---------------------------------------------------------------- атрибуты

def split_attributes(
    template_attributes: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Атрибуты лота-образца -> basicAttributes и numericAttributes.

    Форма создания лота на сайте шлёт их раздельно, а поле attributes затирает.
    Образцом может быть лот, его attributes из list-my или offerDetails заказа.
    """
    basic: List[Dict[str, Any]] = []
    numeric: List[Dict[str, Any]] = []
    for attr in template_attributes or []:
        attr_id = attr.get("id")
        if not attr_id:
            continue
        value = attr.get("value") if isinstance(attr.get("value"), dict) else {}
        option_id = attr.get("optionId") or value.get("id")
        numeric_value = attr.get("numericValue")
        if numeric_value is None:
            numeric_value = value.get("numericValue")
        if option_id:
            basic.append({"id": attr_id, "optionId": option_id})
        elif numeric_value is not None:
            numeric.append({"id": attr_id, "numericValue": int(numeric_value)})
    return basic, numeric


async def find_template(
    sv: StarvellAPI, sub_category_id: int,
    live_offers: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """Атрибуты для новых лотов: свой живой лот -> своя продажа -> пусто."""
    for offer in live_offers or []:
        if int(offer.get("subCategoryId") or 0) == int(sub_category_id) and offer.get("attributes"):
            return offer["attributes"], f"лот {offer.get('id')}"
    try:
        for order in await sv.get_orders():
            details = order.get("offerDetails") or {}
            if (details.get("subCategory") or {}).get("id") == int(sub_category_id) \
                    and details.get("attributes"):
                return details["attributes"], "недавняя продажа"
    except StarvellError:
        pass
    return [], "нет"


def build_offer_payload(
    item: Dict[str, Any], *,
    basic_attributes: List[Dict[str, Any]],
    numeric_attributes: List[Dict[str, Any]],
    availability: int = 999,
) -> Dict[str, Any]:
    """Тело запроса создания лота — ровно как его шлёт форма на сайте."""
    return {
        "type": "LOT",
        "categoryId": int(item["category_id"]),
        "subCategoryId": int(item["sub_category_id"]),
        "price": f"{float(item['price_rub']):.2f}",
        "availability": int(availability),
        "isActive": True,
        "goods": [],
        "goodsInstruction": None,
        "tags": [],
        "basicAttributes": basic_attributes,
        "numericAttributes": numeric_attributes,
        "instantDelivery": False,
        "autoDelivery": False,
        "postPaymentMessage": None,
        "deliveryTime": None,
        "descriptions": {"rus": {
            "briefDescription": item["title"],
            "description": item["description"],
        }},
    }


# ---------------------------------------------------------------- создание

async def create_lots(
    store: Store,
    sv: StarvellAPI,
    plan: List[Dict[str, Any]],
    *,
    live_offers: Optional[List[Dict[str, Any]]] = None,
    dry_run: bool = False,
    progress: Progress = None,
    cancel: Optional[asyncio.Event] = None,
) -> Dict[str, Any]:
    """Создаёт лоты по плану и сразу записывает привязки.

    progress(done, total, name, created, failed) — для полосы прогресса.
    cancel — событие остановки: текущий лот доделывается, дальше стоп.
    """
    pause = float(store.get("lots_batch_pause", 1.5))
    templates: Dict[int, Tuple[List, List, str]] = {}
    created, failed, skipped = 0, 0, 0
    errors: List[str] = []
    streak: Dict[str, int] = {}
    results: List[Dict[str, Any]] = []
    total = len(plan)
    stopped = ""

    for index, item in enumerate(plan, 1):
        if cancel is not None and cancel.is_set():
            stopped = "остановлено вручную"
            break

        sub = int(item["sub_category_id"])
        if sub not in templates:
            attrs, source = await find_template(sv, sub, live_offers)
            basic, numeric = split_attributes(attrs)
            templates[sub] = (basic, numeric, source)
            logger.info("атрибуты для подкатегории %s: %s (%d шт.)", sub, source, len(attrs))
        basic, numeric, _ = templates[sub]

        payload = build_offer_payload(
            item, basic_attributes=basic, numeric_attributes=numeric,
            availability=int(item.get("units") or 720),
        )

        if dry_run:
            skipped += 1
            results.append({"item": item, "payload": payload, "created": False})
            await _call(progress, index, total, item["product_name"], created, failed)
            continue

        try:
            response = await sv.create_offer(payload)
        except StarvellError as exc:
            failed += 1
            reason = human_error(exc)
            errors.append(f"{item['product_name']}: {reason}")
            logger.warning("не создан лот %s: %s", item["product_name"], exc.message or exc)
            streak[reason] = streak.get(reason, 0) + 1
            await _call(progress, index, total, item["product_name"], created, failed)
            if streak[reason] >= 5:
                stopped = f"остановлено: пять одинаковых ошибок подряд — «{reason}»"
                break
            await asyncio.sleep(pause)
            continue
        streak.clear()

        offer_id = _extract_offer_id(response)
        public_id = response.get("publicId") if isinstance(response, dict) else None
        if not offer_id and not public_id:
            failed += 1
            errors.append(f"{item['product_name']}: Starvell не вернул id лота")
            await _call(progress, index, total, item["product_name"], created, failed)
            await asyncio.sleep(pause)
            continue

        store.upsert_mapping({
            "key": item["key"],
            "title_key": item.get("title_key"),
            "game": item["product_name"],
            "offer_id": offer_id,
            "offer_public_id": public_id,
            "offer_title": item["title"],
            "product_id": item["product_id"],
            "product_name": item["product_name"],
            "price_rub": item["price_rub"],
            "enabled": True,
            "auto": True,
            "game_id": item["game_id"],
            "category_id": item["category_id"],
            "sub_category_id": item["sub_category_id"],
            "offer_created": True,
        })
        created += 1
        results.append({"item": item, "offer_id": offer_id,
                        "public_id": public_id, "created": True})
        await _call(progress, index, total, item["product_name"], created, failed)
        await asyncio.sleep(pause)

    return {
        "created": created, "failed": failed, "skipped": skipped,
        "total": total, "errors": errors, "dry_run": dry_run,
        "stopped": stopped, "results": results,
    }


def _extract_offer_id(response: Any) -> Optional[int]:
    if isinstance(response, dict):
        for key in ("id", "offerId", "offer_id"):
            if response.get(key):
                try:
                    return int(response[key])
                except (TypeError, ValueError):
                    pass
        nested = response.get("offer") or response.get("data")
        if isinstance(nested, dict):
            return _extract_offer_id(nested)
    return None


# ---------------------------------------------------------------- переоценка

async def reprice_plan(
    store: Store, ks: KosellAPI, live_offers: List[Dict[str, Any]], demand: Any = None,
) -> List[Dict[str, Any]]:
    """Какие лоты поменяют цену при текущих наценке и спросе."""
    products = {int(p["id"]): p for p in (await ks.products(currency="RUB") or [])}
    per_unit = max(1, int(store.get("hours_per_unit", 1)))
    patterns = store.get("game_title_patterns") or []
    changes: List[Dict[str, Any]] = []
    for offer in live_offers:
        game = offer_game(offer, patterns)
        mapping = store.find_mapping_by_offer(offer.get("id"), offer.get("publicId")) \
            or (store.find_mapping(game) if game else None)
        if not mapping:
            continue
        product = products.get(int(mapping.get("product_id") or 0))
        if not product:
            continue
        cost = unit_cost(product, per_unit)
        if cost <= 0:
            continue
        tier = product_tier(store, demand, product)
        new_price = price_for(store, cost, tier)
        old_price = round(float(offer.get("price") or 0), 2)
        if abs(new_price - old_price) >= 0.01:
            changes.append({"offer": offer, "game": game or mapping.get("game"),
                            "old": old_price, "new": new_price, "cost": round(cost, 2),
                            "tier": tier})
    return changes


async def apply_prices(
    sv: StarvellAPI, changes: List[Dict[str, Any]], *,
    progress: Progress = None, cancel: Optional[asyncio.Event] = None, pause: float = 0.8,
) -> Dict[str, Any]:
    done, failed = 0, 0
    errors: List[str] = []
    for index, ch in enumerate(changes, 1):
        if cancel is not None and cancel.is_set():
            break
        try:
            await sv.set_offer_price(ch["offer"], ch["new"])
            done += 1
        except StarvellError as exc:
            failed += 1
            errors.append(f"{ch['game']}: {human_error(exc)}")
        await _call(progress, index, len(changes), ch["game"], done, failed)
        await asyncio.sleep(pause)
    return {"updated": done, "failed": failed, "errors": errors}


# ---------------------------------------------------------------- ротация

def _age_days(offer: Dict[str, Any]) -> float:
    from core.texts import parse_ts
    ts = parse_ts(offer.get("createdAt") or offer.get("listedAt"))
    return (time.time() - ts) / 86400 if ts else 0.0


def weak_offers(
    store: Store, offers: List[Dict[str, Any]], sold_games: set,
) -> List[Dict[str, Any]]:
    """Лоты, которые занимают место зря: старше N дней, мало просмотров, нет продаж.

    Лоты на модерации и снятые вами вручную не трогаем.
    """
    min_age = float(store.get("rotate_after_days", 7))
    max_views = int(store.get("rotate_max_views", 5))
    patterns = store.get("game_title_patterns") or []
    out = []
    for offer in offers:
        if str(offer.get("moderationStatus") or "").upper() in ("PENDING", "IN_REVIEW"):
            continue
        if _age_days(offer) < min_age:
            continue
        if int(offer.get("viewsCount") or 0) > max_views:
            continue
        game = offer_game(offer, patterns) or ""
        if game_key(game) in sold_games:
            continue
        out.append({"offer": offer, "game": game,
                    "views": int(offer.get("viewsCount") or 0),
                    "age": round(_age_days(offer), 1)})
    out.sort(key=lambda w: (w["views"], -w["age"]))
    return out


def rotation_pairs(
    weak: List[Dict[str, Any]], candidates: List[Dict[str, Any]],
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Слабый лот -> игра на замену (из той же категории, по убыванию спроса)."""
    pairs = []
    pool = list(candidates)
    for w in weak:
        cat = int(w["offer"].get("categoryId") or DEFAULT_CATEGORY_ID)
        pick = next((c for c in pool if int(c["category_id"]) == cat), None)
        if not pick:
            continue
        pool.remove(pick)
        pairs.append((w, pick))
    return pairs
