"""Перенос данных между копиями бота (ПК → хостинг и обратно).

В архив попадает всё, что бот накопил: настройки (без ключей и токенов),
привязки лотов, активные аренды, тексты, статистика, спрос. Ключи в архив
не кладутся — на новом месте они уже заданы своими (переменными окружения
или settings.json), и при загрузке архива остаются как есть.
"""
from __future__ import annotations

import io
import json
import os
import time
import zipfile
from typing import Any, Dict, List

from core.storage import DATA_DIR

FILES = ("settings.json", "mappings.json", "rentals.json", "state.json",
         "texts.json", "sales.json", "demand.json", "starvell_catalog.json")

# что всегда остаётся от текущей копии
KEEP_SETTINGS = ("starvell_session", "kosell_api_key", "tg_token", "tg_admins",
                 "tg_proxy", "proxy_url", "tg_ca_bundle", "github_token", "github_repo")
KEEP_STATE = ("env_seen", "my_user_id", "update_notified")

MAX_SIZE = 20 * 1024 * 1024


def make_backup(data_dir: str = DATA_DIR) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            path = os.path.join(data_dir, name)
            if not os.path.exists(path):
                continue
            if name == "settings.json":
                with open(path, encoding="utf-8") as f:
                    settings = json.load(f)
                for key in KEEP_SETTINGS:
                    settings.pop(key, None)
                z.writestr(name, json.dumps(settings, ensure_ascii=False, indent=2))
            else:
                z.write(path, name)
        z.writestr("backup.json", json.dumps({"created": time.time(), "files": list(FILES)}))
    return buf.getvalue()


def backup_name() -> str:
    return time.strftime("kosell-backup-%Y%m%d-%H%M.zip")


def restore_backup(blob: bytes, store: Any, data_dir: str = DATA_DIR) -> List[str]:
    """Раскладывает архив по папке данных и перечитывает хранилище.

    Возвращает список восстановленных файлов. Бросает ValueError, если
    архив не похож на резервную копию бота.
    """
    if len(blob) > MAX_SIZE:
        raise ValueError("файл слишком большой для резервной копии")
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise ValueError("это не zip-архив") from None
    names = set(z.namelist())
    if "backup.json" not in names:
        raise ValueError("это не резервная копия бота (нет backup.json)")

    payload: Dict[str, Any] = {}
    for name in FILES:
        if name in names:
            try:
                payload[name] = json.loads(z.read(name).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError(f"повреждён файл {name}") from None
    if not payload:
        raise ValueError("архив пустой")

    if isinstance(payload.get("settings.json"), dict):
        settings = payload["settings.json"]
        for key in KEEP_SETTINGS:
            settings.pop(key, None)
            if key in store.settings:
                settings[key] = store.settings[key]
    if isinstance(payload.get("state.json"), dict):
        for key in KEEP_STATE:
            payload["state.json"].pop(key, None)
            if key in store.state:
                payload["state.json"][key] = store.state[key]

    os.makedirs(data_dir, exist_ok=True)
    for name, data in payload.items():
        path = os.path.join(data_dir, name)
        tmp = f"{path}.restore"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    store.load()
    return sorted(payload)
