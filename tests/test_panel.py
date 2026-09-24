"""Офлайн-прогон всех экранов и действий панели на реальных данных."""
import asyncio, sys, json, re, copy, time
from pathlib import Path
FIX = Path(__file__).parent / "fixtures"
from core.log import setup_logging; setup_logging("ERROR")
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from core.storage import Store
from core.engine import Engine
from core.poller import Poller
from core.stats import SalesLog
from core.maintenance import Maintenance
from core.starvell import StarvellError
from lots.sync import OffersCache
from tgpanel.panel import Panel, Ctx
from tgpanel import views

SNAP = json.load(open(str(FIX / 'kosell_snapshot.json'), encoding='utf-8'))
OFFERS = json.load(open(str(FIX / 'his_offers.json'), encoding='utf-8'))

class FakeSV:
    my_username = "Miluoky"; my_user_id = 215890; session_cookie = "x"
    def __init__(self): self.calls = []; self.offers = copy.deepcopy(OFFERS); self.next_id = 400000
    async def get_all_my_offers(self, cats): return copy.deepcopy(self.offers)
    async def get_my_offers(self, cid): return copy.deepcopy(self.offers)
    async def partial_update_offer(self, offer, **ch):
        self.calls.append(("partial", offer["id"], ch))
        for o in self.offers:
            if o["id"] == offer["id"]: o.update(ch)
        return {}
    async def set_offer_price(self, o, p): return await self.partial_update_offer(o, price=p)
    async def set_offer_availability(self, o, a): return await self.partial_update_offer(o, availability=a)
    async def set_offer_active(self, o, a): return await self.partial_update_offer(o, isActive=a)
    async def delete_offer(self, offer):
        self.calls.append(("delete", offer["id"])); self.offers = [o for o in self.offers if o["id"] != offer["id"]]
    async def bump_offers(self, g, c):
        self.calls.append(("bump", g, c))
        if g == 16: raise StarvellError("cd", status=400, message="cooldown", code="OFFERS_BUMP_COOLDOWN")
        return {}
    async def get_orders(self, status=None): return []
    async def create_offer(self, payload):
        self.calls.append(("create", payload["descriptions"]["rus"]["briefDescription"]))
        t = payload["descriptions"]["rus"]["briefDescription"]
        from core.textfit import starvell_len
        if starvell_len(t) > 100:
            raise StarvellError("x", status=400, message="briefDescription must be shorter than or equal to 100 characters")
        self.next_id += 1
        self.offers.append({"id": self.next_id, "publicId": f"p{self.next_id}", "price": payload["price"],
                            "availability": payload["availability"], "isActive": True, "moderationStatus": "PENDING",
                            "categoryId": payload["categoryId"], "subCategoryId": payload["subCategoryId"], "gameId": 16,
                            "descriptions": payload["descriptions"], "attributes": []})
        return {"id": self.next_id, "publicId": f"p{self.next_id}"}
    async def get_user_info(self): return {"id": 215890}
    async def get_category_limit(self, cid): return 100

class FakeKS:
    api_key = "k"
    async def products(self, search=None, currency="RUB"): return copy.deepcopy(SNAP["products"])
    async def balance(self): return SNAP["balance"]
    async def guard_code(self, uid): return {"code": "AB12C", "expires_in": 25}
    async def calculate_price(self, *a): return {"total_rub": 0}
    async def close(self): pass

class FakeMsg:
    def __init__(self): self.edits = []; self.sent = []
    async def edit_text(self, text, **kw): self.edits.append((text, kw.get("reply_markup"))); return self
    async def answer(self, text, **kw): self.sent.append((text, kw.get("reply_markup"))); return self

class FakeCall:
    def __init__(self, data, msg): self.data = data; self.message = msg; self.from_user = type("U",(object,),{"id":1})()
    async def answer(self, *a, **k): pass

# ---------- проверки экрана
ROUTES = None
problems = []
seen_cbs = set()
def check(name, screen):
    if screen is None: return
    text, markup = screen
    if len(text) > 4096: problems.append(f"{name}: текст {len(text)} > 4096")
    for tag in ("b", "i", "code", "pre"):
        if text.count(f"<{tag}>") != text.count(f"</{tag}>"):
            problems.append(f"{name}: несбалансирован <{tag}>")
    for row in markup.inline_keyboard:
        for btn in row:
            cb = btn.callback_data; seen_cbs.add(cb)
            if len(cb.encode()) > 64: problems.append(f"{name}: callback {len(cb.encode())}б {cb}")
            if cb.partition(":")[0] not in ROUTES: problems.append(f"{name}: нет маршрута для «{cb}»")

async def main():
    global ROUTES
    store = Store(); store.settings.update({"tg_admins": [1], "dry_run": True, "min_quantity": 3,
                                           "lots_markup_percent": 50, "lots_commission_percent": 10,
                                           "lots_batch_pause": 0.01})
    sv, ks = FakeSV(), FakeKS()
    stats = SalesLog("storage/sales.json")
    stats.record(order_id="o1", game="Palworld", quantity=5, hours=5, revenue_rub=17.2, cost_rub=4.2)
    stats.record(order_id="o2", game="PEAK", quantity=3, hours=3, revenue_rub=10.3, cost_rub=0.5, kind="extend")
    offers = OffersCache()
    engine = Engine(store, sv, ks, stats=stats)
    poller = Poller(store, sv, engine)
    from core.demand import Demand
    import shutil; shutil.copy(str(FIX / 'demand_snapshot.json'), 'storage/demand.json')
    demand = Demand('storage/demand.json')
    async def fake_refresh(products, **kw):
        prog = kw.get("progress")
        if prog: await prog(1, 1, "x", 1, 0)
        return {"games": 421, "no_appid": 3, "updated": 421, "failed": 0}
    demand.refresh = fake_refresh
    maint = Maintenance(store, sv, engine, offers, demand)
    panel = Panel(store, sv, ks, poller, engine, maint, offers, stats, demand)
    ROUTES = panel._routes
    store.add_rental("555", {"rental_uid": "a1b2c3d4-0000-1111-2222-333344445555", "product_id": 63,
                             "product_name": "Palworld", "login": "steam_login_1", "hours": 5,
                             "started_ts": time.time(), "expires_ts": time.time() + 9000, "chat_id": "c"})
    fsm = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))

    async def go(data):
        msg = FakeMsg(); call = FakeCall(data, msg)
        prefix, _, arg = data.partition(":")
        screen = await ROUTES[prefix](Ctx(call, fsm, msg), arg)
        check(data, screen)
        return screen, msg

    offer_id = str(OFFERS[0]["id"])
    screens = ["home", "tog_home:dry_run", "tog_home:dry_run", "lots:0", "lots:1", "lots:3", "lots_r",
               f"lot:{offer_id}", f"lotact:{offer_id}:del", "new", "new_n:5", "new_ex",
               "rent", "rentc:a1b2c3d4-0000-1111-2222-333344445555",
               "stats:day", "stats:week", "stats:month", "stats:all",
               "maps:0", "maps:1", "map_add", "set", "help", "txt", "txt:delivery", "txte:delivery",
               "tpl:lots_title_template", "tpl:lots_description_template", "lots_price", "noop",
               "rot", "setg:demand", "ver", "bak", "bak:drop", "bak:apply"]
    from tgpanel.schema import GROUPS, FIELDS
    screens += [f"setg:{g}" for g, _, _ in GROUPS]
    screens += [f"ask:{k}:sale" for k in FIELDS]
    screens += [f"tog:{k}:sale" for k in FIELDS if FIELDS[k].kind == "bool"] * 2  # туда-обратно
    ok = 0
    for s in screens:
        try:
            await go(s); ok += 1
        except Exception as exc:
            problems.append(f"{s}: ИСКЛЮЧЕНИЕ {type(exc).__name__}: {exc}")
    print(f"экранов отрисовано без ошибок: {ok} из {len(screens)}")

    # ---- карточка привязки (длинное имя)
    if not store.mappings:      # чистая папка: привязки строятся по живым лотам
        from lots.sync import sync_mappings
        await sync_mappings(store, await ks.products(), await offers.get(store, sv, force=True))
    assert store.mappings, "привязки не построились"
    m = store.mappings[0]
    await go(f"map:{m['id']}")

    # ---- действия с лотом
    await go(f"lotact:{offer_id}:toggle")
    assert ("partial", int(offer_id), {"isActive": False}) in sv.calls, sv.calls
    await go(f"lotact:{offer_id}:delok")
    assert ("delete", int(offer_id)) in sv.calls
    print("снять с продажи / удалить лот: OK")

    # ---- ввод значений через FSM
    from aiogram.types import Message
    async def feed(data, text):
        await go(data)
        class M(FakeMsg):
            pass
        m = M(); m.text = text
        await panel._on_value(m, fsm)
        return (m.sent[-1][0] if m.sent else "")
    r = await feed("ask:min_quantity:sale", "abc"); assert "нужно число" in r, r
    r = await feed("ask:min_quantity:sale", "0");   assert "не меньше 1" in r, r
    r = await feed("ask:min_quantity:sale", "4");   assert store.get("min_quantity") == 4 and "Сохранено" in r, r
    r = await feed("ask:tg_proxy:conn", "abc");     assert "формат" in r, r
    r = await feed("ask:tg_proxy:conn", "socks5://u:p@1.2.3.4:1080"); assert "перезапуска" in r
    r = await feed("ask:lots_title_template:tpl", "без плейсхолдера"); assert "{game}" in r, r
    r = await feed("txte:delivery", "Держи {login} / {password}"); assert store.texts["delivery"].startswith("Держи")
    r = await feed("map_add", "Моя Игра | Palworld"); assert store.find_mapping("Моя Игра")["product_id"] == 63
    lot2 = str(sv.offers[0]["id"])
    r = await feed(f"lotask:{lot2}:price", "4,5"); assert ("partial", int(lot2), {"price": 4.5}) in sv.calls, sv.calls[-1]
    r = await feed(f"lotask:{lot2}:stock", "1000"); assert "от 0 до 999" in r, r
    print("ввод и проверка значений: OK")

    # ---- фоновые задачи
    async def run_job(data):
        screen, msg = await go(data)
        if panel._job:
            await panel._job
        return msg.edits[-1][0] if msg.edits else (screen[0] if screen else "")

    store.settings["min_quantity"] = 3
    await go("new")
    n_plan = len(panel._plan)
    last = await run_job("new_go:7")
    created = [c for c in sv.calls if c[0] == "create"]
    assert len(created) == 7 and "Создано: <b>7</b>" in last, last
    from core.textfit import starvell_len
    assert all(starvell_len(t) <= 100 for _, t in created)
    await go("new")
    assert len(panel._plan) == n_plan - 7, (len(panel._plan), n_plan)
    print(f"создание лотов: 7 созданы, дублей в новом плане нет ({n_plan} → {len(panel._plan)})")

    last = await run_job("lots_bump")
    assert "Поднято" in last and "перерыв" in last, last
    last = await run_job("lots_link")
    assert "Новых привязок" in last, last
    last = await run_job("lots_stock")
    assert "Изменено лотов" in last, last
    await go("lots_price")
    last = await run_job("lots_price_go")
    assert "Обновлено цен" in last, last
    print("поднятие / привязка / остатки / цены: OK")

    # ---- остановка задачи
    panel._plan = (await __import__('lots.autocreate', fromlist=['x']).build_plan(store, ks, live_offers=sv.offers))
    store.settings["lots_batch_pause"] = 0.2
    screen, msg = await go("new_go:30")
    await asyncio.sleep(0.5)
    await go("job_stop")
    await panel._job
    stop_text = msg.edits[-1][0]
    made = len([c for c in sv.calls if c[0] == "create"]) - 7
    assert "Остановлено" in stop_text and made < 30, (made, stop_text[:200])
    print(f"кнопка «Остановить»: OK (успело создаться {made} из 30)")

    # ---- спрос, места, ротация
    sv.offers = copy.deepcopy(OFFERS)          # снова 27 лотов
    for o in sv.offers: o["viewsCount"] = 0
    store.mappings.clear(); store.save_mappings()
    screen, _ = await go("new")
    assert "Свободных мест: <b>73</b>" in screen[0], screen[0][:400]
    assert len(panel._plan) == 73, len(panel._plan)
    ranks = [p["rank"] for p in panel._plan]
    assert ranks == sorted(ranks, reverse=True), "план не отсортирован по спросу"
    top = panel._plan[0]
    print(f"мастер: 73 места из 100, первым идёт {top['product_name']} "
          f"(оценка {top['score']}, {top['tier']}, цена {top['price_rub']} ₽)")
    hot = [p for p in panel._plan if p["tier"] == "hot"]
    assert hot and all(p["price_rub"] >= 5.0 for p in hot), "хиты дешевле порога"
    pop = [p for p in panel._plan if p["tier"] == "popular"]
    assert all(p["price_rub"] >= 4.0 for p in pop), "популярные дешевле порога"
    last = await run_job("dem")
    assert "хитов" in last, last

    store.settings.update({"rotate_after_days": 0, "rotate_max_views": 5})
    screen, _ = await go("rot")
    assert "Заменю" in screen[0], screen[0][:300]
    n_pairs = len(panel._rotation)
    dels_before = len([c for c in sv.calls if c[0] == "delete"])
    last = await run_job("rot_go")
    dels = len([c for c in sv.calls if c[0] == "delete"]) - dels_before
    assert dels == n_pairs and f"Заменено лотов: <b>{n_pairs}</b>" in last, (dels, n_pairs, last[:200])
    assert len(sv.offers) == 27, len(sv.offers)          # место в место
    assert int(store.state.get("deleted_by_panel", 0)) >= n_pairs
    print(f"ротация: заменено {n_pairs} слабых лотов, число лотов не изменилось (27)")

    # ---- сигнал о пропаже лотов не срабатывает на удаления из панели
    alerts = []
    engine._notify_admin = lambda t: alerts.append(t)
    maint.last_offer_count = 27
    await maint._watch_count(27 - 0)              # удаления панели уже учтены
    store.state["deleted_by_panel"] = 0
    await maint._watch_count(20)                  # 7 пропали сами
    assert len(alerts) == 1 and "было 27, сейчас 20" in alerts[0], alerts
    print("сигнал о пропаже лотов: OK")

    # ---- автопересчёт цен по спросу
    from lots import autocreate as _ac
    _orig_apply = _ac.apply_prices
    _ac.apply_prices = lambda sv_, ch, **kw: _orig_apply(sv_, ch, **{**kw, "pause": 0})
    store.settings["auto_reprice_enabled"] = True
    maint.last_reprice_ts = 0
    for o in sv.offers: o["price"] = "3.00000"      # как старые лоты с плоской ценой
    offers.invalidate()
    before = len([c for c in sv.calls if c[0] == "partial"])
    res = await maint.reprice_tick()
    changed = len([c for c in sv.calls if c[0] == "partial"]) - before
    assert res and res["updated"] == changed and changed > 0, (res, changed)
    print(f"автопересчёт цен: изменено {changed} из {len(sv.offers)} (хиты и популярные подорожали)")
    res2 = await maint.reprice_tick()
    assert res2 is None, "повторный пересчёт должен ждать интервал"
    _ac.apply_prices = _orig_apply

    # ---- все кнопки на всех экранах ведут на существующий маршрут
    print(f"уникальных кнопок проверено: {len(seen_cbs)}")
    print("\n" + ("ПРОБЛЕМЫ:\n  " + "\n  ".join(problems) if problems else "✅ проблем не найдено"))

def test_panel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(main())
