"""Telegram-панель управления ботом (aiogram 3).

Устройство:
  * все нажатия кнопок идут через один маршрутизатор (_routes);
  * экраны рисуются чистыми функциями из views.py;
  * долгие операции (создание лотов, переоценка…) — фоновые задачи с
    полосой прогресса и кнопкой «Остановить»;
  * нажатие подтверждается мгновенно, до любой сетевой работы, — иначе
    на медленной связи Telegram отвечает «query is too old».
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BufferedInputFile, CallbackQuery, ErrorEvent, Message

from core import backup
from core.engine import Engine
from core.kosell import KosellAPI
from core.log import get_logger
from core.maintenance import Maintenance
from core.poller import Poller
from core.starvell import StarvellAPI, StarvellError, human_error
from core.stats import SalesLog, day_start
from core.storage import DEFAULT_SETTINGS, Store
from core.texts import DEFAULT_TEXTS
from core.textfit import starvell_len
from lots import autocreate, catalog
from lots.sync import OffersCache, match_offer, sync_mappings
from tgpanel import views
from tgpanel.schema import FIELDS
from tgpanel.smm_panel import SmmPanelMixin
from version import VERSION

logger = get_logger("panel")

Screen = views.Screen


class ConflictWatch(logging.Filter):
    """Ловит «Conflict: terminated by other getUpdates request».

    Это значит, что с тем же токеном работает ещё одна копия бота — например,
    на ПК и на хостинге одновременно. Тогда один заказ могут выдать дважды.
    """

    def __init__(self) -> None:
        super().__init__()
        self.last = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "Conflict" in msg and "getUpdates" in msg and time.time() - self.last > 600:
            self.last = time.time()
            logger.error("‼️ С этим же токеном Telegram работает ДРУГАЯ копия бота "
                         "(ПК и хостинг одновременно?). Оставьте одну — иначе заказы "
                         "могут выдаваться дважды.")
        return True


class Ask(StatesGroup):
    value = State()


@dataclass
class Ctx:
    call: Optional[CallbackQuery]
    state: Optional[FSMContext]
    message: Optional[Message]


def _mask_proxy(url: str) -> str:
    if not url or "@" not in url:
        return url or ""
    scheme, _, rest = url.partition("://")
    return f"{scheme}://***@{rest.split('@', 1)[1]}"


def _looks_blocked(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in (
        "cannot connect to host api.telegram.org", "семафора", "semaphore",
        "timed out", "timeout", "network is unreachable", "connection refused",
        "clientconnectorerror"))


# ================================================================ middleware

class AdminOnly(BaseMiddleware):
    """Пускает только администраторов. Первый написавший становится админом."""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None:
            return None
        admins = self.store.get("tg_admins") or []
        if not admins:
            self.store.set("tg_admins", [int(user.id)])
            logger.info("первый администратор: %s", user.id)
        elif not self.store.is_admin(user.id):
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("Нет доступа", show_alert=True)
                except Exception:
                    pass
            elif isinstance(event, Message) and event.text and event.text.startswith("/id"):
                await event.answer(f"Ваш Telegram ID: <code>{user.id}</code>", parse_mode="HTML")
            return None
        return await handler(event, data)


class InstantAck(BaseMiddleware):
    """Подтверждает нажатие сразу — кнопка перестаёт «крутиться» мгновенно."""

    async def __call__(self, handler, event: CallbackQuery, data):
        try:
            await event.answer()
        except Exception:
            pass    # «query is too old» и сетевые ошибки тут не важны
        return await handler(event, data)


# ================================================================ панель

class Panel(SmmPanelMixin):
    def __init__(
        self, store: Store, sv: StarvellAPI, ks: KosellAPI, poller: Poller,
        engine: Engine, maintenance: Maintenance, offers: OffersCache, stats: SalesLog,
        demand: Any = None, updates: Any = None, smm: Any = None,
    ) -> None:
        self.smm = smm
        self._smm_found: List[Dict[str, Any]] = []
        self.demand = demand
        self.updates = updates
        # ставит main.py: мягкая остановка и запуск с новым кодом
        self.request_restart: Optional[Callable[[], None]] = None
        self._rotation: List[Any] = []
        self.store = store
        self.sv = sv
        self.ks = ks
        self.poller = poller
        self.engine = engine
        self.maint = maintenance
        self.offers = offers
        self.stats = stats
        self.bot: Optional[Bot] = None
        self.dp = Dispatcher(storage=MemoryStorage())
        self.online = False
        self._stopping = False
        self._plan: List[Dict[str, Any]] = []
        self._price_changes: List[Dict[str, Any]] = []
        self._job: Optional[asyncio.Task] = None
        self._job_cancel: Optional[asyncio.Event] = None
        self._job_title = ""
        self._routes: Dict[str, Callable[[Ctx, str], Awaitable[Optional[Screen]]]] = {}
        self._restore: Optional[Dict[str, Any]] = None
        logging.getLogger("aiogram.dispatcher").addFilter(ConflictWatch())
        self._register()

    # ------------------------------------------------------------ запуск

    async def run(self) -> None:
        token = (self.store.get("tg_token") or "").strip()
        if not token:
            logger.warning("tg_token не задан — панель не запущена, автовыдача работает")
            return
        delay = 15
        while not self._stopping:
            try:
                await self._run_once(token)
                return
            except asyncio.CancelledError:
                return
            except TelegramUnauthorizedError:
                self.online = False
                logger.error("Telegram не принял токен бота. Проверьте поле \"tg_token\" в "
                             "storage/settings.json (токен выдаёт @BotFather) и перезапустите. "
                             "Автовыдача заказов работает и без панели.")
                await self._close_bot()
                return
            except Exception as exc:
                self.online = False
                logger.error("панель: %s: %s", type(exc).__name__, str(exc)[:200])
                if _looks_blocked(exc) and not (self.store.get("tg_proxy") or "").strip():
                    logger.error(
                        "Похоже, провайдер режет api.telegram.org. Укажите прокси в "
                        "storage/settings.json, поле \"tg_proxy\" "
                        "(socks5://логин:пароль@хост:1080), и перезапустите бота. "
                        "Автовыдача заказов работает и без панели.")
                await self._close_bot()
                if self._stopping:
                    return
                logger.info("панель: новая попытка через %d с", delay)
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
                delay = min(delay * 2, 60)   # быстро ловим момент, когда связь вернулась

    def _make_session(self) -> AiohttpSession:
        proxy = (self.store.get("tg_proxy") or "").strip()
        try:
            session = AiohttpSession(proxy=proxy) if proxy else AiohttpSession()
        except RuntimeError as exc:
            logger.error("для SOCKS-прокси нужен aiohttp-socks: pip install aiohttp-socks (%s)", exc)
            session, proxy = AiohttpSession(), ""
        ca = (self.store.get("tg_ca_bundle") or "").strip() or os.getenv("SSL_CERT_FILE", "")
        if ca and os.path.exists(ca):
            session._connector_init["ssl"] = ssl.create_default_context(cafile=ca)
        if proxy:
            logger.info("Telegram: через прокси %s", _mask_proxy(proxy))
        return session

    async def _run_once(self, token: str) -> None:
        self.bot = Bot(token=token, session=self._make_session())
        me = await self.bot.get_me()
        self.online = True
        logger.info("панель запущена: @%s", me.username)
        try:
            await self.bot.set_my_commands([
                BotCommand(command="menu", description="Главное меню"),
                BotCommand(command="lots", description="Мои лоты"),
                BotCommand(command="new", description="Создать лоты"),
                BotCommand(command="stats", description="Статистика"),
                BotCommand(command="balance", description="Баланс KOSell"),
                BotCommand(command="smm", description="SMM · OptSMM"),
                BotCommand(command="backup", description="Копия данных для переноса"),
                BotCommand(command="help", description="Как это работает"),
            ])
        except Exception as exc:
            logger.debug("команды меню: %s", exc)
        await self.notify(
            "🟢 <b>Бот запущен</b>\n"
            f"Версия {VERSION} · Starvell: {views.e(self.sv.my_username or '—')} · "
            f"привязок: {len(self.store.mappings)}"
            + ("\n🧪 Тестовый режим — аккаунты не выдаются." if self.store.get("dry_run") else ""),
            menu=True,
        )
        await self.dp.start_polling(self.bot, handle_signals=False, polling_timeout=25)

    async def _close_bot(self) -> None:
        if self.bot:
            try:
                await self.bot.session.close()
            except Exception:
                pass
            self.bot = None

    async def stop(self) -> None:
        self._stopping = True
        if self._job_cancel:
            self._job_cancel.set()
        try:
            await self.dp.stop_polling()
        except Exception:
            pass
        await self._close_bot()

    async def notify(self, text: str, menu: bool = False) -> None:
        if not self.bot or not self.online:
            return
        markup = views.kb([views.b("📋 Меню", "home")]) if menu else None
        for admin_id in self.store.get("tg_admins") or []:
            try:
                await self.bot.send_message(int(admin_id), text, parse_mode="HTML",
                                            reply_markup=markup, disable_web_page_preview=True)
            except TelegramBadRequest:
                try:   # текст с неэкранированными символами — шлём без разметки
                    await self.bot.send_message(int(admin_id), text, reply_markup=markup,
                                                disable_web_page_preview=True)
                except Exception as exc:
                    logger.debug("уведомление %s: %s", admin_id, exc)
            except Exception as exc:
                logger.debug("уведомление %s: %s", admin_id, exc)

    # ------------------------------------------------------------ вывод

    async def _show(self, target: Any, screen: Screen) -> Optional[Message]:
        text, markup = screen
        if len(text) > 4000:
            text = text[:3990] + "…"
        msg = target.message if isinstance(target, CallbackQuery) else target
        if msg is None:
            return None
        if isinstance(target, CallbackQuery):
            try:
                return await msg.edit_text(text, reply_markup=markup, parse_mode="HTML",
                                           disable_web_page_preview=True)
            except TelegramBadRequest as exc:
                if "not modified" in str(exc).lower():
                    return msg
                logger.debug("edit не удался (%s) — отправляю новое", exc)
        return await msg.answer(text, reply_markup=markup, parse_mode="HTML",
                                disable_web_page_preview=True)

    async def _edit(self, msg: Message, screen: Screen) -> None:
        text, markup = screen
        try:
            await msg.edit_text(text[:4000], reply_markup=markup, parse_mode="HTML",
                                disable_web_page_preview=True)
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc).lower():
                logger.debug("прогресс: %s", exc)
        except TelegramNetworkError:
            pass

    @staticmethod
    async def _soft(coro: Awaitable[Any], timeout: float = 8.0) -> Any:
        try:
            return await asyncio.wait_for(coro, timeout)
        except Exception as exc:
            logger.debug("мягкий вызов: %s", exc)
            return None

    # ------------------------------------------------------------ регистрация

    def _register(self) -> None:
        dp = self.dp
        admin = AdminOnly(self.store)
        dp.message.outer_middleware(admin)
        dp.callback_query.outer_middleware(admin)
        dp.callback_query.middleware(InstantAck())

        r = self._routes
        r.update({
            "home": self.r_home, "tog_home": self.r_tog_home, "noop": self.r_noop,
            "lots": self.r_lots, "lots_r": self.r_lots_refresh, "lot": self.r_lot,
            "lotact": self.r_lot_action, "lotask": self.r_lot_ask,
            "lots_stock": self.r_lots_stock, "lots_price": self.r_lots_price,
            "lots_price_go": self.r_lots_price_go, "lots_bump": self.r_lots_bump,
            "lots_link": self.r_lots_link,
            "new": self.r_new, "new_n": self.r_new_n, "new_go": self.r_new_go,
            "new_ex": self.r_new_example, "cat_scan": self.r_catalog_scan,
            "job_stop": self.r_job_stop,
            "rent": self.r_rentals, "rentc": self.r_rental_code,
            "stats": self.r_stats,
            "maps": self.r_maps, "map": self.r_map, "mapt": self.r_map_toggle,
            "mapd": self.r_map_delete, "mapp": self.r_map_product, "map_add": self.r_map_add,
            "tog_maps": self.r_tog_maps,
            "set": self.r_settings, "setg": self.r_settings_group, "tog": self.r_toggle,
            "ask": self.r_ask,
            "txt": self.r_texts, "txte": self.r_text_edit, "txtr": self.r_text_reset,
            "tpl": self.r_template, "tplr": self.r_template_reset,
            "help": self.r_help, "cancel": self.r_cancel, "ver": self.r_version,
            "upd": self.r_update,
            "bak": self.r_backup,
            "dem": self.r_demand_refresh, "rot": self.r_rotation, "rot_go": self.r_rotation_go,
        })
        if self.smm is not None:
            r.update(self._smm_routes())
            self._register_smm_commands(dp, Ctx)

        @dp.message(Command("start", "menu"))
        async def c_menu(message: Message, state: FSMContext) -> None:
            await state.clear()
            await self._show(message, await self.r_home(Ctx(None, state, message), ""))

        @dp.message(Command("lots"))
        async def c_lots(message: Message, state: FSMContext) -> None:
            await state.clear()
            await self._show(message, await self.r_lots(Ctx(None, state, message), "0"))

        @dp.message(Command("new"))
        async def c_new(message: Message, state: FSMContext) -> None:
            await state.clear()
            wait = await message.answer("⏳ Считаю, что можно создать…")
            await self._edit(wait, await self.r_new(Ctx(None, state, wait), ""))

        @dp.message(Command("stats"))
        async def c_stats(message: Message, state: FSMContext) -> None:
            await state.clear()
            await self._show(message, await self.r_stats(Ctx(None, state, message), "day"))

        @dp.message(Command("balance"))
        async def c_balance(message: Message) -> None:
            data = await self._soft(self.maint.get_balance(force=True))
            if not data:
                await message.answer("❌ KOSell не ответил. Проверьте ключ в ⚙️ → 🔌 Подключения.")
                return
            await message.answer(
                f"💰 <b>Баланс KOSell</b>\n\n{views.money(data.get('balance_rub'))}"
                f" · ${float(data.get('balance_usd') or 0):.2f}\n"
                f"Аккаунт: {views.e(data.get('username'))}",
                parse_mode="HTML")

        @dp.message(Command("help"))
        async def c_help(message: Message, state: FSMContext) -> None:
            await state.clear()
            await self._show(message, views.help_screen())

        @dp.message(Command("id"))
        async def c_id(message: Message) -> None:
            await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>",
                                 parse_mode="HTML")

        @dp.message(Command("backup"))
        async def c_backup(message: Message) -> None:
            await self._send_backup(message.chat.id)

        @dp.message(F.document)
        async def on_document(message: Message, state: FSMContext) -> None:
            await state.clear()
            await self._show(message, await self._receive_backup(message))

        @dp.message(StateFilter(Ask.value))
        async def on_value(message: Message, state: FSMContext) -> None:
            await self._on_value(message, state)

        @dp.message()
        async def on_other(message: Message, state: FSMContext) -> None:
            await self._show(message, await self.r_home(Ctx(None, state, message), ""))

        @dp.callback_query()
        async def on_callback(call: CallbackQuery, state: FSMContext) -> None:
            prefix, _, arg = (call.data or "").partition(":")
            route = self._routes.get(prefix)
            if route is None:
                return
            if prefix not in ("cancel", "noop", "job_stop"):
                await state.clear()
            try:
                screen = await route(Ctx(call, state, call.message), arg)
            except StarvellError as exc:
                screen = (f"⚠️ <b>Starvell ответил ошибкой</b>\n\n{views.e(human_error(exc))}",
                          views.back("home", "◀️ Меню"))
            if screen:
                await self._show(call, screen)

        @dp.errors()
        async def on_error(event: ErrorEvent) -> bool:
            exc = event.exception
            text = str(exc).lower()
            if isinstance(exc, TelegramBadRequest) and (
                    "not modified" in text or "query is too old" in text):
                return True
            logger.warning("ошибка в панели: %s: %s", type(exc).__name__, str(exc)[:200])
            return True

    # ================================================================ маршруты

    async def r_noop(self, ctx: Ctx, arg: str) -> None:
        return None

    # ------------------------------------------------------------ перенос данных

    async def _send_backup(self, chat_id: int) -> None:
        self.store.save_all()
        blob = await asyncio.to_thread(backup.make_backup)
        await self.bot.send_document(
            chat_id, BufferedInputFile(blob, filename=backup.backup_name()),
            caption="💾 Резервная копия данных бота (без ключей и токенов).\n"
                    "Перешлите этот файл новому боту, чтобы перенести данные.")

    async def _receive_backup(self, message: Message) -> Screen:
        doc = message.document
        name = doc.file_name or "file"
        if not name.lower().endswith(".zip"):
            return views.backup_screen("⚠️ Это не резервная копия — нужен .zip от «📤 Скачать копию».")
        if (doc.file_size or 0) > backup.MAX_SIZE:
            return views.backup_screen("⚠️ Файл слишком большой.")
        buf = await self.bot.download(doc)
        blob = buf.read() if buf else b""
        created = 0.0
        try:
            import io
            import zipfile
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                created = float(json.loads(z.read("backup.json")).get("created") or 0)
        except Exception:
            return views.backup_screen("⚠️ Это не резервная копия бота.")
        self._restore = {"name": name, "blob": blob, "created": created}
        return views.restore_confirm(self._restore)

    async def r_backup(self, ctx: Ctx, arg: str) -> Screen:
        if arg == "get":
            chat_id = ctx.message.chat.id if ctx.message else None
            if chat_id is None or not self.bot:
                return views.backup_screen("⚠️ Не удалось определить чат.")
            await self._send_backup(chat_id)
            return views.backup_screen("✅ Файл отправлен выше.")
        if arg == "drop":
            self._restore = None
            return views.backup_screen("Загрузка отменена.")
        if arg == "apply":
            pending, self._restore = self._restore, None
            if not pending:
                return views.backup_screen("⚠️ Файл устарел — пришлите его ещё раз.")
            try:
                restored = await asyncio.to_thread(backup.restore_backup, pending["blob"], self.store)
            except ValueError as exc:
                return views.backup_screen(f"⚠️ {views.e(exc)}")
            for part in (self.stats, self.demand):
                loader = getattr(part, "_load", None)
                if loader:
                    try:
                        loader()
                    except Exception as exc:
                        logger.debug("перечитывание после восстановления: %s", exc)
            self.offers.invalidate()
            logger.info("данные восстановлены из %s: %s", pending["name"], ", ".join(restored))
            return views.backup_screen(
                f"✅ Загружено: {len(restored)} файлов. Привязки, аренды и настройки "
                "уже действуют.")
        return views.backup_screen()

    async def r_help(self, ctx: Ctx, arg: str) -> Screen:
        return views.help_screen()

    async def r_version(self, ctx: Ctx, arg: str, note: str = "") -> Screen:
        from core import selfupdate
        from core.updates import is_newer
        from version import VERSION
        up = self.updates
        if up is not None and (arg == "check" or not up.checked_ts):
            await self._soft(up.check(force=True), 25)
        latest = up.latest if up else None
        return views.version_screen({
            "current": VERSION,
            "repo": up.repo if up else "",
            "latest": latest,
            "newer": bool(latest and is_newer(latest.get("version", ""))),
            "error": up.error if up else "",
            "checked": up.checked_ts if up else 0,
            "self_update": selfupdate.configured() and self.request_restart is not None,
            "note": note,
        })

    async def r_update(self, ctx: Ctx, arg: str) -> Screen:
        """Кнопка «⬇️ Обновить сейчас»: git pull и перезапуск на хостинге."""
        from core import selfupdate
        if not selfupdate.configured() or self.request_restart is None:
            return await self.r_version(ctx, "", "Обновление из панели работает только на "
                                                 "хостинге с заданным GIT REPO ADDRESS.")
        result = await asyncio.to_thread(selfupdate.update)
        logger.info("панель: обновление — %s", result.message)
        if not result.ok:
            return await self.r_version(ctx, "", f"❌ {result.message}")
        if not result.changed:
            return await self.r_version(ctx, "", f"✅ Код уже свежий ({result.new[:7]}).")
        asyncio.get_running_loop().call_later(1.5, self.request_restart)
        return views.restarting_screen(result.message)

    async def r_cancel(self, ctx: Ctx, arg: str) -> Screen:
        data = await ctx.state.get_data() if ctx.state else {}
        if ctx.state:
            await ctx.state.clear()
        return await self._screen(data.get("back") or "home", ctx)

    async def _screen(self, target: str, ctx: Ctx) -> Screen:
        prefix, _, arg = target.partition(":")
        route = self._routes.get(prefix, self.r_home)
        return await route(ctx, arg) or await self.r_home(ctx, "")

    # ------------------------------------------------------------ главная

    async def r_home(self, ctx: Ctx, arg: str) -> Screen:
        balance, offers = await asyncio.gather(
            self._soft(self.maint.get_balance(), 6),
            self._soft(self.offers.get(self.store, self.sv), 8),
        )
        st = self.poller.status()
        today = self.stats.summary(since=day_start(int(self.store.get("tz_offset_hours", 3))))
        return views.home({
            "enabled": self.store.get("enabled", True),
            "dry_run": self.store.get("dry_run", False),
            "username": self.sv.my_username,
            "uptime": st["uptime_seconds"],
            "balance": float(balance["balance_rub"]) if balance else None,
            "balance_alert": float(self.store.get("balance_alert_rub", 0) or 0),
            "offers_total": len(offers) if offers is not None else None,
            "offers_active": sum(1 for o in offers or [] if views.offer_status(o)[0] == "✅"),
            "rentals": st["active_rentals"],
            "smm": self.smm.summary()["active"] if self.smm is not None and self.smm.rules else None,
            "today": today,
            "last_error": st.get("last_error"),
            "version": VERSION,
            "update": self.updates.available if self.updates else None,
        })

    async def r_tog_home(self, ctx: Ctx, key: str) -> Screen:
        if key in ("dry_run", "enabled"):
            self.store.set(key, not bool(self.store.get(key)))
            logger.info("панель: %s = %s", key, self.store.get(key))
        return await self.r_home(ctx, "")

    # ------------------------------------------------------------ спрос и места

    def _tier_info(self, product: Optional[Dict[str, Any]]):
        if not product or self.demand is None or not self.demand.ready:
            return None, {}
        return autocreate.product_tier(self.store, self.demand, product), self.demand.info(product)

    async def _slots(self, offers: List[Dict[str, Any]]):
        """(лимит основного раздела, свободно в нём, {категория: свободно})."""
        from collections import Counter
        from lots.autocreate import DEFAULT_CATEGORY_ID
        used = Counter(int(o.get("categoryId") or DEFAULT_CATEGORY_ID) for o in offers)
        cats = set(used) | {DEFAULT_CATEGORY_ID}
        cats |= {int(t["category_id"]) for t in catalog.rental_targets(catalog.load_catalog())}
        slots: Dict[int, int] = {}
        for cid in cats:
            limit = await self._soft(self.sv.get_category_limit(cid), 10) if cid in used or \
                cid == DEFAULT_CATEGORY_ID else None
            if limit:
                slots[cid] = max(0, limit - used.get(cid, 0))
        main_limit = await self._soft(self.sv.get_category_limit(DEFAULT_CATEGORY_ID), 10)
        main_free = slots.get(DEFAULT_CATEGORY_ID)
        return main_limit, main_free, slots

    # ------------------------------------------------------------ лоты

    async def _lots_bundle(self, force: bool = False):
        offers = await self.offers.get(self.store, self.sv, force=force)
        patterns = self.store.get("game_title_patterns") or []
        names = {o.get("id"): autocreate.offer_game(o, patterns) or views.offer_name(o, None)
                 for o in offers}
        linked, free, tiers = set(), {}, {}
        products = {int(p["id"]): p for p in await self.engine.products()}
        for o in offers:
            m = match_offer(self.store, o)
            if m:
                linked.add(o.get("id"))
                prod = products.get(int(m.get("product_id") or 0))
                free[o.get("id")] = int(prod.get("available_accounts") or 0) if prod else None
                tiers[o.get("id")] = self._tier_info(prod)[0]
        return offers, names, linked, free, tiers

    async def r_lots(self, ctx: Ctx, arg: str, force: bool = False) -> Screen:
        offers, names, linked, free, tiers = await self._lots_bundle(force)
        page = int(arg or 0) if str(arg or "0").isdigit() else 0
        limit = await self._soft(self.sv.get_category_limit(autocreate.DEFAULT_CATEGORY_ID), 8)
        return views.lots_list(offers, names, linked, page, {"ts": self.maint.last_stock_ts},
                               free, tiers, limit)

    async def r_lots_refresh(self, ctx: Ctx, arg: str) -> Screen:
        return await self.r_lots(ctx, "0", force=True)

    async def _find_offer(self, offer_id: str) -> Optional[Dict[str, Any]]:
        for force in (False, True):
            for o in await self.offers.get(self.store, self.sv, force=force):
                if str(o.get("id")) == str(offer_id):
                    return o
        return None

    async def r_lot(self, ctx: Ctx, offer_id: str) -> Screen:
        offer = await self._find_offer(offer_id)
        if not offer:
            return ("Лот не найден — возможно, он удалён.", views.back("lots_r", "◀️ К списку"))
        mapping = match_offer(self.store, offer)
        product = None
        if mapping:
            pid = int(mapping.get("product_id") or 0)
            product = next((p for p in await self.engine.products() if int(p["id"]) == pid), None)
        game = autocreate.offer_game(offer, self.store.get("game_title_patterns") or [])
        tier, info = self._tier_info(product)
        return views.lot_card(offer, game, mapping, product, int(self.store.get("hours_per_unit", 1)),
                              tier, info)

    async def r_lot_action(self, ctx: Ctx, arg: str) -> Screen:
        offer_id, _, action = arg.partition(":")
        offer = await self._find_offer(offer_id)
        if not offer:
            return ("Лот не найден.", views.back("lots_r", "◀️ К списку"))
        game = autocreate.offer_game(offer, self.store.get("game_title_patterns") or [])
        mapping = match_offer(self.store, offer)

        if action == "toggle":
            new_state = not offer.get("isActive", True)
            await self.sv.set_offer_active(offer, new_state)
            offer["isActive"] = new_state
            if mapping:                      # ручное решение важнее автоскрытия
                mapping["hidden_by_bot"] = False
                self.store.save_mappings()
            return await self.r_lot(ctx, offer_id)
        if action == "del":
            return views.lot_delete_confirm(offer, game)
        if action == "delok":
            await self.sv.delete_offer(offer)
            self._note_deleted(1)
            self.offers.remove(offer.get("id"))
            if mapping:
                for k in ("offer_id", "offer_public_id", "offer_created"):
                    mapping.pop(k, None)
                self.store.save_mappings()
            screen = await self.r_lots(ctx, "0")
            return (f"🗑 Лот «{views.e(game or offer_id)}» удалён.\n\n" + screen[0], screen[1])
        return await self.r_lot(ctx, offer_id)

    def _note_deleted(self, n: int) -> None:
        self.store.state["deleted_by_panel"] = int(self.store.state.get("deleted_by_panel") or 0) + n
        self.store.save_state()

    async def r_lot_ask(self, ctx: Ctx, arg: str) -> Screen:
        offer_id, _, what = arg.partition(":")
        offer = await self._find_offer(offer_id)
        if not offer:
            return ("Лот не найден.", views.back("lots_r"))
        await ctx.state.set_state(Ask.value)
        await ctx.state.update_data(mode="lot", what=what, offer_id=offer_id, back=f"lot:{offer_id}")
        if what == "price":
            text = (f"💵 <b>Новая цена за штуку</b>\n\nСейчас: {views.money(offer.get('price'))}\n\n"
                    "Пришлите число, например 3.5")
        else:
            text = (f"📦 <b>Сколько штук можно купить за раз</b>\n\n"
                    f"Сейчас: до {int(offer.get('availability') or 0)} шт\n\n"
                    "Пришлите число от 0 до 999. Обычно это максимум часов, который "
                    "KOSell сдаёт за раз (720). При включённой сверке бот сам держит это значение.")
        return text, views.kb([views.b("✖️ Отмена", "cancel")])

    # --- массовые действия

    async def r_lots_stock(self, ctx: Ctx, arg: str) -> Optional[Screen]:
        async def job(progress, cancel):
            await progress(0, 1, "сверяю с KOSell", 0, 0)
            res = await self.maint.stock_tick(force=True)
            if not res:
                return ["KOSell не ответил — попробуйте позже."]
            lines = [f"Изменено лотов: <b>{res['changed']}</b>",
                     f"Снято с продажи (нет аккаунтов): {res['hidden']}",
                     f"Вернулось в продажу: {res['restored']}"]
            if res["failed"]:
                lines += ["", f"❌ Ошибок: {res['failed']}"] + views.grouped_errors(res["errors"])
            return lines
        return await self._start_job(ctx, "Сверка остатков", "✅ Остатки сверены", job, "lots")

    async def r_lots_price(self, ctx: Ctx, arg: str) -> Screen:
        offers = await self.offers.get(self.store, self.sv, force=True)
        self._price_changes = await autocreate.reprice_plan(self.store, self.ks, offers, self.demand)
        ch = self._price_changes
        if not ch:
            return ("💸 <b>Пересчёт цен</b>\n\nВсе цены уже соответствуют настройкам наценки.",
                    views.kb([views.b("⚙️ Настройки цен", "setg:price")],
                             [views.b("◀️ К лотам", "lots:0")]))
        up = sum(1 for c in ch if c["new"] > c["old"])
        lines = [f"💸 <b>Пересчёт цен</b>\n\nИзменится: <b>{len(ch)}</b> "
                 f"(дороже {up}, дешевле {len(ch) - up})", ""]
        for c in ch[:12]:
            arrow = "🔺" if c["new"] > c["old"] else "🔻"
            icon = views.TIER_ICON.get(c.get("tier") or "", "")
            lines.append(f"{arrow} {icon}{views.e(str(c['game'])[:28])}: {c['old']:.2f} → <b>{c['new']:.2f}</b>")
        if len(ch) > 12:
            lines.append(f"… и ещё {len(ch) - 12}")
        return "\n".join(lines), views.kb(
            [views.b("✅ Применить", "lots_price_go")],
            [views.b("⚙️ Настройки цен", "setg:price"), views.b("◀️ Назад", "lots:0")])

    async def r_lots_price_go(self, ctx: Ctx, arg: str) -> Optional[Screen]:
        changes = list(self._price_changes)
        if not changes:
            return await self.r_lots_price(ctx, "")

        async def job(progress, cancel):
            res = await autocreate.apply_prices(self.sv, changes, progress=progress, cancel=cancel)
            self.offers.invalidate()
            lines = [f"Обновлено цен: <b>{res['updated']}</b>"]
            if res["failed"]:
                lines += [f"❌ Ошибок: {res['failed']}"] + views.grouped_errors(res["errors"])
            return lines
        return await self._start_job(ctx, "Меняю цены", "✅ Цены обновлены", job, "lots")

    async def r_lots_bump(self, ctx: Ctx, arg: str) -> Optional[Screen]:
        async def job(progress, cancel):
            await progress(0, 1, "поднимаю", 0, 0)
            res = await self.maint.bump_tick(force=True) or {}
            lines = [f"Поднято игр: <b>{res.get('ok', 0)}</b> из {res.get('games', 0)}"]
            if res.get("cooldown"):
                lines.append("⏳ Часть лотов пока нельзя поднять — у Starvell перерыв между поднятиями.")
            if res.get("failed"):
                lines += views.grouped_errors(res.get("errors", []))
            return lines
        return await self._start_job(ctx, "Поднятие лотов", "⬆️ Готово", job, "lots")

    async def r_lots_link(self, ctx: Ctx, arg: str) -> Optional[Screen]:
        async def job(progress, cancel):
            await progress(0, 1, "сопоставляю", 0, 0)
            products = await self.engine.products(force=True)
            offers = await self.offers.get(self.store, self.sv, force=True)
            res = await sync_mappings(self.store, products, offers)
            lines = [f"Лотов проверено: {res['offers']}",
                     f"Новых привязок: <b>{res['linked']}</b> · обновлено: {res['updated']}"]
            if res["unknown"]:
                lines += ["", f"⚠️ Не нашлись в KOSell ({len(res['unknown'])}):"]
                lines += [f"• {views.e(g)}" for g in res["unknown"][:10]]
                lines.append("<i>Их можно привязать вручную в 🔗 Привязки.</i>")
            return lines
        return await self._start_job(ctx, "Связываю лоты с KOSell", "🔗 Готово", job, "lots")

    # ------------------------------------------------------------ создание лотов

    async def r_new(self, ctx: Ctx, arg: str) -> Screen:
        if ctx.call and ctx.message:
            await self._edit(ctx.message, ("⏳ Считаю, что можно создать…", views.kb()))
        offers = await self.offers.get(self.store, self.sv, force=True)
        products = await self.engine.products()
        limit, free, slots = await self._slots(offers)
        try:
            everything = await autocreate.build_plan(
                self.store, self.ks, live_offers=offers, demand=self.demand, products=products)
        except RuntimeError as exc:
            return (f"➕ <b>Создание лотов</b>\n\n⚠️ {views.e(exc)}",
                    views.kb([views.b("🔄 Обновить каталог", "cat_scan")],
                             [views.b("◀️ Меню", "home")]))
        self._plan = autocreate.build_plan_cap(everything, slots)
        ready = self.demand is not None and self.demand.ready
        return views.wizard({
            "existing": len(offers),
            "limit": limit,
            "free_slots": free,
            "candidates": len(everything),
            "available": len(self._plan),
            "demand_ready": ready,
            "demand_ts": self.demand.updated_at if ready else 0,
            "top": self._plan[:5] if ready else [],
            "markup": float(self.store.get("lots_markup_percent", 0)),
            "commission": float(self.store.get("lots_commission_percent", 0)),
            "min_price": autocreate.tier_floor(self.store, None),
            "floor_pop": autocreate.tier_floor(self.store, "popular"),
            "floor_hot": autocreate.tier_floor(self.store, "hot"),
        })

    async def r_new_n(self, ctx: Ctx, arg: str) -> Screen:
        if not self._plan:
            return await self.r_new(ctx, "")
        n = max(1, min(int(arg or 1), len(self._plan)))
        return views.wizard_confirm(self._plan[:n], float(self.store.get("lots_batch_pause", 1.5)))

    async def r_new_example(self, ctx: Ctx, arg: str) -> Screen:
        if not self._plan:
            return await self.r_new(ctx, "")
        item = dict(self._plan[0])
        item["title_len"] = starvell_len(item["title"])
        return views.lot_example(item)

    async def r_new_go(self, ctx: Ctx, arg: str) -> Optional[Screen]:
        n = int(arg or 0)
        plan = self._plan[:n]
        if not plan:
            return await self.r_new(ctx, "")

        async def job(progress, cancel):
            live = await self.offers.get(self.store, self.sv)
            res = await autocreate.create_lots(self.store, self.sv, plan, live_offers=live,
                                               progress=progress, cancel=cancel)
            self.offers.invalidate()
            self._plan = []
            lines = [f"Создано: <b>{res['created']}</b> из {res['total']}"]
            if res["failed"]:
                lines += ["", f"❌ Не создано: {res['failed']}"] + views.grouped_errors(res["errors"])
            if res.get("stopped"):
                lines += ["", f"⏹ {views.e(res['stopped'])}"]
            if res["created"]:
                lines += ["", "Новые лоты проходят модерацию Starvell — обычно это быстро.",
                          "Привязки к KOSell записаны автоматически."]
            return lines
        return await self._start_job(ctx, f"Создаю {len(plan)} лотов", "➕ Готово", job, "lots")

    async def r_catalog_scan(self, ctx: Ctx, arg: str) -> Optional[Screen]:
        async def job(progress, cancel):
            async def prog(i, total, name):
                await progress(i, total, name, i, 0)
            data = await catalog.scan_catalog(self.sv, rental_only=True, progress=prog, pause=0.3)
            targets = catalog.rental_targets(data)
            return [f"Разделов аренды: <b>{len(targets)}</b>", ""] + [
                f"• {views.e(t['game_name'])}" for t in targets[:25]]
        return await self._start_job(ctx, "Обновляю каталог Starvell", "📚 Каталог обновлён", job, "home")

    async def r_demand_refresh(self, ctx: Ctx, arg: str) -> Optional[Screen]:
        if self.demand is None:
            return ("Модуль спроса не подключён.", views.back("home"))

        async def job(progress, cancel):
            products = await self.engine.products(force=True)
            res = await self.demand.refresh(products, force=True, progress=progress, cancel=cancel)
            if res.get("skipped"):
                return ["Обновление уже идёт в фоне — загляните через минуту."]
            hot = pop = 0
            for p in products:
                t = autocreate.product_tier(self.store, self.demand, p)
                hot += t == "hot"
                pop += t == "popular"
            lines = [f"Игр с данными Steam: <b>{res['games']}</b>",
                     f"🔥 хитов: {hot} · ⭐ популярных: {pop} · остальные — обычные"]
            if res.get("failed"):
                lines.append(f"⚠️ Steam не ответил по {res['failed']} играм — повторю позже.")
            lines += ["", "Теперь «➕ Создать лоты» выставляет лучшие игры первыми, а "
                          "«💸 Пересчитать цены» поднимет цены хитов."]
            return lines
        return await self._start_job(ctx, "Загружаю спрос из Steam", "📊 Спрос обновлён", job, "home")

    async def _sold_games(self) -> set:
        from core.storage import game_key
        days = float(self.store.get("rotate_after_days", 7))
        since = time.time() - days * 86400
        return {game_key(i.get("game", "")) for i in self.stats.items
                if i.get("ts", 0) >= since and i.get("kind") in ("rent", "extend")}

    async def r_rotation(self, ctx: Ctx, arg: str) -> Screen:
        offers = await self.offers.get(self.store, self.sv, force=True)
        weak = autocreate.weak_offers(self.store, offers, await self._sold_games())
        candidates = await autocreate.build_plan(self.store, self.ks, live_offers=offers,
                                                 demand=self.demand)
        self._rotation = autocreate.rotation_pairs(weak, candidates)
        return views.rotation(self._rotation, len(weak), {
            "days": self.store.get("rotate_after_days", 7),
            "views": self.store.get("rotate_max_views", 5)})

    async def r_rotation_go(self, ctx: Ctx, arg: str) -> Optional[Screen]:
        pairs = list(self._rotation)
        if not pairs:
            return await self.r_rotation(ctx, "")

        async def job(progress, cancel):
            live = await self.offers.get(self.store, self.sv)
            replaced, failed, errors = 0, 0, []
            for i, (weak, pick) in enumerate(pairs, 1):
                if cancel.is_set():
                    break
                try:
                    await self.sv.delete_offer(weak["offer"])
                    self._note_deleted(1)
                    m = match_offer(self.store, weak["offer"])
                    if m:
                        for k in ("offer_id", "offer_public_id", "offer_created"):
                            m.pop(k, None)
                        self.store.save_mappings()
                except StarvellError as exc:
                    failed += 1
                    errors.append(f"{weak['game']}: {human_error(exc)}")
                    await progress(i, len(pairs), weak["game"], replaced, failed)
                    continue
                res = await autocreate.create_lots(self.store, self.sv, [pick], live_offers=live)
                if res["created"]:
                    replaced += 1
                else:
                    failed += 1
                    errors += res["errors"]
                await progress(i, len(pairs), pick["product_name"], replaced, failed)
            self.offers.invalidate()
            self._rotation = []
            lines = [f"Заменено лотов: <b>{replaced}</b> из {len(pairs)}"]
            if failed:
                lines += ["", f"❌ Не получилось: {failed}"] + views.grouped_errors(errors)
            return lines
        return await self._start_job(ctx, "Заменяю слабые лоты", "♻️ Готово", job, "lots")

    # ------------------------------------------------------------ фоновые задачи

    async def _start_job(self, ctx: Ctx, title: str, done_title: str,
                         runner: Callable, back_to: str = "home") -> Optional[Screen]:
        if self._job and not self._job.done():
            return (f"⏳ Уже выполняется: <b>{views.e(self._job_title)}</b>\n\n"
                    "Дождитесь окончания или остановите её.",
                    views.kb([views.b("⏹ Остановить", "job_stop")], [views.b("◀️ Меню", "home")]))
        msg = ctx.message
        if msg is None:
            return None
        cancel = asyncio.Event()
        state = {"last": 0.0}

        async def progress(done: int, total: int, current: str = "", ok: int = 0, failed: int = 0):
            now = time.time()
            if done < total and now - state["last"] < 2.0:
                return        # Telegram не любит частые правки одного сообщения
            state["last"] = now
            await self._edit(msg, views.progress(title, done, total, ok, failed, current))

        async def wrapper():
            try:
                await self._edit(msg, views.progress(title, 0, 1, 0, 0, "запускаю…"))
                lines = await runner(progress, cancel)
                if cancel.is_set():
                    lines = ["⏹ Остановлено по вашей команде.", ""] + list(lines or [])
                await self._edit(msg, views.job_done(done_title, lines or ["Готово."], back_to))
            except asyncio.CancelledError:
                raise
            except StarvellError as exc:
                await self._edit(msg, views.job_done("⚠️ Не получилось", [views.e(human_error(exc))]))
            except Exception as exc:
                logger.exception("задача «%s»", title)
                await self._edit(msg, views.job_done("⚠️ Ошибка", [views.e(str(exc)[:300])]))

        self._job_title = title
        self._job_cancel = cancel
        self._job = asyncio.create_task(wrapper(), name=f"job:{title}")
        return None

    async def r_job_stop(self, ctx: Ctx, arg: str) -> Optional[Screen]:
        if self._job and not self._job.done() and self._job_cancel:
            self._job_cancel.set()
            if ctx.message:
                await self._edit(ctx.message, (f"⏹ Останавливаю «{views.e(self._job_title)}»…\n"
                                               "Текущий шаг доделается, дальше — стоп.", views.kb()))
            return None
        return await self.r_home(ctx, "")

    # ------------------------------------------------------------ аренды

    async def r_rentals(self, ctx: Ctx, arg: str) -> Screen:
        return views.rentals(self.store.all_rentals(), int(self.store.get("tz_offset_hours", 3)))

    async def r_rental_code(self, ctx: Ctx, uid: str) -> Screen:
        rental = next((r for r in self.store.all_rentals() if r.get("rental_uid") == uid), None)
        data = await self._soft(self.ks.guard_code(uid), 15)
        login = views.e((rental or {}).get("login") or "—")
        if not data or not data.get("code"):
            text = f"🔐 Код для <code>{login}</code> получить не удалось.\n" \
                   f"{views.e((data or {}).get('error') or 'KOSell не ответил')}"
        else:
            text = (f"🔐 Steam Guard для <code>{login}</code>:\n\n<code>{views.e(data['code'])}</code>\n\n"
                    f"Действует ~{data.get('expires_in') or 30} сек.")
        return text, views.kb([views.b("🔄 Ещё раз", f"rentc:{uid}")],
                              [views.b("◀️ К арендам", "rent")])

    # ------------------------------------------------------------ статистика

    async def r_stats(self, ctx: Ctx, period: str) -> Screen:
        period = period if period in views.PERIODS else "day"
        tz = int(self.store.get("tz_offset_hours", 3))
        since = {"day": day_start(tz), "week": time.time() - 7 * 86400,
                 "month": time.time() - 30 * 86400, "all": None}[period]
        commission = float(self.store.get("lots_commission_percent", 0))
        return views.stats(self.stats.summary(since, commission), period, commission)

    # ------------------------------------------------------------ привязки

    async def r_maps(self, ctx: Ctx, arg: str) -> Screen:
        page = int(arg) if (arg or "").isdigit() else 0
        items = sorted(self.store.mappings, key=lambda m: str(m.get("game") or "").lower())
        return views.maps_list(items, page, bool(self.store.get("auto_map_by_name", True)))

    async def r_tog_maps(self, ctx: Ctx, key: str) -> Screen:
        self.store.set(key, not bool(self.store.get(key)))
        return await self.r_maps(ctx, "0")

    async def r_map(self, ctx: Ctx, mid: str) -> Screen:
        m = self.store.find_mapping_by_id(mid)
        return views.map_card(m) if m else await self.r_maps(ctx, "0")

    async def r_map_toggle(self, ctx: Ctx, mid: str) -> Screen:
        m = self.store.find_mapping_by_id(mid)
        if m:
            m["enabled"] = not m.get("enabled", True)
            self.store.save_mappings()
        return await self.r_map(ctx, mid)

    async def r_map_delete(self, ctx: Ctx, mid: str) -> Screen:
        m = self.store.find_mapping_by_id(mid)
        if m:
            self.store.delete_mapping(m.get("key"))
        return await self.r_maps(ctx, "0")

    async def r_map_product(self, ctx: Ctx, mid: str) -> Screen:
        await ctx.state.set_state(Ask.value)
        await ctx.state.update_data(mode="map_product", mid=mid, back=f"map:{mid}")
        return ("🎮 <b>Какой товар KOSell выдавать?</b>\n\nПришлите название игры "
                "как в каталоге KOSell или номер товара (#63).",
                views.kb([views.b("✖️ Отмена", "cancel")]))

    async def r_map_add(self, ctx: Ctx, arg: str) -> Screen:
        await ctx.state.set_state(Ask.value)
        await ctx.state.update_data(mode="map_add", back="maps:0")
        return ("➕ <b>Новая привязка</b>\n\nПришлите одной строкой:\n"
                "<code>название в лоте | товар KOSell</code>\n\n"
                "Например: <code>PEAK | PEAK</code>\n"
                "Если названия совпадают, достаточно одного.",
                views.kb([views.b("✖️ Отмена", "cancel")]))

    # ------------------------------------------------------------ настройки

    async def r_settings(self, ctx: Ctx, arg: str) -> Screen:
        return views.settings_root()

    def _values(self) -> Dict[str, Any]:
        vals = {k: self.store.get(k) for k in FIELDS}
        cost = 1.0
        price = autocreate.price_for(self.store, cost * max(1, int(self.store.get("hours_per_unit", 1))))
        vals["_price_example"] = (f"📐 Пример: аккаунт за {views.money(cost)}/ч → "
                                  f"<b>{views.money(price)}</b> за штуку")
        return vals

    async def r_settings_group(self, ctx: Ctx, gid: str, note: str = "") -> Screen:
        return views.settings_group(gid, self._values(), note)

    async def r_toggle(self, ctx: Ctx, arg: str) -> Screen:
        key, _, gid = arg.partition(":")
        if key in FIELDS and FIELDS[key].kind == "bool":
            self.store.set(key, not bool(self.store.get(key)))
            logger.info("панель: %s = %s", key, self.store.get(key))
        return await self.r_settings_group(ctx, gid)

    async def r_ask(self, ctx: Ctx, arg: str) -> Screen:
        key, _, gid = arg.partition(":")
        if key not in FIELDS:
            return views.settings_root()
        back = f"tpl:{key}" if gid == "tpl" else f"setg:{gid}"
        await ctx.state.set_state(Ask.value)
        await ctx.state.update_data(mode="field", key=key, back=back)
        return views.ask_prompt(key, self.store.get(key))

    # ------------------------------------------------------------ тексты

    async def r_texts(self, ctx: Ctx, arg: str) -> Screen:
        if arg:
            current = self.store.texts.get(arg, DEFAULT_TEXTS.get(arg, ""))
            return views.text_card(arg, current, current != DEFAULT_TEXTS.get(arg, ""))
        custom = {k for k, v in self.store.texts.items() if v != DEFAULT_TEXTS.get(k)}
        return views.texts_list(custom, bool(self.store.get("lots_title_template")),
                                bool(self.store.get("lots_description_template")))

    async def r_text_edit(self, ctx: Ctx, key: str) -> Screen:
        await ctx.state.set_state(Ask.value)
        await ctx.state.update_data(mode="text", key=key, back=f"txt:{key}")
        return views.text_edit_prompt(key)

    async def r_text_reset(self, ctx: Ctx, key: str) -> Screen:
        if key in DEFAULT_TEXTS:
            self.store.texts[key] = DEFAULT_TEXTS[key]
            self.store.save_texts()
        return await self.r_texts(ctx, key)

    async def r_template(self, ctx: Ctx, key: str) -> Screen:
        if key == "lots_title_template":
            example = autocreate.make_title(self.store, "PEAK")
            example += f"\n\n({starvell_len(example)}/100 символов)"
        else:
            example = autocreate.make_description(self.store, "PEAK")
        return views.template_card(key, self.store.get(key) or "", example)

    async def r_template_reset(self, ctx: Ctx, key: str) -> Screen:
        if key in ("lots_title_template", "lots_description_template"):
            self.store.set(key, "")
        return await self.r_template(ctx, key)

    # ================================================================ ввод значений

    async def _on_value(self, message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        mode = data.get("mode")
        raw = (message.text or "").strip()
        if not raw:
            await message.answer("Пришлите значение текстом или нажмите «Отмена».")
            return
        try:
            note = await self._apply_value(mode, data, raw)
        except ValueError as exc:
            await message.answer(f"❌ {views.e(exc)}. Попробуйте ещё раз или нажмите «Отмена».",
                                 parse_mode="HTML",
                                 reply_markup=views.kb([views.b("✖️ Отмена", "cancel")]))
            return
        except StarvellError as exc:
            await state.clear()
            await message.answer(f"⚠️ Starvell: {views.e(human_error(exc))}", parse_mode="HTML",
                                 reply_markup=views.kb([views.b("◀️ Назад", data.get("back") or "home")]))
            return
        await state.clear()
        screen = await self._screen(data.get("back") or "home", Ctx(None, None, message))
        if note:
            screen = (f"{note}\n\n{screen[0]}", screen[1])
        await self._show(message, screen)

    async def _apply_value(self, mode: str, data: Dict[str, Any], raw: str) -> str:
        if mode in ("smm_search", "smm_rule"):
            return await self._smm_apply_value(mode, raw)
        if mode == "field":
            key = data["key"]
            value = FIELDS[key].parse(raw)
            self.store.set(key, value)
            logger.info("панель: %s изменён", key)
            if key == "starvell_session":
                self.sv.session_cookie = value
                user = await self._soft(self.sv.get_user_info(), 15)
                return (f"✅ Cookie принят, вход как {views.e(self.sv.my_username)}."
                        if user and user.get("id") else "⚠️ Сохранено, но войти не удалось — проверьте cookie.")
            if key == "kosell_api_key":
                await self.ks.close()
                self.ks.api_key = value
                bal = await self._soft(self.ks.balance(), 15)
                return (f"✅ Ключ KOSell работает, баланс {views.money(bal.get('balance_rub'))}."
                        if bal else "⚠️ Сохранено, но KOSell ключ не принял.")
            if key == "optsmm_api_key" and self.smm is not None:
                self.smm.api.api_key = str(value).strip()
                try:
                    bal, cur = await self.smm.api.balance()
                    return f"✅ Ключ OptSMM работает, баланс {bal:.2f} {views.e(cur)}."
                except Exception as exc:
                    return f"⚠️ Сохранено, но OptSMM ключ не принял: {views.e(exc)}"
            if FIELDS[key].restart:
                return "✅ Сохранено. Вступит в силу после перезапуска бота."
            return "✅ Сохранено."

        if mode == "text":
            self.store.texts[data["key"]] = raw
            self.store.save_texts()
            return "✅ Текст обновлён."

        if mode == "lot":
            offer = await self._find_offer(data["offer_id"])
            if not offer:
                raise ValueError("лот не найден")
            if data["what"] == "price":
                price = FIELDS["lots_min_price_rub"].parse(raw)
                await self.sv.set_offer_price(offer, price)
                offer["price"] = f"{price:.2f}"
                return f"✅ Цена: {views.money(price)}"
            try:
                qty = int(raw)
            except ValueError:
                raise ValueError("нужно целое число") from None
            if not 0 <= qty <= 999:
                raise ValueError("от 0 до 999")
            await self.sv.set_offer_availability(offer, qty)
            offer["availability"] = qty
            return f"✅ Наличие: {qty} шт"

        if mode in ("map_add", "map_product"):
            parts = [p.strip() for p in raw.split("|")]
            game = parts[0]
            query = parts[1] if len(parts) > 1 else parts[0]
            product = await self._resolve_product(query)
            if not product:
                raise ValueError(f"в каталоге KOSell нет «{query}»")
            if mode == "map_product":
                m = self.store.find_mapping_by_id(data["mid"])
                if not m:
                    raise ValueError("привязка не найдена")
                m.update(product_id=int(product["id"]), product_name=product.get("name"), auto=False)
                self.store.save_mappings()
            else:
                self.store.upsert_mapping({"game": game, "product_id": int(product["id"]),
                                           "product_name": product.get("name"),
                                           "enabled": True, "auto": False})
            return f"✅ «{views.e(game)}» → {views.e(product.get('name'))}"

        raise ValueError("неизвестное действие")

    async def _resolve_product(self, query: str) -> Optional[Dict[str, Any]]:
        q = query.strip().lstrip("#")
        products = await self.engine.products()
        if q.isdigit():
            return next((p for p in products if int(p["id"]) == int(q)), None)
        from core.storage import game_key
        key = game_key(q)
        exact = next((p for p in products if game_key(p.get("name", "")) == key), None)
        if exact:
            return exact
        return next((p for p in products if key and key in game_key(p.get("name", ""))), None)
