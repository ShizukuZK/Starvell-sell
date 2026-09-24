import asyncio, sys
from pathlib import Path
FIX = Path(__file__).parent / "fixtures"
from core.log import setup_logging; setup_logging("WARNING")
from core.storage import Store
from core.engine import Engine
BRIEF="🎮 АРЕНДА [PEAK] ⚡ АВТОВЫДАЧА 24/7 ⚡ ⏱️ 1 ШТ = 1 ЧАС (ОТ 3-Х ШТ)"
class SV:
    def __init__(self): self.sent=[]; self.refunds=[]; self.confirms=[]
    async def get_order(self,o): return {"order":{"buyerId":1},"chat":{"id":"c"}}
    async def send_message(self,c,t): self.sent.append(t)
    async def refund_order(self,o): self.refunds.append(o)
    async def confirm_order(self,o): self.confirms.append(o)
class KS:
    rented=0
    async def products(self,search=None,currency="RUB"):
        return [{"id":29,"name":"PEAK","price_per_hour_rub":3.4,"available_accounts":2,"min_hours":1,"max_hours":720}]
    async def rent(self,*a,**k): KS.rented+=1; raise AssertionError("в тестовом режиме аренда запрещена")
    async def extend(self,*a,**k): raise AssertionError("в тестовом режиме продление запрещено")
async def main():
    store=Store(); store.settings.update({"dry_run":True,"notify_sales":False,"auto_confirm_order":True})
    sv=SV(); eng=Engine(store,sv,KS())
    order={"id":"x1","shortId":"X1","status":"CREATED","quantity":5,"totalPrice":2500,"buyerId":1,
           "offerDetails":{"subCategory":{"name":"Аренда"},
                           "descriptions":{"rus":{"briefDescription":BRIEF,"description":""}}}}
    await eng.handle_order(order)
    assert KS.rented==0, "аренда не должна вызываться"
    assert sv.refunds==[] and sv.confirms==[], (sv.refunds, sv.confirms)
    assert sv.sent==[], "в чат в тестовом режиме писать нельзя"
    assert len(store.active_rentals("1"))==0, "аренда не должна записываться"
    assert len(store.mappings)==1, "привязка всё равно должна создаться"
    # ниже минимума — возврат тоже не отправляется
    o2=dict(order, id="x2", quantity=1); o2["shortId"]="X2"
    await eng.handle_order(o2)
    assert sv.refunds==[], sv.refunds
    print("DRY-RUN OK — ни одной платной операции, привязок:", len(store.mappings))
def test_dry_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(main())
