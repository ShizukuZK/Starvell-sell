"""Асинхронный клиент Starvell.

Starvell — Next.js-приложение, поэтому используются два канала:
  * Next.js Data API  — GET /_next/data/{buildId}/{route}.json  (чтение страниц)
  * внутренний REST   — POST /api/...                            (действия)

Авторизация — cookie `session` из браузера (DevTools → Application → Cookies).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional

import aiohttp

from core.log import get_logger

logger = get_logger("starvell")

BASE_URL = "https://starvell.com"
API_URL = f"{BASE_URL}/api"
BUILD_ID_TTL = 1800
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "sec-ch-ua": '"Not)A;Brand";v="8", "Chromium";v="138", "Google Chrome";v="138"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}
DEFAULT_COOKIES = {"starvell.theme": "dark", "starvell.time_zone": "Europe/Moscow"}

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
_BUILD_ID_RE = re.compile(r'"buildId"\s*:\s*"([^"]+)"')


class StarvellError(Exception):
    """Ошибка Starvell с разобранным ответом сервера.

    status  — HTTP-код (0 — сеть), message — текст от сервера,
    code    — машинный код из extra.code (например OFFERS_BUMP_COOLDOWN).
    """

    def __init__(self, text: str = "", *, status: int = 0,
                 message: str = "", code: str = "") -> None:
        super().__init__(text or message)
        self.status = status
        self.message = message or text
        self.code = code


class StarvellAuthError(StarvellError):
    pass


class StarvellAPI:
    def __init__(
        self,
        session_cookie: str,
        proxy_url: str = "",
        user_agent: str = DEFAULT_UA,
        timeout: int = 20,
        min_interval: float = 0.35,
    ) -> None:
        self.session_cookie = (session_cookie or "").strip()
        self.proxy_url = proxy_url or None
        self.user_agent = user_agent
        self.timeout = timeout
        self.min_interval = min_interval

        self._session: Optional[aiohttp.ClientSession] = None
        self._build_id: Optional[str] = None
        self._build_id_ts: float = 0.0
        self._sid: Optional[str] = None
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()
        self.my_user_id: Optional[int] = None
        self.my_username: Optional[str] = None

    # ---------- жизненный цикл ----------

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            connector = None
            if self.proxy_url and self.proxy_url.startswith("socks"):
                from aiohttp_socks import ProxyConnector  # type: ignore
                connector = ProxyConnector.from_url(self.proxy_url)
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                connector=connector,
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "StarvellAPI":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ---------- низкий уровень ----------

    def _headers(self, referer: Optional[str] = None, extra: Optional[dict] = None) -> dict:
        h = {
            "accept": "*/*",
            "accept-language": "ru,en;q=0.9",
            "user-agent": self.user_agent,
            **BROWSER_HEADERS,
        }
        if referer:
            h["referer"] = referer
            h["origin"] = BASE_URL
        if extra:
            h.update(extra)
        return h

    def _cookies(self, include_sid: bool = False, anonymous: bool = False) -> dict:
        c = dict(DEFAULT_COOKIES)
        if not anonymous and self.session_cookie:
            c["session"] = self.session_cookie
        if include_sid and self._sid:
            c["sid"] = self._sid
        return c

    async def _throttle(self) -> None:
        async with self._lock:
            delta = time.time() - self._last_call
            if delta < self.min_interval:
                await asyncio.sleep(self.min_interval - delta)
            self._last_call = time.time()

    async def _raw(
        self,
        method: str,
        url: str,
        *,
        referer: Optional[str] = None,
        json_body: Any = None,
        include_sid: bool = False,
        anonymous: bool = False,
        as_text: bool = False,
        retries: int = 3,
    ) -> Any:
        await self.start()
        assert self._session is not None
        headers = self._headers(referer)
        if json_body is not None:
            headers["content-type"] = "application/json"

        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            await self._throttle()
            try:
                kwargs: Dict[str, Any] = {}
                if self.proxy_url and not self.proxy_url.startswith("socks"):
                    kwargs["proxy"] = self.proxy_url
                async with self._session.request(
                    method, url,
                    headers=headers,
                    cookies=self._cookies(include_sid, anonymous),
                    json=json_body,
                    **kwargs,
                ) as resp:
                    # запоминаем sid, если сервер его выдал
                    sid = resp.cookies.get("sid")
                    if sid is not None:
                        self._sid = sid.value

                    if resp.status == 401:
                        raise StarvellAuthError("session cookie недействителен")
                    if resp.status == 429:
                        await asyncio.sleep(5 * (attempt + 1))
                        last_exc = StarvellError("429 rate limit")
                        continue
                    if resp.status >= 400:
                        body = await resp.text()
                        message, code = _parse_error(body)
                        raise StarvellError(
                            f"HTTP {resp.status} {url}: {message or body[:200]}",
                            status=resp.status, message=message or body[:200], code=code,
                        )

                    if as_text:
                        return await resp.text()
                    return await resp.json(content_type=None)

            except StarvellAuthError:
                raise
            except StarvellError as exc:
                if 400 <= exc.status < 500 and exc.status != 429:
                    raise
                last_exc = exc
                if attempt < retries - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < retries - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))

        raise StarvellError(f"не удалось выполнить {method} {url}: {last_exc}")

    # ---------- Next.js Data API ----------

    async def build_id(self, force: bool = False) -> str:
        if not force and self._build_id and time.time() - self._build_id_ts < BUILD_ID_TTL:
            return self._build_id
        html = await self._raw("GET", f"{BASE_URL}/", referer=BASE_URL, as_text=True)
        m = _BUILD_ID_RE.search(html)
        if not m:
            raise StarvellError("не удалось определить buildId Starvell")
        self._build_id = m.group(1)
        self._build_id_ts = time.time()
        logger.debug("buildId = %s", self._build_id)
        return self._build_id

    async def next_data(
        self,
        route: str,
        *,
        query: str = "",
        referer: Optional[str] = None,
        include_sid: bool = True,
        anonymous: bool = False,
    ) -> Dict[str, Any]:
        """route — например 'account/sells.json' или 'order/<uuid>.json'."""
        for force in (False, True):
            bid = await self.build_id(force=force)
            url = f"{BASE_URL}/_next/data/{bid}/{route}{query}"
            try:
                data = await self._raw(
                    "GET", url,
                    referer=referer or f"{BASE_URL}/",
                    include_sid=include_sid,
                    anonymous=anonymous,
                    retries=2,
                )
                if isinstance(data, dict):
                    return data.get("pageProps", data)
            except StarvellAuthError:
                raise
            except StarvellError as exc:
                # протухший buildId -> 404, пробуем обновить
                if "HTTP 404" in str(exc) and not force:
                    continue
                if force:
                    raise
        return {}

    async def page_props(self, path: str, anonymous: bool = False) -> Dict[str, Any]:
        """Фоллбэк: тянем HTML страницы и вынимаем __NEXT_DATA__."""
        html = await self._raw(
            "GET", f"{BASE_URL}/{path.lstrip('/')}",
            referer=BASE_URL, as_text=True, anonymous=anonymous,
        )
        m = _NEXT_DATA_RE.search(html)
        if not m:
            return {}
        try:
            return json.loads(m.group(1)).get("props", {}).get("pageProps", {})
        except Exception:
            return {}

    # ---------- профиль ----------

    async def get_user_info(self) -> Dict[str, Any]:
        props = await self.next_data("chat.json", include_sid=True)
        user = props.get("user") or {}
        if not user:
            props = await self.page_props("chat")
            user = props.get("user") or {}
        if user.get("id"):
            self.my_user_id = int(user["id"])
            self.my_username = user.get("username")
        return user

    async def keep_alive(self) -> bool:
        try:
            await self.next_data("chat.json", include_sid=True)
            return True
        except Exception as exc:
            logger.debug("keep_alive: %s", exc)
            return False

    # ---------- чаты ----------

    async def get_chats(self) -> List[Dict[str, Any]]:
        props = await self.next_data("chat.json", include_sid=True)
        chats = props.get("chats")
        if chats is None:
            chats = (props.get("bff") or {}).get("chats") or []
        return chats if isinstance(chats, list) else []

    async def get_chat(self, chat_id: str) -> Dict[str, Any]:
        referer = f"{BASE_URL}/chat/{chat_id}"
        attempts = [
            (f"chat/{chat_id}.json", f"?chatId={chat_id}"),
            (f"chat/{chat_id}.json", f"?id={chat_id}"),
            (f"chat/{chat_id}.json", ""),
            (f"chats/{chat_id}.json", ""),
        ]
        for route, query in attempts:
            try:
                props = await self.next_data(route, query=query, referer=referer)
                if props.get("messages") or props.get("chat"):
                    return props
            except Exception as exc:
                logger.debug("get_chat %s: %s", route, exc)
        return {}

    async def get_messages(self, chat_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        props = await self.get_chat(chat_id)
        msgs = props.get("messages") or (props.get("chat") or {}).get("messages") or []
        if not isinstance(msgs, list):
            return []
        msgs.sort(key=_msg_sort_key)
        return msgs[-limit:]

    async def list_messages(self, chat_id: str, limit: int = 30) -> List[Dict[str, Any]]:
        """История чата, от старых к новым. Сообщение со страницы лота несёт
        сам лот в поле offer (и metadata.offerId)."""
        data = await self._raw(
            "POST", f"{API_URL}/messages/list-v2",
            json_body={"chatId": chat_id, "limit": int(limit)},
            referer=f"{BASE_URL}/chat/{chat_id}",
            include_sid=True,
        )
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        return sorted(items, key=_msg_sort_key)

    async def viewed_offer(self, buyer_id: Any) -> Optional[Dict[str, Any]]:
        """Лот, который покупатель сейчас смотрит (как «Покупатель смотрит» на сайте)."""
        if not self.my_user_id:
            await self.get_user_info()
        try:
            data = await self._raw(
                "GET", f"{API_URL}/viewed-offers?sellerId={self.my_user_id}&buyerId={int(buyer_id)}",
                referer=f"{BASE_URL}/chat", include_sid=True, retries=1,
            )
        except (StarvellError, ValueError, TypeError):
            return None
        offer = data.get("data") if isinstance(data, dict) else None
        return offer if isinstance(offer, dict) and offer.get("id") else None

    async def send_message(self, chat_id: str, content: str) -> Dict[str, Any]:
        return await self._raw(
            "POST", f"{API_URL}/messages/send",
            json_body={"chatId": chat_id, "content": content},
            referer=f"{BASE_URL}/chat/{chat_id}",
            include_sid=True,
        )

    async def find_chat_by_user_id(self, user_id: Any) -> Optional[str]:
        uid = str(user_id)
        for chat in await self.get_chats():
            for key in ("interlocutor", "user", "companion", "opponent"):
                obj = chat.get(key) or {}
                if isinstance(obj, dict) and str(obj.get("id")) == uid:
                    return chat.get("id")
            participants = chat.get("participants") or chat.get("users") or []
            if isinstance(participants, list):
                for p in participants:
                    if isinstance(p, dict) and str(p.get("id")) == uid:
                        return chat.get("id")
        return None

    async def send_message_to_user(self, user_id: Any, content: str) -> Optional[Dict[str, Any]]:
        chat_id = await self.find_chat_by_user_id(user_id)
        if not chat_id:
            return None
        return await self.send_message(chat_id, content)

    # ---------- заказы ----------

    async def get_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """ПРОДАЖИ продавца (свежие сверху).

        /api/orders/list игнорирует фильтры и отдаёт ПОКУПКИ, поэтому
        продажи берутся со страницы личного кабинета.
        """
        props = await self.next_data(
            "account/sells.json", referer=f"{BASE_URL}/account/sells", include_sid=True,
        )
        orders = props.get("orders")
        if not isinstance(orders, list):
            return []
        if status:
            orders = [o for o in orders if str(o.get("status")) == status]
        return orders

    async def get_purchases(self) -> List[Dict[str, Any]]:
        """Покупки текущего аккаунта (то, что отдаёт /api/orders/list)."""
        data = await self._raw(
            "POST", f"{API_URL}/orders/list",
            json_body={"filter": {}},
            referer=f"{BASE_URL}/account/orders",
            include_sid=True,
        )
        return data if isinstance(data, list) else []

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        return await self.next_data(
            f"order/{order_id}.json",
            query=f"?order_id={order_id}",
            referer=f"{BASE_URL}/order/{order_id}",
            include_sid=True,
        )

    async def refund_order(self, order_id: str) -> Dict[str, Any]:
        return await self._raw(
            "POST", f"{API_URL}/orders/refund",
            json_body={"orderId": order_id},
            referer=f"{BASE_URL}/order/{order_id}",
            include_sid=True,
        )

    async def confirm_order(self, order_id: str) -> Dict[str, Any]:
        return await self._raw(
            "POST", f"{API_URL}/orders/confirm",
            json_body={"orderId": order_id},
            referer=f"{BASE_URL}/order/{order_id}",
            include_sid=True,
        )

    # ---------- лоты ----------

    async def get_offer(self, offer_id: int) -> Dict[str, Any]:
        return await self.next_data(
            f"offers/{offer_id}.json",
            query=f"?offer_id={offer_id}",
            referer=f"{BASE_URL}/offers/{offer_id}",
            include_sid=True,
        )

    async def get_my_offers(self, category_id: int) -> List[Dict[str, Any]]:
        """Свои лоты в категории.

        Страница профиля /users/{id} закрыта антиботом (403), поэтому
        используется тот же внутренний метод, что и личный кабинет.
        """
        data = await self._raw(
            "POST", f"{API_URL}/offers/list-my",
            json_body={"categoryId": int(category_id)},
            referer=f"{BASE_URL}/account/sells",
            include_sid=True,
        )
        return data if isinstance(data, list) else []

    async def get_user_offers(self, user_id: int) -> List[Dict[str, Any]]:
        """Лоты пользователя по его профилю.

        Starvell часто отдаёт /users/{id} с 403 (антибот) — тогда пусто.
        Для своих лотов используйте get_my_offers().
        """
        try:
            props = await self.next_data(
                f"users/{user_id}.json",
                query=f"?user_id={user_id}",
                referer=f"{BASE_URL}/users/{user_id}",
                include_sid=True,
            )
        except StarvellError as exc:
            logger.info("профиль недоступен (%s)", str(exc)[:80])
            return []

        cats = (
            props.get("userProfileOffers")
            or (props.get("bff") or {}).get("userProfileOffers")
            or props.get("categoriesWithOffers")
            or []
        )
        out: List[Dict[str, Any]] = []
        for cat in cats:
            for offer in cat.get("offers", []) or []:
                descr = (offer.get("descriptions") or {}).get("rus", {})
                out.append({
                    "id": offer.get("id"),
                    "publicId": offer.get("publicId"),
                    "title": descr.get("briefDescription"),
                    "price": offer.get("price"),
                    "availability": offer.get("availability"),
                    "url": f"{BASE_URL}/offers/{offer.get('id')}",
                })
        return out

    async def bump_offers(self, game_id: int, category_ids: List[int]) -> Dict[str, Any]:
        if not self._sid:
            await self.get_user_info()
        return await self._raw(
            "POST", f"{API_URL}/offers/bump",
            json_body={"gameId": int(game_id), "categoryIds": [int(c) for c in category_ids]},
            referer=BASE_URL,
            include_sid=True,
        )

    async def create_offer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Создать лот. Тело собирается в lots/autocreate.build_offer_payload()."""
        if not self._sid:
            await self.get_user_info()
        return await self._raw(
            "POST", f"{API_URL}/offers/create",
            json_body=payload,
            referer=f"{BASE_URL}/offers/add/{payload.get('categoryId')}",
            include_sid=True,
            retries=1,
        )

    async def update_offer(self, offer_ref: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Полное обновление лота. offer_ref — publicId (предпочтительно) или id."""
        if not self._sid:
            await self.get_user_info()
        return await self._raw(
            "POST", f"{API_URL}/offers/{offer_ref}/update",
            json_body=payload,
            referer=f"{BASE_URL}/offers/edit/{offer_ref}",
            include_sid=True,
            retries=1,
        )

    async def partial_update_offer(self, offer: Dict[str, Any], **changes: Any) -> Any:
        """Цена / наличие / видимость — тот же запрос, что шлёт таблица лотов
        в личном кабинете: все четыре поля разом, изменённые поверх текущих.
        """
        if not self._sid:
            await self.get_user_info()
        ref = offer.get("publicId") or offer.get("id")
        price = changes.get("price", offer.get("price") or "0")
        body = {
            "availability": int(changes.get("availability", offer.get("availability") or 0)),
            "price": f"{float(str(price).replace(',', '.')):.2f}",
            "minOrderCurrencyAmount": (
                offer.get("minOrderCurrencyAmount")
                if float(offer.get("minOrderCurrencyAmount") or 0) > 0 else None
            ),
            "isActive": bool(changes.get("isActive", offer.get("isActive", True))),
        }
        return await self._raw(
            "POST", f"{API_URL}/offers/{ref}/partial-update",
            json_body=body,
            referer=f"{BASE_URL}/account/sells",
            include_sid=True,
            retries=1,
        )

    async def delete_offer(self, offer: Dict[str, Any]) -> Any:
        if not self._sid:
            await self.get_user_info()
        ref = offer.get("publicId") or offer.get("id")
        return await self._raw(
            "POST", f"{API_URL}/offers/{ref}/delete",
            json_body=None,
            referer=f"{BASE_URL}/account/sells",
            include_sid=True,
            retries=1,
        )

    async def set_offer_price(self, offer: Dict[str, Any], price_rub: float) -> Any:
        return await self.partial_update_offer(offer, price=price_rub)

    async def set_offer_availability(self, offer: Dict[str, Any], availability: int) -> Any:
        return await self.partial_update_offer(offer, availability=availability)

    async def set_offer_active(self, offer: Dict[str, Any], active: bool) -> Any:
        return await self.partial_update_offer(offer, isActive=active)

    async def get_category_limit(self, category_id: int) -> Optional[int]:
        """Сколько лотов можно держать в категории (maxLotCount). None — неизвестно."""
        cache = getattr(self, "_limits", None)
        if cache is None:
            cache = self._limits = {}
        hit = cache.get(int(category_id))
        if hit and time.time() - hit[1] < 86400:
            return hit[0]
        try:
            props = await self.next_data(
                f"offers/add/{int(category_id)}.json", query=f"?category_id={int(category_id)}",
                referer=f"{BASE_URL}/offers/add/{int(category_id)}", include_sid=True,
            )
        except StarvellError as exc:
            logger.debug("лимит категории %s: %s", category_id, exc)
            return hit[0] if hit else None
        limit = (props.get("category") or {}).get("maxLotCount")
        limit = int(limit) if limit else None
        cache[int(category_id)] = (limit, time.time())
        return limit

    async def get_all_my_offers(self, category_ids: List[int]) -> List[Dict[str, Any]]:
        """Свои лоты сразу в нескольких категориях."""
        seen: Dict[Any, Dict[str, Any]] = {}
        for cid in sorted({int(c) for c in category_ids if c}):
            try:
                for offer in await self.get_my_offers(cid):
                    seen[offer.get("id")] = offer
            except StarvellError as exc:
                logger.debug("лоты категории %s: %s", cid, exc)
        return list(seen.values())

    # ---------- публичный каталог (без авторизации) ----------

    async def public_games(self) -> List[Dict[str, Any]]:
        props = await self.page_props("", anonymous=True)
        games: List[Dict[str, Any]] = []
        for group in props.get("gamesByType") or []:
            for item in group.get("items") or []:
                games.append({**item, "type": group.get("type")})
        return games

    async def public_category(self, game_slug: str, category_slug: str) -> Dict[str, Any]:
        return await self.next_data(
            f"{game_slug}/{category_slug}.json",
            referer=f"{BASE_URL}/{game_slug}/{category_slug}",
            include_sid=False,
            anonymous=True,
        )


def _parse_error(body: str) -> tuple:
    """{"success":false,"message":"...","extra":{"code":"..."}} -> (message, code)."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    message = data.get("message") or data.get("error") or ""
    if isinstance(message, list):
        message = "; ".join(str(m) for m in message)
    extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
    return str(message), str(extra.get("code") or data.get("code") or "")


_HUMAN = {
    "briefDescription must be shorter": "заголовок длиннее 100 символов",
    "Некорректная цена": "некорректная цена",
    "Некорректное наличие": "некорректное наличие",
    "OFFERS_BUMP_COOLDOWN": "поднимать пока рано — у Starvell перерыв между поднятиями",
    "kyc": "нужна верификация (KYC) на Starvell",
    "maxLotCount": "достигнут лимит лотов в категории",
}


def human_error(exc: Exception) -> str:
    """Короткое понятное описание ошибки Starvell для панели."""
    if isinstance(exc, StarvellAuthError):
        return "cookie session устарел — обновите его в настройках"
    if isinstance(exc, StarvellError):
        text = f"{exc.code} {exc.message}"
        for needle, human in _HUMAN.items():
            if needle.lower() in text.lower():
                return human
        if exc.status == 0:
            return "нет связи со Starvell"
        if exc.status == 429:
            return "слишком много запросов, Starvell попросил подождать"
        return exc.message[:150] or f"ошибка HTTP {exc.status}"
    return str(exc)[:150]


def _msg_sort_key(message: Dict[str, Any]) -> str:
    for key in ("createdAt", "created_at", "timestamp", "sentAt", "updatedAt", "date", "id"):
        value = message.get(key)
        if value is not None:
            return str(value)
    return ""
