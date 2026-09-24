import json

from core import backup
from core.storage import Store


def test_backup_roundtrip_keeps_local_secrets(tmp_path):
    src = tmp_path / "pc"
    dst = tmp_path / "cloud"
    src.mkdir()
    dst.mkdir()
    (src / "settings.json").write_text(json.dumps({
        "starvell_session": "PC-SECRET", "kosell_api_key": "PC-KEY",
        "lots_markup_percent": 77, "min_quantity": 4}), encoding="utf-8")
    (src / "mappings.json").write_text(json.dumps([{"key": "peak", "game": "PEAK"}]), encoding="utf-8")
    (src / "rentals.json").write_text(json.dumps({"o1": {"login": "l"}}), encoding="utf-8")

    blob = backup.make_backup(str(src))
    assert b"PC-SECRET" not in blob and b"PC-KEY" not in blob

    class FakeStore:
        settings = {"starvell_session": "CLOUD-SECRET", "tg_admins": [1]}
        state = {"env_seen": {"X": "y"}}
        loaded = False

        def load(self):
            self.loaded = True

    st = FakeStore()
    restored = backup.restore_backup(blob, st, str(dst))
    assert "mappings.json" in restored and st.loaded
    settings = json.loads((dst / "settings.json").read_text(encoding="utf-8"))
    assert settings["starvell_session"] == "CLOUD-SECRET"
    assert settings["lots_markup_percent"] == 77 and settings["tg_admins"] == [1]
    assert json.loads((dst / "rentals.json").read_text(encoding="utf-8")) == {"o1": {"login": "l"}}


def test_rejects_foreign_zip(tmp_path):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "x")
    try:
        backup.restore_backup(buf.getvalue(), Store(), str(tmp_path))
    except ValueError as exc:
        assert "не резервная копия" in str(exc)
    else:
        raise AssertionError("чужой архив принят")
