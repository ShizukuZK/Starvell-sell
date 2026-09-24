import asyncio

from aiohttp import web

import core.updates as U


class FakeStore:
    def __init__(self, repo):
        self.repo = repo
        self.state = {}

    def get(self, key, default=None):
        return {"github_repo": self.repo, "update_check_enabled": True}.get(key, default)

    def save_state(self):
        pass


def test_versions():
    assert U.parse_version("v2.10.1") > U.parse_version("2.9.9")
    assert U.is_newer("v2.1.1", "2.1.0")
    assert not U.is_newer("2.1.0", "2.1.0")
    assert U.normalize_repo("https://github.com/me/bot.git") == "me/bot"
    assert U.normalize_repo("me/bot") == "me/bot"
    assert U.normalize_repo("garbage") == ""


def test_check_and_notify_once(monkeypatch, unused_port=8766):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    async def main():
        async def latest(request):
            assert request.match_info["name"] == "bot"
            return web.json_response({"tag_name": "v99.0.0",
                                      "html_url": "https://github.com/me/bot/releases/tag/v99.0.0",
                                      "body": "- новое"})
        app = web.Application()
        app.router.add_get("/repos/me/{name}/releases/latest", latest)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(U, "API", f"http://127.0.0.1:{port}/repos/{{repo}}/releases/latest")
        try:
            up = U.UpdateChecker(FakeStore("https://github.com/me/bot"))
            info = await up.check(force=True)
            assert info and info["version"] == "99.0.0"
            text = up.notice_text()
            assert text and "99.0.0" in text
            assert up.notice_text() is None          # второй раз не беспокоим
        finally:
            await runner.cleanup()

    asyncio.run(main())


def test_detect_repo(monkeypatch, tmp_path):
    monkeypatch.delenv("RAILWAY_GIT_REPO_OWNER", raising=False)
    monkeypatch.delenv("RAILWAY_GIT_REPO_NAME", raising=False)
    assert U.detect_repo() == ""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[core]\n\tbare = false\n[remote "origin"]\n\turl = https://github.com/slava/kosell-bot.git\n'
        '\tfetch = +refs/heads/*:refs/remotes/origin/*\n', encoding="utf-8")
    assert U.detect_repo() == "slava/kosell-bot"
    assert U.UpdateChecker(FakeStore("")).repo == "slava/kosell-bot"
    monkeypatch.setenv("RAILWAY_GIT_REPO_OWNER", "o")
    monkeypatch.setenv("RAILWAY_GIT_REPO_NAME", "n")
    assert U.detect_repo() == "o/n"
