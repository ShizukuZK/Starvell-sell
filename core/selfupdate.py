"""Самообновление бота из GitHub на хостинге.

Работает, когда задан адрес репозитория — переменная GIT_ADDRESS (так её
называет панель Pterodactyl: поле «Git Repo Address») или GIT_REPO.
Токен для приватного репозитория — ACCESS_TOKEN / GIT_ACCESS_TOKEN /
GITHUB_TOKEN, ветка — BRANCH / GIT_BRANCH (пусто — ветка по умолчанию).

Как обновляется: git fetch нужной ветки и git reset --hard на неё. Папка
storage/ и всё, что в .gitignore, не трогается. «git clean» не делается
никогда, поэтому лишние файлы на сервере тоже остаются на месте.

Модуль намеренно использует только стандартную библиотеку: он запускается
из main.py до импорта остальных модулей бота — пока они ещё старые.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIRED = ("main.py", "version.py")        # без них это не репозиторий бота


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def settings() -> Dict[str, str]:
    return {
        "url": _env("GIT_ADDRESS", "GIT_REPO", "GIT_REPO_ADDRESS"),
        "branch": _env("BRANCH", "GIT_BRANCH"),
        # USERNAME — имя поля в Pterodactyl; на Windows так зовут пользователя ПК,
        # но GitHub при входе по токену имя всё равно не проверяет
        "user": _env("GIT_USERNAME", "USERNAME"),
        "token": _env("ACCESS_TOKEN", "GIT_ACCESS_TOKEN", "GITHUB_TOKEN"),
    }


def configured() -> bool:
    """Задан ли репозиторий — тогда в панели есть кнопка «Обновить»."""
    return bool(settings()["url"]) and shutil.which("git") is not None


def auto_enabled() -> bool:
    """Ставить обновления самому: при запуске и при выходе релиза.

    Включается тумблером AUTO UPDATE в панели хостинга (AUTO_UPDATE=1).
    """
    flag = os.environ.get("AUTO_UPDATE", "1").strip().lower()
    return configured() and flag not in ("0", "false", "no", "off")


def clean_url(url: str) -> str:
    """Адрес без логина и токена внутри — его можно хранить и показывать."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and "@" in parts.netloc:
        host = parts.netloc.rsplit("@", 1)[1]
        url = parts._replace(netloc=host).geturl()
    if url.startswith(("https://github.com/", "http://github.com/")) \
            and not url.endswith(".git"):
        url = url.rstrip("/") + ".git"
    return url


@dataclass
class Result:
    ok: bool
    changed: bool = False
    old: str = ""
    new: str = ""
    deps: bool = False          # поменялся requirements.txt
    message: str = ""


class Git:
    def __init__(self, root: str, token: str = "", user: str = "") -> None:
        self.root = root
        self.token = token
        self.user = user or "x-access-token"

    def __call__(self, *args: str, auth: bool = False, timeout: int = 120) -> str:
        cmd: List[str] = ["git", "-c", "safe.directory=*", "-c", "core.autocrlf=false"]
        if auth and self.token:
            pair = base64.b64encode(f"{self.user}:{self.token}".encode()).decode()
            cmd += ["-c", f"http.extraHeader=Authorization: Basic {pair}"]
        cmd += list(args)
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        proc = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True,
                              text=True, timeout=timeout)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            text = err[-1] if err else f"код {proc.returncode}"
            if self.token:
                text = text.replace(self.token, "***")
            raise RuntimeError(text)
        return proc.stdout.strip()

    def head(self) -> str:
        try:
            return self("rev-parse", "HEAD")
        except RuntimeError:
            return ""               # пустой репозиторий — коммитов ещё нет


def _friendly(error: str) -> str:
    low = error.lower()
    if "authentication" in low or "403" in low or "401" in low \
            or "could not read username" in low:
        return ("GitHub не пустил: проверьте GIT USERNAME и GIT ACCESS TOKEN "
                "(у токена должно быть право Contents: Read)")
    if "not found" in low or "404" in low:
        return "репозиторий не найден: проверьте ссылку и токен (для приватного он обязателен)"
    if "couldn't find remote ref" in low:
        return "такой ветки нет в репозитории — проверьте GIT BRANCH"
    if "could not resolve host" in low or "timed out" in low:
        return "нет связи с GitHub, попробую при следующем запуске"
    return error


def _default_branch(git: Git) -> str:
    out = git("ls-remote", "--symref", "origin", "HEAD", auth=True, timeout=60)
    for line in out.splitlines():
        if line.startswith("ref:") and "refs/heads/" in line:
            return line.split("refs/heads/", 1)[1].split()[0]
    return "main"


def _file_at(git: Git, rev: str, path: str) -> Optional[str]:
    try:
        return git("show", f"{rev}:{path}")
    except RuntimeError:
        return None


def install_requirements(root: str = ROOT) -> str:
    """Доставляет библиотеки туда же, куда их ставит хостинг."""
    cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-q",
           "-r", os.path.join(root, "requirements.txt")]
    if sys.prefix == sys.base_prefix:           # не venv — ставим в ~/.local
        cmd.insert(4, "--user")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        return tail[-1] if tail else f"pip: код {proc.returncode}"
    return ""


def update(root: str = ROOT, install_deps: bool = True) -> Result:
    """Скачивает свежий код. Возвращает, изменилось ли что-нибудь."""
    cfg = settings()
    if not cfg["url"]:
        return Result(False, message="репозиторий не задан (GIT_ADDRESS)")
    if shutil.which("git") is None:
        return Result(False, message="на сервере нет git")
    url = clean_url(cfg["url"])
    git = Git(root, cfg["token"], cfg["user"])
    try:
        if not os.path.isdir(os.path.join(root, ".git")):
            # файлы загружены вручную — превращаем папку в репозиторий на месте
            git("init", "-q")
        remotes = git("remote").split()
        if "origin" in remotes:
            git("remote", "set-url", "origin", url)
        else:
            git("remote", "add", "origin", url)
        branch = cfg["branch"] or _default_branch(git)
        git("fetch", "-q", "--depth=1", "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}", auth=True)
        target = f"refs/remotes/origin/{branch}"
        new = git("rev-parse", target)
        old = git.head()
        if old == new:
            return Result(True, False, old, new, message=f"уже последняя ({new[:7]})")
        missing = [p for p in REQUIRED if _file_at(git, target, p) is None]
        if missing:
            return Result(False, old=old, new=new,
                          message="в репозитории нет " + ", ".join(missing)
                          + " — это точно репозиторий бота? Обновление отменено")
        req_path = os.path.join(root, "requirements.txt")
        try:
            with open(req_path, encoding="utf-8") as f:
                req_before = f.read()
        except OSError:
            req_before = None
        # reset --hard перезаписывает и файлы, загруженные когда-то вручную;
        # checkout на их месте отказался бы работать
        git("symbolic-ref", "HEAD", f"refs/heads/{branch}")
        git("reset", "-q", "--hard", target)
        git("branch", "-q", f"--set-upstream-to=origin/{branch}", branch)
    except subprocess.TimeoutExpired:
        return Result(False, message="GitHub не ответил вовремя")
    except (RuntimeError, OSError) as exc:
        return Result(False, message=_friendly(str(exc)))

    try:
        with open(req_path, encoding="utf-8") as f:
            deps = f.read() != req_before
    except OSError:
        deps = False
    msg = f"обновлено {old[:7] or '—'} → {new[:7]}"
    if deps and install_deps:
        err = install_requirements(root)
        msg += " · библиотеки обновлены" if not err else f" · pip: {err}"
    return Result(True, True, old, new, deps, msg)


def restart_process() -> None:
    """Перезапускает бота в том же процессе — с уже новым кодом."""
    os.environ["BOT_JUST_UPDATED"] = "1"
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable] + sys.argv)
