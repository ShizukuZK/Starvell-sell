"""Асинхронный клиент KOSell Public API v1 (аренда Steam-аккаунтов).

Документация: https://ru.kosell.store/api/docs
Спецификация:  https://ru.kosell.store/api/openapi.json
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from core.log import get_logger

logger = get_logger("kosell")

API_BASE = "https://www.kosell.store/api/v1"
TIMEOUT = 25
RATE_LIMIT_PER_MIN = 55  # официальный лимит 60/мин, держим запас


class KosellError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


HUMAN_ERRORS = {
    "connection_error": "нет связи с KOSell",
    "insufficient_balance": "недостаточно средств на балансе KOSell",
    "no_accounts_available": "нет свободных аккаунтов",
    "product_not_found": "товар не найден",
    "rental_not_found": "аренда не найдена",
    "invalid_api_key": "неверный API-ключ",
    "http_401": "неверный API-ключ",
    "http_403": "доступ запрещён",
    "http_404": "не найдено",
    "http_429": "превышен лимит запросов",
}


def human_error(code: str) -> str:
    return HUMAN_ERRORS.get(code, code)


class KosellAPI:
    def __init__(self, api_key: str, proxy_url: str = "") -> None:
        self.api_key = _sanitize_key(api_key)
        self.proxy_url = proxy_url or None
        self._session: Optional[aiohttp.ClientSession] = None
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "KosellAPI":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            connector = None
            if self.proxy_url and self.proxy_url.startswith("socks"):
                from aiohttp_socks import ProxyConnector  # type: ignore
                connector = ProxyConnector.from_url(self.proxy_url)
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                connector=connector,
                headers={
                    "X-API-Key": self.api_key,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ---------- низкий уровень ----------

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.time()
            while self._calls and now - self._calls[0] > 60:
                self._calls.popleft()
            if len(self._calls) >= RATE_LIMIT_PER_MIN:
                wait = 60 - (now - self._calls[0]) + 0.5
                if wait > 0:
                    logger.warning("самоограничение, ждём %.1f с", wait)
                    await asyncio.sleep(wait)
            self._calls.append(time.time())

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        retries: int = 3,
    ) -> Tuple[Optional[Any], Optional[str]]:
        """Возвращает (данные, код_ошибки). Один из двух всегда None."""
        await self.start()
        url = f"{API_BASE}{path}"
        last_code = "connection_error"

        for attempt in range(retries):
            await self._throttle()
            try:
                assert self._session is not None
                kwargs: Dict[str, Any] = {}
                if self.proxy_url and not self.proxy_url.startswith("socks"):
                    kwargs["proxy"] = self.proxy_url
                async with self._session.request(
                    method, url, json=json_body, params=params,
                    headers=headers, **kwargs,
                ) as resp:
                    if resp.status == 429:
                        wait = int(resp.headers.get("Retry-After", "5") or 5)
                        logger.warning("429 на %s, ждём %s с", path, wait)
                        await asyncio.sleep(wait)
                        last_code = "http_429"
                        continue
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = None
                    if 200 <= resp.status < 300:
                        return data, None
                    code = "http_%d" % resp.status
                    if isinstance(data, dict):
                        code = str(data.get("error") or data.get("detail") or code)
                    logger.warning("%s %s -> %s (%s)", method, path, resp.status, code)
                    return None, code
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("сеть %s %s (попытка %d): %s", method, path, attempt + 1, exc)
                last_code = "connection_error"
                if attempt < retries - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))

        return None, last_code

    # ---------- аккаунт ----------

    async def balance(self) -> Optional[Dict[str, Any]]:
        data, _ = await self._request("GET", "/account/balance")
        return data if isinstance(data, dict) else None

    # ---------- товары ----------

    async def products(
        self, search: Optional[str] = None, currency: str = "RUB",
    ) -> Optional[List[Dict[str, Any]]]:
        params: Dict[str, Any] = {"currency": currency}
        if search:
            params["search"] = search
        data, _ = await self._request("GET", "/rental/products", params=params)
        return data if isinstance(data, list) else None

    async def calculate_price(self, product_id: int, hours: int) -> Optional[Dict[str, Any]]:
        data, _ = await self._request(
            "POST", "/rental/calculate-price",
            json_body={"product_id": int(product_id), "hours": int(hours)},
        )
        return data if isinstance(data, dict) else None

    async def calculate_extend_price(self, rental_uid: str, hours: int) -> Optional[Dict[str, Any]]:
        data, _ = await self._request(
            "POST", "/rental/calculate-extend-price",
            json_body={"rental_uid": rental_uid, "hours": int(hours)},
        )
        return data if isinstance(data, dict) else None

    # ---------- аренда ----------

    async def rent(
        self, product_id: int, hours: int, currency: str = "RUB",
        idempotency_key: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        key = idempotency_key or uuid.uuid4().hex
        data, err = await self._request(
            "POST", "/rental/rent",
            json_body={
                "product_id": int(product_id),
                "duration_hours": int(hours),
                "currency": currency,
                "idempotency_key": key,
            },
            headers={"Idempotency-Key": key},
            retries=1,  # аренда платная — не повторяем вслепую
        )
        return (data if isinstance(data, dict) else None), err

    async def active(self) -> Optional[List[Dict[str, Any]]]:
        data, _ = await self._request("GET", "/rental/active")
        return data if isinstance(data, list) else None

    async def credentials(self, rental_uid: str) -> Optional[Dict[str, Any]]:
        data, _ = await self._request("GET", f"/rental/{rental_uid}/credentials")
        return data if isinstance(data, dict) else None

    async def guard_code(self, rental_uid: str) -> Optional[Dict[str, Any]]:
        data, _ = await self._request("GET", f"/rental/{rental_uid}/code")
        return data if isinstance(data, dict) else None

    async def extend(
        self, rental_uid: str, hours: int, currency: str = "RUB",
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        data, err = await self._request(
            "POST", f"/rental/{rental_uid}/extend",
            json_body={"hours": int(hours), "currency": currency},
            retries=1,
        )
        return (data if isinstance(data, dict) else None), err

    async def terminate(self, rental_uid: str) -> bool:
        _, err = await self._request("POST", f"/rental/{rental_uid}/terminate")
        return err is None

    async def rotate_password(self, rental_uid: str) -> bool:
        _, err = await self._request("POST", f"/rental/{rental_uid}/rotate-password")
        return err is None

    async def full_logout(self, rental_uid: str) -> bool:
        _, err = await self._request("POST", f"/rental/{rental_uid}/full-logout")
        return err is None


def _sanitize_key(raw: str) -> str:
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] in "\"'`" and s[-1] == s[0]:
        s = s[1:-1].strip()
    return "".join(ch for ch in s if ch.isprintable() and not ch.isspace())
