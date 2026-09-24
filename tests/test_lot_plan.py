import asyncio, sys
from pathlib import Path
FIX = Path(__file__).parent / "fixtures"
from core.log import setup_logging; setup_logging("ERROR")
from core.storage import Store
from core.textfit import starvell_len
from lots import autocreate

class FakeKS:
    async def products(self, search=None, currency="RUB"):
        return [
            {"id":2,"name":"Rust","price_per_hour_rub":3.5,"available_accounts":12,"min_hours":1,"max_hours":720},
            {"id":9,"name":"Dota 2","price_per_hour_rub":1.2,"available_accounts":5,"min_hours":1,"max_hours":168},
            {"id":77,"name":"Sons Of The Forest","price_per_hour_rub":2.0,"available_accounts":3,"min_hours":1,"max_hours":720},
            {"id":78,"name":"Нет в наличии","price_per_hour_rub":2.0,"available_accounts":0,"min_hours":1,"max_hours":720},
            {"id":79,"name":"Только от 10 часов","price_per_hour_rub":2.0,"available_accounts":4,"min_hours":10,"max_hours":720},
            {"id":80,"name":"Уже выставлена","price_per_hour_rub":2.0,"available_accounts":4,"min_hours":1,"max_hours":720},
        ]

async def main():
    store=Store()
    store.settings.update({"hours_per_unit":1,"min_quantity":3,"lots_markup_percent":60,
                           "lots_commission_percent":8,"lots_min_price_rub":3,"lots_round_to":0.01})
    live=[{"id":1,"descriptions":{"rus":{"briefDescription":"🎮 АРЕНДА [Уже выставлена] ⚡"}}}]
    plan = await autocreate.build_plan(store, FakeKS(), live_offers=live)
    names={p['product_name'] for p in plan}
    for p in plan:
        print(f"  {p['product_name']:<20} {p['price_rub']:>6.2f}₽/шт себест {p['cost_rub']:.2f} "
              f"до {p['units']} шт · 👤{p['available']} -> {p['game_name']}")
    assert names=={"Rust","Dota 2","Sons Of The Forest"}, names
    rust=[p for p in plan if p['product_name']=='Rust'][0]
    assert abs(rust['price_rub']-6.09)<0.02, rust['price_rub']
    assert rust['units']==720 and [p for p in plan if p['product_name']=='Dota 2'][0]['units']==168
    assert all(starvell_len(p['title'])<=100 for p in plan)
    assert "(ОТ 3-Х ШТ)" in rust['title'] and "Минимальный заказ — 3 шт" in rust['description']
    pl=autocreate.build_offer_payload(rust, basic_attributes=[{"id":"a","optionId":"b"}],
                                      numeric_attributes=[{"id":"c","numericValue":1}],
                                      availability=rust['units'])
    assert pl["basicAttributes"] and pl["numericAttributes"] and "attributes" not in pl
    assert pl["availability"]==720 and pl["price"]=="6.09"
    print("PLAN OK — без стока, дублей и «от 10 часов» в плане нет")
def test_lot_plan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(main())
