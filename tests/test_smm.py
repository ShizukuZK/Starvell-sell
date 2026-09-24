"""SMM-модуль: лот → ссылка → OptSMM → статус, и соседство с автоарендой."""
import asyncio

from core.log import setup_logging
from core.optsmm import OptSmmError
from core.poller import Poller
from core.smm import SmmEngine, extract_link, rule_matches
from core.storage import Store

setup_logging("WARNING")

SMM_BRIEF = "📣 ПОДПИСЧИКИ TELEGRAM ⚡ 1 ШТ = 100 ПОДПИСЧИКОВ"
RENT_BRIEF = "🎮 АРЕНДА [PEAK] ⚡ АВТОВЫДАЧА 24/7 ⏱️ 1 ШТ = 1 ЧАС (ОТ 3-Х ШТ)"


class FakeSV:
    def __init__(self):
        self.sent, self.refunds, self.details = [], [], {}

    async def get_order(self, oid):
        return self.details[oid]

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return {}

    async def refund_order(self, oid):
        self.refunds.append(oid)
        return {}


class FakeAPI:
    configured = True

    def __init__(self):
        self.bal, self.added, self.st, self.fail_add = 0.0, [], {}, None

    async def service(self, sid):
        if str(sid) == "1":
            return {"service": 1, "name": "TG подписчики", "rate": "50", "min": 100, "max": 10000}
        return None

    async def services(self, force=False):
        return [await self.service(1)]

    async def balance(self):
        return self.bal, "RUB"

    async def add(self, service, link, qty):
        if self.fail_add:
            raise OptSmmError(self.fail_add)
        self.added.append((service, link, qty))
        return 500 + len(self.added)

    async def status_many(self, ids):
        return {i: self.st.get(i, {"status": "In progress"}) for i in ids}


def sale(oid, qty, brief=SMM_BRIEF, buyer=77):
    return {"id": oid, "shortId": oid.upper(), "status": "CREATED", "quantity": qty,
            "totalPrice": qty * 3000, "basePrice": qty * 3000, "buyerId": buyer,
            "offerDetails": {"descriptions": {"rus": {"briefDescription": brief}}}}


def make():
    store = Store()
    store.settings.update({"notify_sales": False, "optsmm_api_key": "k"})
    sv, api = FakeSV(), FakeAPI()
    smm = SmmEngine(store, sv, api, path="storage/smm.json")
    smm.add_rule("подписчики, telegram", 1, 100, "tg")
    return store, sv, api, smm


def test_links():
    assert extract_link("вот https://t.me/chan.", "tg") == "https://t.me/chan"
    assert extract_link("@my_chan", "tg") == "https://t.me/my_chan"
    assert extract_link("t.me/abc", "any") == "https://t.me/abc"
    assert extract_link("https://vk.com/x", "tg") is None
    assert extract_link("привет", "any") is None
    assert rule_matches({"match": "подписчики, telegram"}, SMM_BRIEF)
    assert not rule_matches({"match": "подписчики, telegram"}, RENT_BRIEF)
    assert rule_matches({"match": r"re:подписчик\w+ telegram"}, SMM_BRIEF)


def test_full_flow_and_refund():
    async def run():
        store, sv, api, smm = make()
        sv.details["o1"] = {"order": {"buyerId": 77, "buyer": {"username": "bob"}}, "chat": {"id": "c1"}}

        # чужой лот (аренда) не трогаем
        assert await smm.handle_order(sale("r1", 5, RENT_BRIEF)) is False

        assert await smm.handle_order(sale("o1", 2)) is True
        o = smm.orders["o1"]
        assert o["state"] == "ASKED" and o["qty"] == 200 and store.is_handled("o1")
        assert "Количество: 200" in sv.sent[-1][1]

        # сообщение без ссылки -> подсказка, сообщение поглощено
        assert await smm.handle_message("c1", 77, "а когда?") is True
        assert "Не вижу" in sv.sent[-1][1]

        # ссылка, но баланса нет -> ждём баланс
        smm._last_hint.clear()
        assert await smm.handle_message("c1", 77, "@coolchan") is True
        assert o["state"] == "WAIT_BALANCE"

        api.bal = 100
        await smm.tick()
        assert o["state"] == "PLACED" and api.added == [(1, "https://t.me/coolchan", 200)]
        assert o["cost"] == 10

        api.st[501] = {"status": "Completed", "charge": "9.5", "currency": "RUB"}
        await smm.tick()
        assert o["state"] == "DONE" and o["cost"] == 9.5
        assert "выполнен" in sv.sent[-1][1]

        # второй заказ -> OptSMM отменил -> возврат
        sv.details["o2"] = {"order": {"buyerId": 77}, "chat": {"id": "c1"}}
        await smm.handle_order(sale("o2", 1))
        await smm.handle_message("c1", 77, "https://t.me/x2")
        assert smm.orders["o2"]["state"] == "PLACED"
        api.st[502] = {"status": "Canceled"}
        await smm.tick()
        assert smm.orders["o2"]["state"] == "REFUNDED" and sv.refunds == ["o2"]

        # после перезапуска состояние сохраняется
        again = SmmEngine(store, sv, api, path="storage/smm.json")
        assert again.orders["o1"]["state"] == "DONE" and len(again.rules) == 1
    asyncio.run(run())


def test_below_min_refund_and_early_link():
    async def run():
        store, sv, api, smm = make()
        # 1 шт лота = 100, но у услуги минимум 100 -> ок; ставим per_unit=50 -> 50 < 100
        smm.rules[0]["per_unit"] = 50
        sv.details["o3"] = {"order": {"buyerId": 5}, "chat": {"id": "c5"}}
        await smm.handle_order(sale("o3", 1, buyer=5))
        assert smm.orders["o3"]["state"] == "REFUNDED" and sv.refunds == ["o3"]

        # ссылка пришла раньше, чем поллер увидел заказ
        smm.rules[0]["per_unit"] = 100
        api.bal = 100
        assert await smm.handle_message("c6", 6, "https://t.me/early") is True
        sv.details["o4"] = {"order": {"buyerId": 6}, "chat": {"id": "c6"}}
        await smm.handle_order(sale("o4", 1, buyer=6))
        assert smm.orders["o4"]["state"] == "PLACED"
        assert api.added[-1] == (1, "https://t.me/early", 100)
    asyncio.run(run())


def test_queue_and_manual_link():
    async def run():
        store, sv, api, smm = make()
        api.bal = 1000
        for oid in ("q1", "q2"):
            sv.details[oid] = {"order": {"buyerId": 9}, "chat": {"id": "c9"}}
            await smm.handle_order(sale(oid, 1, buyer=9))
        assert smm.orders["q1"]["state"] == "ASKED" and smm.orders["q2"]["state"] == "QUEUED"
        await smm.handle_message("c9", 9, "t.me/one")
        assert smm.orders["q1"]["state"] == "PLACED" and smm.orders["q2"]["state"] == "ASKED"

        api.fail_add = "incorrect link"
        await smm.handle_message("c9", 9, "t.me/two")
        assert smm.orders["q2"]["state"] == "MANUAL"
        api.fail_add = None
        assert "запущен" in await smm.manual_link("Q2", "https://t.me/two_ok")
        assert smm.orders["q2"]["state"] == "PLACED"
    asyncio.run(run())


def test_poller_routes_smm_before_rent():
    """Поллер отдаёт SMM-лот модулю, а лот аренды — движку аренды."""
    async def run():
        store, sv, api, smm = make()
        handled = []

        class Eng:
            async def handle_order(self, order):
                handled.append(order["id"])
                store.mark_handled(order["id"])

        class SV2(FakeSV):
            orders = []

            async def get_orders(self, status=None):
                return self.orders

        sv2 = SV2()
        sv2.details = {"s1": {"order": {"buyerId": 1}, "chat": {"id": "c"}}}
        smm.sv = sv2
        p = Poller(store, sv2, Eng(), smm=smm)
        await p._poll_orders()               # стартовая синхронизация
        sv2.orders = [sale("s1", 1), sale("r1", 5, RENT_BRIEF)]
        await p._poll_orders()
        assert handled == ["r1"] and "s1" in smm.orders
    asyncio.run(run())


def test_dry_run_places_nothing():
    async def run():
        store, sv, api, smm = make()
        store.settings["dry_run"] = True
        api.bal = 100
        sv.details["d1"] = {"order": {"buyerId": 3}, "chat": {"id": "c3"}}
        await smm.handle_order(sale("d1", 1, buyer=3))
        await smm.handle_message("c3", 3, "t.me/test")
        assert api.added == [] and sv.sent == []
    asyncio.run(run())


def test_panel_smm_screens():
    """Экраны SMM рисуются, правило добавляется и ищутся услуги."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from core.engine import Engine
    from core.maintenance import Maintenance
    from core.stats import SalesLog
    from lots.sync import OffersCache
    from tgpanel.panel import Ctx, Panel

    async def run():
        store, sv, api, smm = make()
        sv.my_username, sv.my_user_id = "me", 1
        engine = Engine(store, sv, None)
        poller = Poller(store, sv, engine, smm=smm)
        panel = Panel(store, sv, None, poller, engine, Maintenance(store, sv, engine, OffersCache(), None),
                      OffersCache(), SalesLog("storage/sales.json"), None, None, smm=smm)
        fsm = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        ctx = Ctx(None, fsm, None)
        text, _ = await panel.r_smm(ctx, "")
        assert "OptSMM" in text and "правил: <b>1</b>" in text
        text, _ = await panel.r_smm_rules(ctx, "")
        assert "подписчики, telegram" in text
        rid = smm.rules[0]["id"]
        text, _ = await panel.r_smm_rule(ctx, rid)
        assert "Себестоимость 1 шт лота" in text and "от 1 шт лота" in text
        note = await panel._apply_value("smm_rule", {}, "лайки, instagram | 1 | 1000 | ig")
        assert "добавлено" in note and len(smm.rules) == 2
        await panel._apply_value("smm_search", {}, "tg")
        text, _ = await panel.r_smm_services(ctx, "")
        assert "Найдено услуг: 1" in text
        text, _ = await panel.r_smm_orders(ctx, "")
        assert "SMM-заказы" in text
        assert "smm" in panel._routes and "setg" in panel._routes
    asyncio.run(run())
