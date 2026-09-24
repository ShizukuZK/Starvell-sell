"""Самообновление из GitHub: проверяем на локальном репозитории."""
import os
import subprocess

import pytest

import core.selfupdate as S

pytestmark = pytest.mark.skipif(S.shutil.which("git") is None, reason="нет git")


def sh(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, check=True, capture_output=True)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


@pytest.fixture
def origin(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    sh(src, "init", "-q", "-b", "main")
    write(str(src / "main.py"), "print('v1')\n")
    write(str(src / "version.py"), 'VERSION = "1.0.0"\n')
    write(str(src / "requirements.txt"), "aiohttp\n")
    write(str(src / ".gitignore"), "storage/\n")
    sh(src, "add", "-A")
    sh(src, "commit", "-q", "-m", "v1")
    for name in ("GIT_ADDRESS", "GIT_REPO", "GIT_REPO_ADDRESS", "BRANCH", "GIT_BRANCH",
                 "USERNAME", "GIT_USERNAME", "ACCESS_TOKEN", "GIT_ACCESS_TOKEN",
                 "GITHUB_TOKEN", "AUTO_UPDATE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_ADDRESS", str(src))
    return src


def test_bootstrap_uploaded_folder_and_keep_storage(origin, tmp_path):
    # на сервер файлы залиты вручную: без .git, со старым кодом и данными
    bot = tmp_path / "bot"
    write(str(bot / "main.py"), "print('old')\n")
    write(str(bot / "version.py"), 'VERSION = "0.9"\n')
    write(str(bot / "requirements.txt"), "aiohttp\n")
    write(str(bot / "storage" / "settings.json"), '{"secret": 1}')
    write(str(bot / "venv" / "junk.txt"), "x")

    r = S.update(str(bot), install_deps=False)
    assert r.ok and r.changed, r.message
    assert (bot / "main.py").read_text() == "print('v1')\n"
    assert (bot / "storage" / "settings.json").read_text() == '{"secret": 1}'
    assert (bot / "venv" / "junk.txt").exists()          # git clean не делаем
    assert not r.deps

    r = S.update(str(bot), install_deps=False)
    assert r.ok and not r.changed

    # новая версия на GitHub
    write(str(origin / "main.py"), "print('v2')\n")
    write(str(origin / "requirements.txt"), "aiohttp\naiogram\n")
    sh(origin, "commit", "-qam", "v2")
    (bot / "main.py").write_text("print('правка на сервере')\n")
    r = S.update(str(bot), install_deps=False)
    assert r.ok and r.changed and r.deps
    assert (bot / "main.py").read_text() == "print('v2')\n"
    assert (bot / "storage" / "settings.json").exists()


def test_refuses_foreign_repo(origin, tmp_path):
    sh(origin, "rm", "-q", "main.py")
    sh(origin, "commit", "-qm", "не бот")
    bot = tmp_path / "bot"
    write(str(bot / "main.py"), "print('old')\n")
    r = S.update(str(bot), install_deps=False)
    assert not r.ok and "main.py" in r.message
    assert (bot / "main.py").read_text() == "print('old')\n"


def test_bad_branch_and_settings(origin, tmp_path, monkeypatch):
    monkeypatch.setenv("BRANCH", "nope")
    bot = tmp_path / "bot"
    bot.mkdir()
    r = S.update(str(bot), install_deps=False)
    assert not r.ok and "ветки" in r.message
    monkeypatch.setenv("AUTO_UPDATE", "0")
    assert S.configured() and not S.auto_enabled()
    monkeypatch.delenv("GIT_ADDRESS")
    assert not S.configured()


def test_clean_url():
    assert S.clean_url("https://me:tok@github.com/me/bot") == "https://github.com/me/bot.git"
    assert S.clean_url("https://github.com/me/bot.git") == "https://github.com/me/bot.git"


def test_version_screen_button():
    from tgpanel import views
    text, markup = views.version_screen({"current": "2.1.0", "repo": "me/bot",
                                         "latest": {"version": "2.2.0", "notes": ""},
                                         "newer": True, "self_update": True,
                                         "note": "✅ Код уже свежий"})
    buttons = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "upd" in buttons and "Обновить сейчас" in text and "Код уже свежий" in text
    text, markup = views.version_screen({"current": "2.1.0", "repo": "me/bot"})
    assert "upd" not in [b.callback_data for r in markup.inline_keyboard for b in r]
    assert "Перезапускаюсь" in views.restarting_screen("обновлено a → b")[0]
