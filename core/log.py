"""Логирование проекта."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

LOG_DIR = os.path.join(os.environ.get("DATA_DIR") or "storage", "logs")
_configured = False


class RepeatFilter(logging.Filter):
    """Схлопывает одинаковые сообщения: первое пишется, дальше — раз в N.

    Когда Telegram недоступен, aiogram пишет одну и ту же ошибку каждые
    5 секунд — за час это сотни строк, в которых тонет полезное.
    """

    def __init__(self, every: int = 30) -> None:
        super().__init__()
        self.every = every
        self._counts: dict = {}

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        key = (record.name, msg.split(" - ")[0][:80] if "Sleep for" not in msg else "sleep")
        n = self._counts.get(key, 0) + 1
        self._counts[key] = n
        if len(self._counts) > 500:
            self._counts.clear()
        if n == 1:
            return True
        if n % self.every == 0:
            record.msg = f"{msg}  (повторилось {n} раз)"
            record.args = ()
            return True
        return False


def setup_logging(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    os.makedirs(LOG_DIR, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
        datefmt="%d.%m.%Y %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "bot.log"), maxBytes=5 * 1024 * 1024,
        backupCount=3, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    for noisy in ("aiohttp.access", "aiogram.event", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("aiogram.dispatcher").addFilter(RepeatFilter(every=30))

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
