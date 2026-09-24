"""Проверка новых версий бота через GitHub Releases.

Репозиторий берётся из настройки github_repo (или переменной GITHUB_REPO),
формат «владелец/имя». Для приватного репозитория нужен токен в
переменной окружения GITHUB_TOKEN (права только на чтение содержимого).
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, Optional, Tuple

import aiohttp

from core.log import get_logger
from version import VERSION

logger = get_logger("updates")

API = "https://api.github.com/repos/{repo}/releases/latest"


def parse_version(text: str) -> Tuple[int, ...]:
    """'v2.10.1' → (2, 10, 1). Всё нечисловое отбрасывается."""
    nums = re.findall(r"\d+", str(text or "").split("-")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(latest: str, current: str = VERSION) -> bool:
    return parse_version(latest) > parse_version(current)


def normalize_repo(value: str) -> str:
    """Принимает и «owner/name», и полную ссылку на GitHub."""
    value = (value or "").strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    m = re.search(r"github\.com[/:]([^/\s]+/[^/\s]+)$", value)
    if m:
        return m.group(1)
    return value if re.fullmatch(r"[\w.-]+/[\w.-]+", value) else ""


def detect_repo() -> str:
    """Репозиторий, если он не указан в настройках.

    Railway сам передаёт владельца и имя репозитория в переменных окружения,
    а на ПК после setup_github.bat адрес лежит в .git/config.
    """
    for var in ("GIT_ADDRESS", "GIT_REPO", "GIT_REPO_ADDRESS"):   # панель хостинга
        repo = normalize_repo(os.environ.get(var, ""))
        if repo:
            return repo
    owner = os.environ.get("RAILWAY_GIT_REPO_OWNER", "").strip()
    name = os.environ.get("RAILWAY_GIT_REPO_NAME", "").strip()
    if owner and name:
        return f"{owner}/{name}"
    try:
        with open(os.path.join(".git", "config"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', text)
    return normalize_repo(m.group(1)) if m else ""


class UpdateChecker:
    def __init__(self, store: Any, proxy_url: str = "") -> None:
        self.store = store
        self.proxy_url = proxy_url or None
        self.latest: Optional[Dict[str, Any]] = None
        self.checked_ts: float = 0.0
        self.error: str = ""

    @property
    def repo(self) -> str:
        return normalize_repo(self.store.get("github_repo", "")) or detect_repo()

    @property
    def available(self) -> Optional[Dict[str, Any]]:
        """Сведения о новой версии, если она вышла."""
        if self.latest and is_newer(self.latest.get("version", "")):
            return self.latest
        return None

    async def check(self, force: bool = False) -> Optional[Dict[str, Any]]:
        repo = self.repo
        if not repo or not self.store.get("update_check_enabled", True):
            return None
        if not force and time.time() - self.checked_ts < 3600:
            return self.available
        self.checked_ts = time.time()
        headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": f"kosell-starvell-bot/{VERSION}"}
        token = (str(self.store.get("github_token", "") or "").strip()
                 or os.environ.get("GITHUB_TOKEN", "").strip()
                 or os.environ.get("ACCESS_TOKEN", "").strip()
                 or os.environ.get("GIT_ACCESS_TOKEN", "").strip())
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(API.format(repo=repo), headers=headers,
                                 proxy=self.proxy_url) as r:
                    if r.status == 404:
                        self.error = ("релизов нет или репозиторий приватный "
                                      "(нужен токен GitHub в настройках)")
                        return None
                    if r.status == 401:
                        self.error = "GitHub не принял токен — выпустите новый"
                        return None
                    if r.status in (403, 429):
                        self.error = "GitHub временно ограничил запросы, проверю позже"
                        return None
                    if r.status != 200:
                        self.error = f"GitHub ответил {r.status}"
                        return None
                    data = await r.json()
        except Exception as exc:
            self.error = f"GitHub недоступен: {exc}"
            logger.debug("проверка обновлений: %s", exc)
            return None
        self.error = ""
        tag = str(data.get("tag_name") or "")
        self.latest = {
            "version": tag.lstrip("vV"),
            "tag": tag,
            "url": data.get("html_url") or f"https://github.com/{repo}/releases",
            "notes": str(data.get("body") or "").strip(),
            "published": data.get("published_at") or "",
        }
        return self.available

    def notice_text(self) -> Optional[str]:
        """Текст уведомления — один раз на каждую новую версию."""
        info = self.available
        if not info:
            return None
        state = self.store.state
        if state.get("update_notified") == info["version"]:
            return None
        state["update_notified"] = info["version"]
        self.store.save_state()
        notes = info["notes"]
        if len(notes) > 700:
            notes = notes[:700].rsplit("\n", 1)[0] + "\n…"
        text = (f"🆕 Вышла версия <b>{info['version']}</b> (у вас {VERSION}).\n"
                f"{info['url']}")
        if notes:
            from html import escape
            text += "\n\n" + escape(notes)
        from core import selfupdate
        if selfupdate.auto_enabled():
            text += "\n\n<i>Ставлю её сам — бот перезапустится.</i>"
        elif selfupdate.configured():
            text += "\n\n<i>Панель → «ℹ️ Версия» → «⬇️ Обновить сейчас».</i>"
        else:
            text += "\n\n<i>На ПК: закройте бота и запустите update.bat.</i>"
        return text
