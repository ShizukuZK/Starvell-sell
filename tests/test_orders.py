import asyncio, sys, time
from pathlib import Path
FIX = Path(__file__).parent / "fixtures"
from core.log import setup_logging; setup_logging("WARNING")
from core.storage import Store
from core.engine import Engine

BRIEF = "🎮 АРЕНДА [PEAK] ⚡ АВТОВЫДАЧА 24/7 ⚡ ⏱️ 1 ШТ = 1 ЧАС (ОТ 3-Х ШТ) 🚀 СВОБОДЕН? ПИШИ !наличие"

class FakeSV:
    def __init__(self): self.sent=[]; self.refunds=[]; self.confirms=[]; self.details={}
    async def get_order(self, oid): return self.details[oid]
    async def send_message(self, c,t): self.sent.append((c,t)); return {}
    async def refund_order(self, oid): self.refunds.append(oid); return {}
    async def confirm_order(self, oid): self.confirms.append(oid); return {}

class FakeKS:
    def __init__(self): self.n=0; self.fail=False; self.free=2; self.next_av=None
    async def products(self, search=None, currency="RUB"):
        return [{"id":29,"name":"PEAK","price_per_hour_rub":3.4,"available_accounts":self.free,
                 "next_available":self.next_av,"min_hours":1,"max_hours":720}]
    async def rent(self,pid,h,c,k=None):
        if self.fail: return None,"no_accounts_available"
        self.n+=1
        return {"rental_uid":f"u{self.n}","steam_login":f"log{self.n}","steam_password":"pw",
                "product_name":"PEAK","expires_at":None}, None
    async def credentials(self,u): return None
    async def guard_code(self,u): return {"code":"ZZ999","expires_in":25}
    async def extend(self,u,h,c): return {"new_expires_at":None}, None

def sale(oid, qty, buyer=4242, brief=BRIEF):
    return {"id":oid,"shortId":oid.upper(),"status":"CREATED","quantity":qty,
            "totalPrice":qty*500,"buyerId":buyer,
            "offerDetails":{"subCategory":{"name":"Аренда"},
                            "descriptions":{"rus":{"briefDescription":brief,"description":""}}}}

async def main():
    store=Store(); store.settings.update({"notify_sales":False,"hours_per_unit":1,
                                          "min_quantity":3,"auto_confirm_order":True})
    sv=FakeSV(); ks=FakeKS(); eng=Engine(store,sv,ks)

    # 1. заказ 5 шт -> аренда на 5 часов, привязка создана автоматически
    sv.details["o1"]={"order":{"buyerId":4242,"buyer":{"username":"vasya"}},"chat":{"id":"c1"}}
    await eng.handle_order(sale("o1",5))
    assert ks.n==1, ks.n
    r=store.active_rentals("4242")[0]
    assert r["hours"]==5, r
    assert len(store.mappings)==1 and store.mappings[0]["product_id"]==29, store.mappings
    assert store.mappings[0]["auto"] is True
    assert sv.confirms==["o1"]

    # 2. заказ ниже минимума -> возврат, аренды нет
    sv.details["o2"]={"order":{"buyerId":777},"chat":{"id":"c2"}}
    await eng.handle_order(sale("o2",2,buyer=777))
    assert sv.refunds==["o2"], sv.refunds
    assert "Минимальный заказ" in sv.sent[-1][1]
    assert not store.active_rentals("777")

    # 3. повторный заказ той же игры -> продление на 3 часа
    sv.details["o3"]={"order":{"buyerId":4242},"chat":{"id":"c1"}}
    await eng.handle_order(sale("o3",3))
    assert ks.n==1, "новая аренда не нужна"
    assert "продлена" in sv.sent[-1][1]

    # 4. не-арендный лот игнорируется
    before=len(sv.sent)
    sv.details["o4"]={"order":{"buyerId":999},"chat":{"id":"c9"}}
    await eng.handle_order(sale("o4",5,buyer=999,brief="🎁✨ Награды Steam | 1–12 штук ✨🎁"))
    assert len(sv.sent)==before, "нераспознанный лот не должен обрабатываться"
    assert not store.is_handled("o4"), "такой заказ не помечаем — вдруг добавят шаблон"

    # 5. !наличие — свободен
    await eng.handle_message("c1","4242","!наличие PEAK")
    assert "свободен прямо сейчас" in sv.sent[-1][1], sv.sent[-1]

    # 6. !наличие — занят, с временем
    ks.free=0; ks.next_av=time.time()+3900
    await eng.handle_message("c1","4242","!наличие PEAK")
    assert "занят" in sv.sent[-1][1] and ("через 64 мин" in sv.sent[-1][1] or "через 65 мин" in sv.sent[-1][1]), sv.sent[-1]

    # 7. !наличие — неизвестная игра
    await eng.handle_message("c1","4242","!наличие Неведомая Игра")
    assert "Не нашёл" in sv.sent[-1][1], sv.sent[-1]

    # 8. !код
    await eng.handle_message("c1","4242","!код")
    assert "ZZ999" in sv.sent[-1][1], sv.sent[-1]

    # 9. сбой аренды -> возврат
    ks.fail=True
    sv.details["o5"]={"order":{"buyerId":555},"chat":{"id":"c5"}}
    await eng.handle_order(sale("o5",4,buyer=555))
    assert "o5" in sv.refunds, sv.refunds

    print("E2E-2 OK | сообщений:", len(sv.sent), "| возвратов:", sv.refunds)
def test_orders(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(main())
