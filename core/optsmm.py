"""Асинхронный клиент OptSMM API v2 (накрутка соцсетей).

Документация: https://optsmm.ru/developer
Все методы — POST https://optsmm.ru/api/v2 с полями key и action.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from core.log import get_logger

logger = get_logger("optsmm")

API_URL = "https://optsmm.ru/api/v2"
TIMEOUT = 30

# статусы заказа OptSMM
DONE = {"Completed"}
PARTIAL = {"Partial"}
FAILED = {"Canceled", "Cancelled", "Fail", "Failed"}

HUMAN_ERRORS = {
    "user_inactive": "ключ OptSMM не принят (пользователь неактивен)",
    "incorrect_request": "неверный запрос",
    "incorrect_service_id": "такой услуги нет",
    "not_enough_funds": "недостаточно средств на балансе OptSMM",
    "neworder.error.not_enough_funds": "недостаточно средств на балансе OptSMM",
    "connection_error": "нет связи с OptSMM",
}


class OptSmmError(Exception):
    pass


def human_error(text: Any) -> str:
    raw = str(text or "")
    return HUMAN_ERRORS.get(raw.strip().lower(), raw)


class OptSmmAPI:
    def __init__(self, api_key: str, proxy_url: str = "") -> None:
        self.api_key = (api_key or "").strip()
        self.proxy_url = proxy_url or None
        self._session: Optional[aiohttp.ClientSession] = None
        self._services: List[Dict[str, Any]] = []
        self._services_ts = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            connector = None
            if self.proxy_url and self.proxy_url.startswith("socks"):
                from aiohttp_socks import ProxyConnector  # type: ignore
                connector = ProxyConnector.from_url(self.proxy_url)
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT), connector=connector)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _call(self, action: str, **params: Any) -> Any:
        if not self.api_key:
            raise OptSmmError("не задан API-ключ OptSMM")
        await self.start()
        data = {"key": self.api_key, "action": action, **{k: str(v) for k, v in params.items()}}
        proxy = self.proxy_url if self.proxy_url and not self.proxy_url.startswith("socks") else None
        last: Optional[BaseException] = None
        # add не повторяем: повтор после таймаута может создать второй заказ
        attempts = 1 if action == "add" else 3
        for attempt in range(attempts):
            try:
                assert self._session is not None
                async with self._session.post(API_URL, data=data, proxy=proxy) as resp:
                    body = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                last = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(2 * (attempt + 1))
                continue
            if isinstance(body, dict) and body.get("error"):
                raise OptSmmError(human_error(body["error"]))
            return body
        raise OptSmmError(f"{HUMAN_ERRORS['connection_error']}: {last}")

    # ---------- методы ----------

    async def services(self, force: bool = False) -> List[Dict[str, Any]]:
        if force or not self._services or time.time() - self._services_ts > 600:
            data = await self._call("services")
            if isinstance(data, list):
                self._services = data
                self._services_ts = time.time()
        return self._services

    async def service(self, service_id: Any) -> Optional[Dict[str, Any]]:
        for s in await self.services():
            if str(s.get("service")) == str(service_id):
                return s
        return None

    async def balance(self) -> Tuple[float, str]:
        data = await self._call("balance")
        return float(data.get("balance") or 0), str(data.get("currency") or "RUB")

    async def add(self, service: Any, link: str, quantity: int) -> int:
        data = await self._call("add", service=service, link=link, quantity=int(quantity))
        if not isinstance(data, dict) or "order" not in data:
            raise OptSmmError(f"неожиданный ответ: {str(data)[:200]}")
        return int(data["order"])

    async def status_many(self, ids: List[int]) -> Dict[int, Any]:
        out: Dict[int, Any] = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            data = await self._call("status", orders=",".join(str(x) for x in chunk))
            if isinstance(data, dict):
                for key, value in data.items():
                    try:
                        out[int(key)] = value
                    except (TypeError, ValueError):
                        pass
        return out

    async def refill(self, order_id: int) -> Any:
        return await self._call("refill", order=order_id)

    async def cancel(self, order_id: int) -> Any:
        return await self._call("cancel", order=order_id)
