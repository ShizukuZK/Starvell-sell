import asyncio, sys
from pathlib import Path
FIX = Path(__file__).parent / "fixtures"
from core.log import setup_logging; setup_logging("WARNING")
from core.storage import Store
from core.poller import Poller

class FakeSV:
    my_user_id = 999
    def __init__(self): self.orders=[]; self.chats=[]
    async def get_orders(self, status=None): return self.orders
    async def get_chats(self): return self.chats
    async def keep_alive(self): return True

class FakeEngine:
    def __init__(self): self.handled=[]; self.msgs=[]
    async def handle_order(self, o): self.handled.append(o["id"])
    async def handle_message(self, c,a,t,**kw): self.msgs.append((c,a,t))
    async def check_rentals(self): pass
    async def admin(self, t): pass

async def main():
    store=Store(); store.state["handled_orders"]=[]; store.save_state()
    sv=FakeSV(); eng=FakeEngine(); p=Poller(store, sv, eng)

    sv.orders=[{"id":"old1"},{"id":"old2"}]
    await p._poll_orders()
    assert eng.handled==[] and store.is_handled("old1"), "старые заказы не должны выдаваться"

    sv.orders.append({"id":"new1"})
    await p._poll_orders()
    assert eng.handled==["new1"], eng.handled
    await p._poll_orders()
    assert eng.handled==["new1"], "непривязанный заказ не должен дёргаться каждый цикл"
    # после истечения отсрочки — повторная проверка (вдруг появилась привязка)
    p._defer_seconds = 0
    await p._poll_orders()
    assert eng.handled==["new1","new1"], eng.handled
    p._defer_seconds = 600

    sv.chats=[{"id":"c1","lastMessage":{"id":"m1","authorId":"555","content":"привет"}}]
    await p._poll_chats()
    assert eng.msgs==[], "первый проход чатов только синхронизируется"
    sv.chats=[{"id":"c1","lastMessage":{"id":"m2","authorId":"555","content":"!код"}}]
    await p._poll_chats()
    assert eng.msgs==[("c1","555","!код")], eng.msgs
    sv.chats=[{"id":"c1","lastMessage":{"id":"m3","authorId":"999","content":"мой ответ"}}]
    await p._poll_chats()
    assert len(eng.msgs)==1, "свои сообщения игнорируются"
    print("POLLER OK", p.status())
def test_poller(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(main())
