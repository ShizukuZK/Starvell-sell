"""Защита от двух копий бота, работающих с одними данными.

Два бота на одних заказах могут выдать один заказ дважды и дважды списать
деньги в KOSell. На своём компьютере это ловит занятый порт (main.py), а в
облаке при перевыкладке старый и новый контейнер могут жить одновременно —
там порт не поможет, поэтому замок лежит в папке данных и «сердцебиением»
подтверждает, что владелец жив.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid
from typing import Optional

from core.log import get_logger
from core.storage import DATA_DIR

logger = get_logger("instance")

LOCK_FILE = os.path.join(DATA_DIR, "instance.lock")
STALE_AFTER = 60      # сек без сердцебиения — владелец считается мёртвым
BEAT_EVERY = 15


class InstanceLock:
    def __init__(self, path: str = LOCK_FILE) -> None:
        self.path = path
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._task: Optional[asyncio.Task] = None

    def _read(self) -> Optional[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _write(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"owner": self.owner, "ts": time.time()}, f)
        os.replace(tmp, self.path)

    def holder(self) -> Optional[str]:
        """Кто держит замок сейчас (None — никто живой)."""
        data = self._read()
        if not data or data.get("owner") == self.owner:
            return None
        if time.time() - float(data.get("ts") or 0) > STALE_AFTER:
            return None
        return str(data.get("owner"))

    async def acquire(self, wait: float = 90.0) -> bool:
        """Берёт замок. Если его держит живая копия — ждёт до wait секунд."""
        deadline = time.time() + wait
        announced = False
        while True:
            other = self.holder()
            if other is None:
                self._write()
                await asyncio.sleep(0.3)           # вдруг кто-то записал одновременно
                if (self._read() or {}).get("owner") == self.owner:
                    return True
                continue
            if time.time() >= deadline:
                return False
            if not announced:
                logger.warning("данными уже пользуется другая копия бота (%s) — жду, "
                               "пока она остановится…", other)
                announced = True
            await asyncio.sleep(5)

    def start_heartbeat(self) -> None:
        async def beat():
            while True:
                await asyncio.sleep(BEAT_EVERY)
                try:
                    data = self._read()
                    if data and data.get("owner") not in (None, self.owner):
                        logger.error("замок перехвачен другой копией бота (%s)", data.get("owner"))
                    self._write()
                except OSError as exc:
                    logger.debug("сердцебиение замка: %s", exc)
        self._task = asyncio.create_task(beat(), name="instance-lock")

    async def release(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if (self._read() or {}).get("owner") == self.owner:
            try:
                os.remove(self.path)
            except OSError:
                pass
