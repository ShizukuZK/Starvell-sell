import asyncio
import json
import time

from core import instance


def test_lock_blocks_second_copy(tmp_path):
    path = str(tmp_path / "instance.lock")

    async def main():
        a = instance.InstanceLock(path)
        b = instance.InstanceLock(path)
        assert await a.acquire(wait=1)
        assert not await b.acquire(wait=0.5)      # живая копия держит замок
        await a.release()
        assert await b.acquire(wait=1)            # отпустили — можно
        await b.release()

    asyncio.run(main())


def test_stale_lock_is_taken(tmp_path):
    path = tmp_path / "instance.lock"
    path.write_text(json.dumps({"owner": "dead", "ts": time.time() - 600}))

    async def main():
        lock = instance.InstanceLock(str(path))
        assert await lock.acquire(wait=1)
        await lock.release()

    asyncio.run(main())
