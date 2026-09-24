import asyncio, sys, json, copy
from pathlib import Path
FIX = Path(__file__).parent / "fixtures"
from core.log import setup_logging; setup_logging("ERROR")
from core.storage import Store
from core.engine import Engine
from core.poller import Poller

L4D = "🎮 АРЕНДА [Left 4 Dead 2] ⚡ АВТОВЫДАЧА 24/7 ⚡ ⏱️ 1 ШТ = 1 ЧАС 🚀 СВОБОДЕН? ПИШИ !наличие"
OFFER = {"id": 300655, "publicId": "5e99", "descriptions": {"rus": {"briefDescription": L4D}}}

def msg(i, text, author=362192, offer=None, typ="DEFAULT"):
    return {"id": f"m{i}", "content": text, "type": typ, "authorId": author,
            "createdAt": f"2026-09-23T09:52:{i:02d}Z", "offer": offer}

class SV:
    my_user_id = 215890
    def __init__(s): s.chats=[]; s.history={}; s.sent=[]; s.viewed=None
    async def get_chats(s): return copy.deepcopy(s.chats)
    async def list_messages(s, chat_id, limit=30): return copy.deepcopy(s.history.get(chat_id, []))
    async def viewed_offer(s, buyer): return s.viewed
    async def send_message(s, chat_id, text): s.sent.append((chat_id, text))
    async def get_orders(s, status=None): return []
    async def keep_alive(s): return True

class KS:
    async def products(s, search=None, currency="RUB"):
        return [{"id": 71, "name": "Left 4 Dead 2", "available_accounts": 7, "price_per_hour_rub": 0.26,
                 "min_hours": 1, "max_hours": 720, "next_available": None},
                {"id": 2, "name": "Rust", "available_accounts": 0, "price_per_hour_rub": 4.4,
                 "min_hours": 1, "max_hours": 720, "next_available": "2026-09-23T10:40:00Z"}]

async def main():
    st = Store(); st.settings.update({"dry_run": True, "greeting_enabled": True})
    sv, ks = SV(), KS(); eng = Engine(st, sv, ks); p = Poller(st, sv, eng)

    # старт бота: существующий старый чат синхронизируется без ответов
    sv.chats = [{"id": "old", "lastMessage": msg(1, "старое", author=111)}]
    await p._poll_chats(); assert sv.sent == [], sv.sent

    # 1. друг открывает лот Left 4 Dead 2 и пишет «dadad» — новый чат
    sv.history["c"] = [msg(5, "dadad", offer=OFFER)]
    sv.chats.append({"id": "c", "lastMessage": msg(5, "dadad", offer=None)})
    await p._poll_chats()
    assert len(sv.sent) == 1, f"нет приветствия на первое сообщение: {sv.sent}"
    greet = sv.sent[-1][1]
    assert "Здравствуйте" in greet and "Left 4 Dead 2" in greet and "свободен" in greet, greet
    print("1) «dadad» в новом чате → приветствие (тестовый режим не мешает):")
    print("   " + greet.replace("\n", "\n   "))

    # 2. «!наличие» без названия — лот берётся из «Покупатель смотрит»
    sv.viewed = {"id": 300655, "briefDescription": L4D}
    sv.history["c"].append(msg(37, "!наличие"))
    sv.chats[-1]["lastMessage"] = msg(37, "!наличие")
    await p._poll_chats()
    assert len(sv.sent) == 2 and "Left 4 Dead 2" in sv.sent[-1][1] and "свободен" in sv.sent[-1][1], sv.sent
    print("\n2) «!наличие» → " + sv.sent[-1][1].replace("\n", " "))

    # 3. два сообщения между опросами — оба обработаны
    sv.history["c"] += [msg(40, "наличие Rust"), msg(41, "!помощь")]
    sv.chats[-1]["lastMessage"] = msg(41, "!помощь")
    await p._poll_chats()
    assert len(sv.sent) == 4, len(sv.sent)
    assert "Rust" in sv.sent[2][1] and "занят" in sv.sent[2][1], sv.sent[2]
    assert "!наличие" in sv.sent[3][1], sv.sent[3]
    print("3) два сообщения подряд — оба получили ответ (Rust занят, !помощь)")

    # 4. повторное «привет» — второе приветствие не шлём
    sv.history["c"].append(msg(50, "привет"))
    sv.chats[-1]["lastMessage"] = msg(50, "привет")
    await p._poll_chats()
    assert len(sv.sent) == 4, "приветствие ушло повторно"
    print("4) повторное «привет» — без второго приветствия")

    # 5. свои сообщения и системные — игнорируются
    sv.history["c"] += [msg(51, "Аккаунт выдан", author=215890), msg(52, "Заказ оплачен", typ="ORDER")]
    sv.chats[-1]["lastMessage"] = msg(52, "Заказ оплачен", typ="ORDER")
    await p._poll_chats()
    assert len(sv.sent) == 4
    print("5) свои и системные сообщения не обрабатываются")

    # 6. сообщения про заказ в тесте по-прежнему не отправляются
    await eng.say("c", "логин/пароль", order=True)
    assert len(sv.sent) == 4
    print("6) выдача в тестовом режиме по-прежнему не уходит покупателю")
    print("\n✅ СЦЕНАРИЙ ДРУГА ПРОЙДЕН")
def test_chats(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(main())
