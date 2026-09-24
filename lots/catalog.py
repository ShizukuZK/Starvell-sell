"""Каталог Starvell: игры -> категории -> подкатегории -> атрибуты.

Starvell не отдаёт схему атрибутов отдельным эндпоинтом, поэтому она
восстанавливается по уже опубликованным лотам подкатегории (публичные данные).
Результат кэшируется в storage/starvell_catalog.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Callable, Dict, List, Optional

from core.log import get_logger
from core.starvell import StarvellAPI
from core.storage import CATALOG_FILE

logger = get_logger("catalog")

RENTAL_WORDS = ("аренда", "прокат", "rent")
ACCOUNT_SLUGS = ("accounts", "account")


def is_rental_subcategory(name: str) -> bool:
    low = (name or "").strip().lower()
    return any(word in low for word in RENTAL_WORDS)


async def scan_catalog(
    sv: StarvellAPI,
    *,
    rental_only: bool = True,
    progress: Optional[Callable[[int, int, str], Any]] = None,
    pause: float = 0.4,
) -> Dict[str, Any]:
    """Полный обход публичного каталога Starvell."""
    games = await sv.public_games()
    result: Dict[str, Any] = {
        "updated_at": int(time.time()),
        "rental_only": rental_only,
        "games": [],
    }

    total = len(games)
    for index, game in enumerate(games, 1):
        slug = game.get("slug")
        if not slug:
            continue
        if progress:
            res = progress(index, total, game.get("name", slug))
            if asyncio.iscoroutine(res):
                await res

        first_cat = game.get("firstCategorySlug") or "accounts"
        try:
            props = await sv.public_category(slug, first_cat)
        except Exception as exc:
            logger.debug("игра %s: %s", slug, exc)
            continue

        categories = (props.get("game") or {}).get("categories") or []
        game_entry: Dict[str, Any] = {
            "id": game.get("id"),
            "name": game.get("name"),
            "slug": slug,
            "type": game.get("type"),
            "categories": [],
        }

        for cat in categories:
            cat_slug = cat.get("slug")
            if rental_only and cat_slug not in ACCOUNT_SLUGS:
                continue
            entry = await _scan_category(sv, slug, cat, rental_only=rental_only, pause=pause)
            if entry and entry["subcategories"]:
                game_entry["categories"].append(entry)

        if game_entry["categories"]:
            result["games"].append(game_entry)
        await asyncio.sleep(pause)

    save_catalog(result)
    logger.info(
        "каталог обновлён: %d игр с подходящими категориями", len(result["games"])
    )
    return result


async def _scan_category(
    sv: StarvellAPI, game_slug: str, cat: Dict[str, Any],
    *, rental_only: bool, pause: float,
) -> Optional[Dict[str, Any]]:
    cat_slug = cat.get("slug")
    if not cat_slug:
        return None
    try:
        props = await sv.public_category(game_slug, cat_slug)
    except Exception as exc:
        logger.debug("категория %s/%s: %s", game_slug, cat_slug, exc)
        return None
    await asyncio.sleep(pause)

    category = props.get("category") or {}
    subs = category.get("subCategories") or []
    offers = props.get("offers") or []

    by_sub: Dict[int, Dict[str, Any]] = {}
    for sub in subs:
        sub_id = sub.get("id")
        sub_name = sub.get("name") or ""
        if rental_only and not is_rental_subcategory(sub_name):
            continue
        by_sub[sub_id] = {
            "id": sub_id,
            "name": sub_name,
            "attributes": {},
            "prices": [],
            "offers_seen": 0,
            "sample_offer_id": None,
        }

    for offer in offers:
        sub = offer.get("subCategory") or {}
        sub_id = sub.get("id")
        target = by_sub.get(sub_id)
        if target is None:
            continue
        target["offers_seen"] += 1
        if target["sample_offer_id"] is None:
            target["sample_offer_id"] = offer.get("id")
        try:
            target["prices"].append(round(float(offer.get("price") or 0), 2))
        except (TypeError, ValueError):
            pass
        _merge_attributes(target["attributes"], offer.get("attributes") or [])

    result_subs: List[Dict[str, Any]] = []
    for sub in by_sub.values():
        prices = sorted(p for p in sub["prices"] if p > 0)
        result_subs.append({
            "id": sub["id"],
            "name": sub["name"],
            "offers_seen": sub["offers_seen"],
            "sample_offer_id": sub["sample_offer_id"],
            "price_min": prices[0] if prices else None,
            "price_low5": prices[:5],
            "attributes": list(sub["attributes"].values()),
        })

    if not result_subs:
        return None
    return {
        "id": category.get("id") or cat.get("id"),
        "name": category.get("name") or cat.get("name"),
        "slug": cat_slug,
        "subcategories": result_subs,
    }


def _merge_attributes(acc: Dict[str, Dict[str, Any]], attributes: List[Dict[str, Any]]) -> None:
    for attr in attributes:
        attr_id = attr.get("id")
        if not attr_id:
            continue
        entry = acc.setdefault(attr_id, {
            "id": attr_id,
            "name": attr.get("nameRu") or "",
            "kind": "numeric" if "numericValue" in attr else "option",
            "options": {},
        })
        if attr.get("optionId"):
            entry["kind"] = "option"
            entry["options"][attr["optionId"]] = {
                "id": attr["optionId"],
                "name": attr.get("optionNameRu") or "",
            }


def save_catalog(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(CATALOG_FILE) or ".", exist_ok=True)
    payload = json.loads(json.dumps(data))
    for game in payload.get("games", []):
        for cat in game.get("categories", []):
            for sub in cat.get("subcategories", []):
                for attr in sub.get("attributes", []):
                    if isinstance(attr.get("options"), dict):
                        attr["options"] = list(attr["options"].values())
    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_catalog() -> Dict[str, Any]:
    if not os.path.exists(CATALOG_FILE):
        return {"games": [], "updated_at": 0}
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"games": [], "updated_at": 0}


def rental_targets(catalog: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Плоский список «куда можно публиковать лоты аренды»."""
    catalog = catalog or load_catalog()
    out: List[Dict[str, Any]] = []
    for game in catalog.get("games", []):
        for cat in game.get("categories", []):
            for sub in cat.get("subcategories", []):
                out.append({
                    "game_id": game.get("id"),
                    "game_name": game.get("name"),
                    "game_slug": game.get("slug"),
                    "category_id": cat.get("id"),
                    "category_name": cat.get("name"),
                    "sub_category_id": sub.get("id"),
                    "sub_category_name": sub.get("name"),
                    "attributes": sub.get("attributes", []),
                    "price_min": sub.get("price_min"),
                    "price_low5": sub.get("price_low5", []),
                    "sample_offer_id": sub.get("sample_offer_id"),
                })
    return out
