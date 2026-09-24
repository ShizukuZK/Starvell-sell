import json

from core.storage import Store


def test_env_overrides_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STARVELL_SESSION", "cookie-from-env")
    monkeypatch.setenv("TG_ADMINS", "111, 222")
    monkeypatch.setenv("DRY_RUN", "true")
    store = Store()
    assert store.get("starvell_session") == "cookie-from-env"
    assert store.get("tg_admins") == [111, 222]
    assert store.get("dry_run") is True
