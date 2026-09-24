"""Общие настройки тестов: корень проекта в sys.path, тихие логи."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

for var in ("STARVELL_SESSION", "KOSELL_API_KEY", "OPTSMM_API_KEY", "TG_TOKEN", "TG_ADMINS", "TG_PROXY",
            "PROXY_URL", "DRY_RUN", "GITHUB_REPO", "GITHUB_TOKEN", "DATA_DIR"):
    os.environ.pop(var, None)

import shutil  # noqa: E402

import pytest  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Каждый тест — в пустой папке со своим storage/ и каталогом Starvell."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("storage", exist_ok=True)
    shutil.copy(os.path.join(FIXTURES, "starvell_catalog.json"), "storage/starvell_catalog.json")
    yield tmp_path
