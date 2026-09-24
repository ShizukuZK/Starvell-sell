"""JSON-хранилище: настройки, маппинги лотов, активные аренды, служебное состояние."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import re
import threading
import time
import unicodedata
from typing import Any, Dict, List

# Папка с данными. В облаке укажите DATA_DIR=/data (постоянный том),
# иначе настройки и история пропадут при каждом перезапуске контейнера.
DATA_DIR = os.environ.get("DATA_DIR") or "storage"
_LOCK = threading.RLock()

_KEY_RE = re.compile(r"[^a-z0-9а-я ]+")
_ROMAN_TAIL = {" i": " 1", " ii": " 2", " iii": " 3", " iv": " 4", " v": " 5",
               " vi": " 6", " vii": " 7", " viii": " 8", " ix": " 9", " x": " 10"}


def mapping_id(key: str) -> str:
    """Короткий стабильный идентификатор привязки.

    Нужен для кнопок Telegram: callback_data ограничен 64 байтами,
    а названия игр бывают длиннее.
    """
    return hashlib.md5((key or "").encode("utf-8")).hexdigest()[:10]


def game_key(name: str) -> str:
    """Название игры -> устойчивый ключ привязки."""
    text = unicodedata.normalize("NFKD", (name or "").lower()).replace("ё", "е")
    text = text.replace("&", " and ")
    text = _KEY_RE.sub(" ", text)
    text = " ".join(text.split())
    for roman, digit in _ROMAN_TAIL.items():
        if text.endswith(roman):
            text = text[: -len(roman)] + digit
            break
    return text

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
MAPPINGS_FILE = os.path.join(DATA_DIR, "mappings.json")
RENTALS_FILE = os.path.join(DATA_DIR, "rentals.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
TEXTS_FILE = os.path.join(DATA_DIR, "texts.json")
CATALOG_FILE = os.path.join(DATA_DIR, "starvell_catalog.json")

DEFAULT_SETTINGS: Dict[str, Any] = {
    # --- подключения ---
    "starvell_session": "",        # cookie `session` с starvell.com
    "kosell_api_key": "",          # ключ из профиля kosell.store
    "tg_token": "",                # токен Telegram-бота панели
    "tg_admins": [],               # список Telegram user_id администраторов
    "proxy_url": "",               # socks5://... или http://... (опционально)
    # Прокси ТОЛЬКО для Telegram. Нужен, если провайдер блокирует
    # api.telegram.org (ошибка «Превышен таймаут семафора» при старте панели).
    # Форматы: socks5://user:pass@host:port или http://user:pass@host:port
    "tg_proxy": "",
    # Путь к CA-бандлу для Telegram, если вы за корпоративным прокси с
    # собственным корневым сертификатом. Пусто — системный набор certifi.
    # Проверка сертификата при этом остаётся включённой.
    "tg_ca_bundle": "",

    # --- режимы работы ---
    "enabled": True,
    # тестовый прогон: бот всё читает и разбирает, но не арендует,
    # не продлевает, не возвращает деньги и не пишет покупателям
    "dry_run": False,
    "currency": "RUB",             # валюта оплаты в KOSell: RUB или USD
    "tz_offset_hours": 3,
    "log_level": "INFO",

    # --- поллинг ---
    "orders_poll_interval": 20,    # сек, опрос новых заказов
    "chats_poll_interval": 8,      # сек, опрос новых сообщений
    "rentals_poll_interval": 60,   # сек, контроль сроков аренды

    # --- модель лота: количество штук = часы аренды ---
    "hours_per_unit": 1,           # сколько часов даёт одна единица товара
    "min_quantity": 3,             # минимальный заказ в штуках
    "auto_refund_below_min": True, # возвращать заказы меньше минимума
    "auto_map_by_name": True,      # сам находить товар KOSell по названию игры
    "game_title_patterns": [
        r"АРЕНДА\s*\[(?P<game>[^\]]+)\]",
        r"АРЕНДА[^✅]*✅\s*(?P<game>[^✅]+?)\s*✅",
        r"с игрой:\s*(?P<game>.+)",
    ],

    # --- поведение выдачи ---
    "auto_confirm_order": False,   # подтверждать заказ после успешной выдачи
    "auto_refund": True,           # возврат, если выдать не удалось
    "friend_minutes": 10,          # окно режима «для друга»
    "greeting_enabled": True,      # приветствие с инструкцией на первое сообщение
    "greeting_cooldown_hours": 24, # не чаще раза в N часов одному покупателю
    "notify_rental_end": True,
    "notify_sales": True,
    "code_cooldown_seconds": 20,   # антиспам на !код

    # --- обслуживание лотов ---
    "stock_sync_enabled": True,    # сверять наличие лотов с KOSell
    "stock_sync_minutes": 15,
    "auto_hide_no_stock": True,    # скрывать лот, когда в KOSell 0 аккаунтов
    "auto_bump_enabled": False,    # автоподнятие лотов
    "auto_bump_hours": 4,

    # --- спрос (Steam) и умный отбор ---
    "demand_enabled": True,        # собирать онлайн и отзывы игр из Steam
    "demand_hot_players": 5000,    # пиковый онлайн, с которого игра — 🔥 хит
    "demand_popular_players": 500, # ... и ⭐ популярная
    "price_floor_hot": 5.0,        # минимальная цена за штуку для хитов
    "price_floor_popular": 4.0,    # ... для популярных (для обычных — lots_min_price_rub)
    "lots_min_stock": 1,           # выставлять игру, только если в KOSell столько свободных
    "lots_max_cost_rub": 10.0,     # не выставлять игры дороже этого в час (KOSell)
    "rotate_after_days": 7,        # лот считается слабым не раньше чем через N дней
    "rotate_max_views": 5,         # ... если у него не больше N просмотров и нет продаж
    "auto_reprice_enabled": False, # пересчитывать цены по спросу автоматически
    "auto_reprice_hours": 24,

    # --- уведомления ---
    "balance_alert_rub": 50.0,     # предупредить, когда баланс KOSell ниже

    # --- автосоздание лотов ---
    "lots_markup_percent": 60.0,   # наценка к себестоимости KOSell
    # цена считается ЗА ОДНУ ШТУКУ (= hours_per_unit часов), поэтому порог низкий
    "lots_min_price_rub": 3.0,
    "lots_round_to": 0.01,
    "lots_commission_percent": 0.0,  # комиссия площадки, закладывается в цену
    "lots_title_template": "",       # свой шаблон заголовка; пусто — встроенный
    "lots_description_template": "", # свой шаблон описания; пусто — встроенный
    "lots_batch_pause": 1.5,         # пауза между созданием лотов, сек

    # --- SMM (OptSMM) ---
    "optsmm_api_key": "",            # ключ со страницы optsmm.ru/developer
    "smm_enabled": True,             # обрабатывать SMM-лоты (нужны ключ и правила)
    "smm_auto_refund": True,         # возврат, если OptSMM отменил заказ
    "smm_remind_minutes": 60,        # напомнить покупателю прислать ссылку
    "smm_link_timeout_hours": 24,    # сообщить админу, если ссылки всё нет
    "smm_usd_rate": 90.0,            # если баланс OptSMM в долларах
    "smm_poll_seconds": 45,          # как часто проверять статусы OptSMM

    # --- обновления ---
    "github_token": "",              # только для приватного репозитория (права Contents: Read)
    "github_repo": "",               # владелец/репозиторий для проверки новых версий
    "update_check_enabled": True,
}

DEFAULT_STATE: Dict[str, Any] = {
    "handled_orders": [],     # id заказов, уже обработанных
    "last_message_ids": {},   # chat_id -> последний обработанный message id
    "friend_mode": {},        # buyer_id -> unix ts, до которого активен режим
    "hidden_offers": {},      # offer_id -> причина
    "my_user_id": None,
    "last_code_request": {},  # buyer_id -> unix ts
}


# Переменные окружения -> настройки. Так секреты задаются в панели хостинга
# и никогда не попадают в git.
ENV_SETTINGS: Dict[str, tuple] = {
    "STARVELL_SESSION": ("starvell_session", "str"),
    "KOSELL_API_KEY": ("kosell_api_key", "str"),
    "OPTSMM_API_KEY": ("optsmm_api_key", "str"),
    "TG_TOKEN": ("tg_token", "str"),
    "TG_ADMINS": ("tg_admins", "ids"),
    "TG_PROXY": ("tg_proxy", "str"),
    "PROXY_URL": ("proxy_url", "str"),
    "DRY_RUN": ("dry_run", "bool"),
    "GITHUB_REPO": ("github_repo", "str"),
    "GITHUB_TOKEN": ("github_token", "str"),
}


def _env_value(raw: str, kind: str) -> Any:
    raw = raw.strip()
    if kind == "ids":
        return [int(x) for x in raw.replace(",", " ").split() if x.strip().lstrip("-").isdigit()]
    if kind == "bool":
        return raw.lower() in ("1", "true", "yes", "on", "да")
    return raw


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _atomic_write(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    shutil.move(tmp, path)


def _read(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return json.loads(json.dumps(default))
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        try:
            shutil.copy(path, f"{path}.broken-{int(time.time())}")
        except Exception:
            pass
        return json.loads(json.dumps(default))


class Store:
    """Единая точка доступа к состоянию бота."""

    def __init__(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        self.settings: Dict[str, Any] = {}
        self.mappings: List[Dict[str, Any]] = []
        self.rentals: Dict[str, List[Dict[str, Any]]] = {}
        self.state: Dict[str, Any] = {}
        self.texts: Dict[str, str] = {}
        self.load()

    # ---------- загрузка / сохранение ----------

    def load(self) -> None:
        from core.texts import DEFAULT_TEXTS

        with _LOCK:
            self.settings = {**DEFAULT_SETTINGS, **_read(SETTINGS_FILE, DEFAULT_SETTINGS)}
            self.mappings = _read(MAPPINGS_FILE, [])
            for m in self.mappings:
                if m.get("key") and not m.get("id"):
                    m["id"] = mapping_id(m["key"])
            self.rentals = _read(RENTALS_FILE, {})
            self.state = {**DEFAULT_STATE, **_read(STATE_FILE, DEFAULT_STATE)}
            self._apply_env()
            saved = _read(TEXTS_FILE, {})
            from core.texts import LEGACY_DEFAULTS
            saved = {k: v for k, v in saved.items() if v not in LEGACY_DEFAULTS.get(k, ())}
            self.texts = {**DEFAULT_TEXTS, **saved}

    def _apply_env(self) -> None:
        """Значения из переменных окружения.

        Переменная применяется, когда её задали впервые или изменили. Если потом
        значение поменяли в панели (например, обновили протухший cookie), при
        перезапуске оно не откатится к старому из переменной.
        """
        seen = self.state.setdefault("env_seen", {})
        changed = False
        for env, (key, kind) in ENV_SETTINGS.items():
            raw = os.environ.get(env)
            if raw is None or not raw.strip():
                continue
            mark = _fingerprint(raw.strip())
            if seen.get(env) == mark:
                continue
            self.settings[key] = _env_value(raw, kind)
            seen[env] = mark
            changed = True
        if changed:
            _atomic_write(SETTINGS_FILE, self.settings)
            _atomic_write(STATE_FILE, self.state)

    def save_settings(self) -> None:
        with _LOCK:
            _atomic_write(SETTINGS_FILE, self.settings)

    def save_mappings(self) -> None:
        with _LOCK:
            _atomic_write(MAPPINGS_FILE, self.mappings)

    def save_rentals(self) -> None:
        with _LOCK:
            _atomic_write(RENTALS_FILE, self.rentals)

    def save_state(self) -> None:
        with _LOCK:
            # не даём спискам расти бесконечно
            self.state["handled_orders"] = self.state.get("handled_orders", [])[-3000:]
            _atomic_write(STATE_FILE, self.state)

    def save_texts(self) -> None:
        """Сохраняются только изменённые вами тексты.

        Стандартные в файл не пишутся — иначе улучшения стандартных текстов
        в новых версиях бота никогда бы до вас не дошли.
        """
        from core.texts import DEFAULT_TEXTS
        custom = {k: v for k, v in self.texts.items() if v != DEFAULT_TEXTS.get(k)}
        with _LOCK:
            _atomic_write(TEXTS_FILE, custom)

    def save_all(self) -> None:
        self.save_settings()
        self.save_mappings()
        self.save_rentals()
        self.save_state()
        self.save_texts()

    # ---------- настройки ----------

    def get(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, DEFAULT_SETTINGS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self.settings[key] = value
        self.save_settings()

    def is_admin(self, user_id: int) -> bool:
        admins = self.get("tg_admins") or []
        return int(user_id) in [int(a) for a in admins]

    # ---------- привязки: игра из названия лота -> товар KOSell ----------

    def find_mapping(self, game: Any) -> Dict[str, Any] | None:
        """Привязка по названию игры. Ключ нормализован (регистр, символы)."""
        key = game_key(str(game))
        if not key:
            return None
        for m in self.mappings:
            if m.get("key") == key:
                return m
        for m in self.mappings:           # название, как оно стоит в заголовке лота
            if m.get("title_key") == key:
                return m
        for m in self.mappings:           # совместимость со старым форматом
            if str(m.get("offer_id")) == str(game):
                return m
        return None

    def find_mapping_by_offer(self, offer_id: Any = None, public_id: Any = None) -> Dict[str, Any] | None:
        for m in self.mappings:
            if offer_id is not None and str(m.get("offer_id")) == str(offer_id):
                return m
            if public_id and m.get("offer_public_id") == public_id:
                return m
        return None

    def find_mapping_by_id(self, mid: str) -> Dict[str, Any] | None:
        for m in self.mappings:
            if m.get("id") == mid or mapping_id(m.get("key", "")) == mid:
                return m
        return None

    def upsert_mapping(self, mapping: Dict[str, Any]) -> None:
        mapping = dict(mapping)
        if not mapping.get("key"):
            mapping["key"] = game_key(mapping.get("game") or mapping.get("product_name") or "")
        mapping["id"] = mapping_id(mapping["key"])
        existing = self.find_mapping(mapping["key"])
        if existing:
            existing.update(mapping)
        else:
            self.mappings.append(mapping)
        self.save_mappings()

    def delete_mapping(self, game: Any) -> bool:
        key = game_key(str(game))
        before = len(self.mappings)
        self.mappings = [
            m for m in self.mappings
            if m.get("key") != key and str(m.get("offer_id")) != str(game)
        ]
        if len(self.mappings) != before:
            self.save_mappings()
            return True
        return False

    # ---------- аренды ----------

    def active_rentals(self, buyer_key: str) -> List[Dict[str, Any]]:
        now = time.time()
        items = [r for r in self.rentals.get(str(buyer_key), []) if r.get("expires_ts", 0) > now]
        return items

    def add_rental(self, buyer_key: str, record: Dict[str, Any]) -> None:
        self.rentals.setdefault(str(buyer_key), []).append(record)
        self.save_rentals()

    def all_rentals(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for buyer_key, items in self.rentals.items():
            for r in items:
                out.append({**r, "buyer_key": buyer_key})
        return out

    def cleanup_rentals(self, keep_seconds: int = 86400) -> int:
        now = time.time()
        removed = 0
        for buyer_key in list(self.rentals.keys()):
            kept = []
            for r in self.rentals[buyer_key]:
                if r.get("expires_ts", 0) + keep_seconds > now:
                    kept.append(r)
                else:
                    removed += 1
            if kept:
                self.rentals[buyer_key] = kept
            else:
                del self.rentals[buyer_key]
        if removed:
            self.save_rentals()
        return removed

    # ---------- служебное состояние ----------

    def is_handled(self, order_id: str) -> bool:
        return str(order_id) in self.state.get("handled_orders", [])

    def mark_handled(self, order_id: str) -> None:
        lst = self.state.setdefault("handled_orders", [])
        if str(order_id) not in lst:
            lst.append(str(order_id))
            self.save_state()

    def friend_active(self, buyer_key: str) -> bool:
        return float(self.state.get("friend_mode", {}).get(str(buyer_key), 0)) > time.time()

    def friend_set(self, buyer_key: str, minutes: int) -> None:
        self.state.setdefault("friend_mode", {})[str(buyer_key)] = time.time() + minutes * 60
        self.save_state()

    def friend_clear(self, buyer_key: str) -> None:
        self.state.get("friend_mode", {}).pop(str(buyer_key), None)
        self.save_state()
