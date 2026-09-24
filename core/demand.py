"""Спрос на игры по данным Steam.

Источники (публичные, без ключа):
  * онлайн прямо сейчас — api.steampowered.com GetNumberOfCurrentPlayers;
  * число отзывов       — store.steampowered.com/appreviews (долгий спрос);
  * appid игры          — из ссылки на обложку в каталоге KOSell, а для
                          изданий-бандлов — через поиск магазина Steam.

Онлайн сильно зависит от времени суток, поэтому в расчёт идёт пик за
последние дни, а не одно измерение.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

import aiohttp

from core.log import get_logger
from core.storage import DATA_DIR

logger = get_logger("demand")

DEMAND_FILE = os.path.join(DATA_DIR, "demand.json")
PLAYERS_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
REVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
SEARCH_URL = "https://store.steampowered.com/api/storesearch/"

PLAYERS_TTL = 6 * 3600          # онлайн обновляем раз в 6 часов
REVIEWS_TTL = 7 * 86400         # отзывы — раз в неделю
PEAK_SAMPLES = 8                # пик по последним 8 замерам (~2 суток)

_APPID_RE = re.compile(r"/apps/(\d+)/")
_EDITION_RE = re.compile(
    r"\s*[-–—:]?\s*(digital\s+)?(deluxe|ultimate|complete|gold|premium|standard|"
    r"definitive|collector'?s?|goty|game of the year|anniversary|excalibur|"
    r"starter|enhanced|remastered)?\s*(edition|bundle|collection|pack)\b.*$",
    re.I,
)

TIER_HOT, TIER_POPULAR, TIER_NORMAL = "hot", "popular", "normal"
TIER_ICON = {TIER_HOT: "🔥", TIER_POPULAR: "⭐", TIER_NORMAL: ""}
TIER_NAME = {TIER_HOT: "хит", TIER_POPULAR: "популярная", TIER_NORMAL: "обычная"}


def appid_from_image(url: str) -> Optional[int]:
    m = _APPID_RE.search(url or "")
    return int(m.group(1)) if m else None


def base_title(name: str) -> str:
    """«Hogwarts Legacy: Digital Deluxe Edition» -> «Hogwarts Legacy»."""
    cleaned = _EDITION_RE.sub("", name or "").strip(" -–—:")
    return cleaned or name


def score_of(peak: int, reviews: int) -> float:
    """0..100: 65% — пиковый онлайн, 35% — отзывы (логарифмическая шкала).

    100 тыс. онлайн или 1 млн отзывов — это уже максимум своей половины.
    """
    p = min(1.0, math.log10(max(0, peak) + 1) / 5.0)
    r = min(1.0, math.log10(max(0, reviews) + 1) / 6.0)
    return round(100 * (0.65 * p + 0.35 * r), 1)


class Demand:
    def __init__(self, path: str = DEMAND_FILE, proxy_url: str = "") -> None:
        self.path = path
        self.proxy_url = proxy_url or None
        self.appids: Dict[str, int] = {}          # product_id -> appid (0 = не нашли)
        self.games: Dict[str, Dict[str, Any]] = {}  # appid -> данные
        self.updated_at: float = 0.0
        self.refreshing = False
        self._load()

    # ---------------------------------------------------------- хранение

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        self.appids = {str(k): int(v) for k, v in (data.get("appids") or {}).items()}
        self.games = data.get("games") or {}
        self.updated_at = float(data.get("updated_at") or 0)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"appids": self.appids, "games": self.games,
                       "updated_at": self.updated_at}, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    @property
    def ready(self) -> bool:
        return bool(self.games)

    # ---------------------------------------------------------- чтение

    def info(self, product: Dict[str, Any]) -> Dict[str, Any]:
        """Спрос по товару KOSell. Пустой словарь, если данных нет."""
        appid = self.appids.get(str(product.get("id")))
        if not appid:
            appid = appid_from_image(product.get("header_image") or "")
        if not appid:
            return {}
        g = self.games.get(str(appid))
        if not g:
            return {"appid": appid}
        peak = int(g.get("peak") or 0)
        reviews = int(g.get("reviews") or 0)
        return {"appid": appid, "players": int(g.get("players") or 0), "peak": peak,
                "reviews": reviews, "score": score_of(peak, reviews)}

    def tier(self, product: Dict[str, Any], hot_players: int, popular_players: int) -> str:
        d = self.info(product)
        peak = int(d.get("peak") or 0)
        if peak >= hot_players:
            return TIER_HOT
        if peak >= popular_players or int(d.get("reviews") or 0) >= 20000:
            return TIER_POPULAR
        return TIER_NORMAL

    def score(self, product: Dict[str, Any]) -> float:
        return float(self.info(product).get("score") or 0.0)

    # ---------------------------------------------------------- обновление

    async def refresh(
        self, products: List[Dict[str, Any]], *, force: bool = False,
        progress: Optional[Callable[..., Any]] = None,
        cancel: Optional[asyncio.Event] = None, concurrency: int = 6,
    ) -> Dict[str, Any]:
        """Обновляет онлайн и отзывы. Возвращает сводку."""
        if self.refreshing:
            return {"skipped": True}
        self.refreshing = True
        try:
            return await self._refresh(products, force, progress, cancel, concurrency)
        finally:
            self.refreshing = False

    async def _refresh(self, products, force, progress, cancel, concurrency):
        timeout = aiohttp.ClientTimeout(total=20)
        connector = None
        if self.proxy_url and self.proxy_url.startswith("socks"):
            from aiohttp_socks import ProxyConnector  # type: ignore
            connector = ProxyConnector.from_url(self.proxy_url)
        http_proxy = self.proxy_url if self.proxy_url and not self.proxy_url.startswith("socks") else None

        async with aiohttp.ClientSession(timeout=timeout, connector=connector,
                                         headers={"User-Agent": "Mozilla/5.0"}) as http:
            # 1. appid для всех товаров
            missing = [p for p in products if str(p.get("id")) not in self.appids]
            for p in missing:
                appid = appid_from_image(p.get("header_image") or "")
                if appid:
                    self.appids[str(p["id"])] = appid
            for p in [p for p in missing if str(p.get("id")) not in self.appids]:
                if cancel is not None and cancel.is_set():
                    break
                self.appids[str(p["id"])] = await self._search_appid(http, http_proxy, p.get("name", ""))
                await asyncio.sleep(0.3)

            # 2. онлайн и отзывы
            now = time.time()
            todo = sorted({a for a in self.appids.values() if a})
            sem = asyncio.Semaphore(concurrency)
            done = {"n": 0, "ok": 0, "fail": 0}

            async def one(appid: int) -> None:
                if cancel is not None and cancel.is_set():
                    return
                g = self.games.setdefault(str(appid), {})
                async with sem:
                    if force or now - float(g.get("players_ts") or 0) > PLAYERS_TTL:
                        players = await self._players(http, http_proxy, appid)
                        if players is not None:
                            samples = (g.get("samples") or [])[-(PEAK_SAMPLES - 1):] + [players]
                            g.update(players=players, samples=samples, peak=max(samples),
                                     players_ts=now)
                            done["ok"] += 1
                        else:
                            done["fail"] += 1
                    if force or now - float(g.get("reviews_ts") or 0) > REVIEWS_TTL:
                        reviews = await self._reviews(http, http_proxy, appid)
                        if reviews is not None:
                            g.update(reviews=reviews, reviews_ts=now)
                done["n"] += 1
                if progress:
                    res = progress(done["n"], len(todo), str(appid), done["ok"], done["fail"])
                    if asyncio.iscoroutine(res):
                        await res

            await asyncio.gather(*(one(a) for a in todo))

        self.updated_at = time.time()
        self._save()
        found = sum(1 for a in self.appids.values() if a)
        logger.info("спрос обновлён: игр %d, онлайн получен %d, ошибок %d",
                    found, done["ok"], done["fail"])
        return {"games": found, "no_appid": len(self.appids) - found,
                "updated": done["ok"], "failed": done["fail"]}

    @staticmethod
    async def _get_json(http, proxy, url, params=None) -> Optional[Any]:
        for attempt in range(2):
            try:
                async with http.get(url, params=params, proxy=proxy) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(5)
                        continue
                    if resp.status != 200:
                        return None
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                await asyncio.sleep(1 + attempt)
        return None

    async def _players(self, http, proxy, appid: int) -> Optional[int]:
        data = await self._get_json(http, proxy, PLAYERS_URL, {"appid": appid})
        resp = (data or {}).get("response") or {}
        if resp.get("result") == 1:
            return int(resp.get("player_count") or 0)
        if resp:                      # игра есть, но статистики нет — считаем 0
            return 0
        return None

    async def _reviews(self, http, proxy, appid: int) -> Optional[int]:
        data = await self._get_json(http, proxy, REVIEWS_URL.format(appid=appid), {
            "json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0})
        summary = (data or {}).get("query_summary") or {}
        if "total_reviews" in summary:
            return int(summary["total_reviews"])
        return None

    async def _search_appid(self, http, proxy, name: str) -> int:
        for term in dict.fromkeys([base_title(name), name]):
            data = await self._get_json(http, proxy, SEARCH_URL, {"term": term, "cc": "us", "l": "en"})
            items = (data or {}).get("items") or []
            if items:
                return int(items[0].get("id") or 0)
        return 0
