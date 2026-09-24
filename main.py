"""Точка входа: автоаренда Steam-аккаунтов KOSell на площадке Starvell."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import traceback


def hold_window(message: str = "") -> None:
    """Не дать окну консоли закрыться, если бот запущен двойным кликом.

    Windows закрывает окно сразу после завершения скрипта, и прочитать
    ошибку невозможно. Пауза ставится только в интерактивной консоли,
    чтобы не подвешивать запуск из планировщика или из-под сервиса.
    """
    if message:
        print(message)
    if os.name != "nt":
        return
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\nНажмите Enter, чтобы закрыть окно…")
    except Exception:
        pass


def check_python() -> None:
    if sys.version_info < (3, 10):
        hold_window(
            f"\n[!] Нужен Python 3.10 или новее, а сейчас {sys.version.split()[0]}.\n"
            "    Скачайте свежий Python с python.org и при установке\n"
            "    обязательно отметьте галочку «Add Python to PATH»."
        )
        sys.exit(1)


def check_dependencies() -> None:
    """Проверка библиотек до импорта модулей бота — иначе ошибка непонятна."""
    missing = []
    for module, package in (("aiohttp", "aiohttp"), ("aiogram", "aiogram")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        hold_window(
            "\n[!] Не установлены библиотеки: " + ", ".join(missing) + "\n\n"
            "    Откройте эту папку в командной строке и выполните:\n"
            "        pip install -r requirements.txt\n\n"
            "    Либо просто запустите start.bat — он всё поставит сам."
        )
        sys.exit(1)


LOCK_PORT = 47391
_lock_socket = None


def single_instance() -> bool:
    """Не даёт запустить второго бота.

    Два экземпляра читают одни и те же заказы — и могут выдать по одному
    заказу два аккаунта, дважды списав деньги в KOSell. Замок — занятый
    локальный порт: он освобождается сам, даже если бот упал или окно
    закрыли крестиком.
    """
    global _lock_socket
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):          # Windows
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
        sock.listen(1)
    except OSError:
        sock.close()
        return False
    _lock_socket = sock
    return True


def self_update_on_start() -> None:
    """На хостинге перед запуском подтягивает свежий код из GitHub.

    Срабатывает, только если задан адрес репозитория (GIT_ADDRESS) и не
    выключен AUTO_UPDATE. Сразу после обновления процесс перезапускается
    с новым кодом — второй раз проверка уже не делается.
    """
    if os.environ.pop("BOT_JUST_UPDATED", ""):
        return
    try:
        from core import selfupdate
        if not selfupdate.auto_enabled():
            return
        print("[обновление] проверяю GitHub…", flush=True)
        result = selfupdate.update()
        print(f"[обновление] {result.message}", flush=True)
        if result.changed:
            selfupdate.restart_process()
    except Exception as exc:      # обновление не должно мешать запуску
        print(f"[обновление] пропущено: {exc}", flush=True)


check_python()
check_dependencies()
self_update_on_start()

# Импорты бота — только после проверок, иначе падает с невнятным ImportError
from core.engine import Engine            # noqa: E402
from core.demand import Demand            # noqa: E402
from core.maintenance import Maintenance  # noqa: E402
from core.stats import SalesLog           # noqa: E402
from lots.sync import OffersCache         # noqa: E402
from core.kosell import KosellAPI         # noqa: E402
from core.optsmm import OptSmmAPI, OptSmmError  # noqa: E402
from core.smm import SmmEngine            # noqa: E402
from core.log import get_logger, setup_logging  # noqa: E402
from core.poller import Poller            # noqa: E402
from core.starvell import StarvellAPI, StarvellAuthError  # noqa: E402
from core.storage import DATA_DIR, Store  # noqa: E402
from core.instance import InstanceLock    # noqa: E402
from tgpanel.panel import Panel           # noqa: E402
from core.updates import UpdateChecker    # noqa: E402
from core import selfupdate               # noqa: E402
from version import VERSION               # noqa: E402

logger = get_logger("main")

RESTART = {"requested": False}

BANNER = r"""
  KOSell x OptSMM x Starvell - auto rent + SMM
"""


async def run() -> int:
    os.chdir(os.path.dirname(os.path.abspath(__file__)) or ".")
    if not single_instance():
        hold_window(
            "\n[!] Бот уже запущен в другом окне.\n"
            "    Два бота одновременно могут выдать один заказ дважды,\n"
            "    поэтому второй запуск остановлен. Закройте лишнее окно\n"
            "    (или найдите python.exe в диспетчере задач), затем запустите снова."
        )
        return 1
    store = Store()
    setup_logging(store.get("log_level", "INFO"))
    print(BANNER)
    logger.info("версия %s · данные: %s", VERSION, os.path.abspath(DATA_DIR))

    lock = InstanceLock()
    if not await lock.acquire(wait=90):
        hold_window(
            "\n[!] С этими же данными уже работает другая копия бота.\n"
            "    Остановите её, иначе заказы могут выдаваться дважды."
        )
        return 1
    lock.start_heartbeat()

    missing = [
        name for name, key in (
            ("session cookie Starvell", "starvell_session"),
            ("API-ключ KOSell", "kosell_api_key"),
            ("токен Telegram-бота", "tg_token"),
        )
        if not (store.get(key) or "").strip()
    ]
    if missing:
        store.save_settings()
        logger.error("Не заполнены настройки: %s", ", ".join(missing))
        if not (store.get("tg_token") or "").strip():
            hold_window(
                "\n[!] Откройте файл storage\\settings.json (он только что создан)\n"
                "    и впишите значения:\n"
                "      starvell_session — cookie session со starvell.com\n"
                "      kosell_api_key   — ключ из профиля kosell.store\n"
                "      tg_token         — токен бота от @BotFather\n"
                "    На хостинге вместо файла задайте переменные окружения:\n"
                "      STARVELL_SESSION, KOSELL_API_KEY, TG_TOKEN, TG_ADMINS"
            )
            await lock.release()
            return 1

    proxy = (store.get("proxy_url") or "").strip()
    if proxy in ("-", "нет"):
        proxy = ""

    sv = StarvellAPI(store.get("starvell_session", ""), proxy_url=proxy)
    ks = KosellAPI(store.get("kosell_api_key", ""), proxy_url=proxy)
    smm_api = OptSmmAPI(store.get("optsmm_api_key", ""), proxy_url=proxy)
    await sv.start()
    await ks.start()

    try:
        user = await sv.get_user_info()
        if user.get("id"):
            store.state["my_user_id"] = sv.my_user_id
            store.save_state()
            logger.info("Starvell: вход выполнен как %s (id %s)", sv.my_username, sv.my_user_id)
        else:
            logger.warning("Starvell: профиль не определён — проверьте session cookie")
    except StarvellAuthError:
        logger.error("Starvell: session cookie недействителен")
    except Exception as exc:
        logger.warning("Starvell: не удалось получить профиль (%s)", exc)

    balance = await ks.balance()
    if balance:
        logger.info(
            "KOSell: %s, баланс %.2f ₽ / %.2f $",
            balance.get("username"), balance.get("balance_rub", 0),
            balance.get("balance_usd", 0),
        )
    else:
        logger.warning("KOSell: баланс не получен — проверьте API-ключ")

    if smm_api.configured:
        try:
            smm_bal, smm_cur = await smm_api.balance()
            logger.info("OptSMM: баланс %.2f %s", smm_bal, smm_cur)
        except OptSmmError as exc:
            logger.warning("OptSMM: %s", exc)
    else:
        logger.info("OptSMM: ключ не задан — SMM-модуль ждёт настройки")

    panel_holder: dict = {}

    async def notify_admin(text: str) -> None:
        panel = panel_holder.get("panel")
        if panel:
            await panel.notify(text)

    stats = SalesLog()
    offers = OffersCache(ttl=60)
    engine = Engine(store, sv, ks, notify_admin=notify_admin, stats=stats)
    smm = SmmEngine(store, sv, smm_api, notify_admin=notify_admin, stats=stats)
    poller = Poller(store, sv, engine, smm=smm)
    demand = Demand(proxy_url=proxy)
    maintenance = Maintenance(store, sv, engine, offers, demand)
    updates = UpdateChecker(store, proxy_url=proxy)
    stop_event = asyncio.Event()

    def request_restart() -> None:
        """Мягкая остановка и запуск заново — уже с новым кодом."""
        RESTART["requested"] = True
        stop_event.set()

    panel = Panel(store, sv, ks, poller, engine, maintenance, offers, stats, demand, updates, smm=smm)
    panel.request_restart = request_restart
    panel_holder["panel"] = panel

    async def watch_updates() -> None:
        """Следит за релизами на GitHub.

        На ПК только сообщает о новой версии (раз в 6 часов). На хостинге с
        AUTO_UPDATE сам ставит релиз и перезапускается (проверка раз в 30 мин).
        """
        await asyncio.sleep(60)
        while True:
            auto = selfupdate.auto_enabled()
            try:
                if await updates.check(force=True):
                    text = updates.notice_text()
                    if text:
                        logger.info("доступна новая версия %s", updates.latest["version"])
                        await notify_admin(text)
                    if auto and not RESTART["requested"]:
                        version = updates.latest["version"]
                        result = await asyncio.to_thread(selfupdate.update)
                        logger.info("автообновление до %s: %s", version, result.message)
                        if result.changed:
                            request_restart()
                        elif not result.ok:
                            await notify_admin(f"⚠️ Не удалось обновиться: {result.message}")
            except Exception as exc:
                logger.debug("проверка обновлений: %s", exc)
            await asyncio.sleep(1800 if auto else 6 * 3600)

    updates_task = asyncio.create_task(watch_updates(), name="updates")

    await poller.start()
    await maintenance.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # Windows
            pass

    panel_task = asyncio.create_task(panel.run(), name="panel")

    def _panel_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("панель остановлена: %s: %s", type(exc).__name__, exc)

    panel_task.add_done_callback(_panel_done)

    try:
        # процесс живёт, пока не придёт сигнал: падение панели
        # не должно останавливать автовыдачу.
        # Короткий сон вместо ожидания события — чтобы Ctrl+C
        # срабатывал сразу и на Windows, где обработчики сигналов недоступны.
        while not stop_event.is_set():
            await asyncio.sleep(0.5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("останавливаюсь…")
        await poller.stop()
        await maintenance.stop()
        try:
            await panel.stop()
        except Exception as exc:
            logger.debug("остановка панели: %s", exc)
        panel_task.cancel()
        updates_task.cancel()
        await asyncio.gather(panel_task, updates_task, return_exceptions=True)
        store.save_all()
        await sv.close()
        await ks.close()
        await smm_api.close()
        await lock.release()
        logger.info("до встречи")

    return 0


def main() -> None:
    try:
        code = asyncio.run(run())
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)
    except Exception:
        print("\n[!] Бот аварийно завершился. Подробности:\n")
        traceback.print_exc()
        print(
            f"\n    Полный лог: {os.path.join(DATA_DIR, 'logs', 'bot.log')}\n"
            "    Если непонятно — пришлите текст выше."
        )
        hold_window()
        sys.exit(1)

    if code:
        sys.exit(code)
    if RESTART["requested"]:
        print("\nПерезапуск с новой версией…", flush=True)
        selfupdate.restart_process()
    hold_window("\nБот остановлен.")


if __name__ == "__main__":
    main()
