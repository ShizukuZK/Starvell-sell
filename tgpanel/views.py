"""Экраны панели. Чистые функции: данные -> (текст, клавиатура).

Здесь нет обращений к сети — всё нужное передаётся аргументами. Благодаря
этому каждый экран можно отрисовать и проверить без Telegram.
"""
from __future__ import annotations

import html
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from aiogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup

from core.storage import mapping_id
from core.texts import DEFAULT_TEXTS, fmt_expires
from tgpanel.schema import FIELDS, GROUP_TITLES, GROUPS, TEXT_LABELS

Screen = Tuple[str, InlineKeyboardMarkup]
PER_PAGE = 8


# ============================================================== помощники

def kb(*rows: Sequence[Btn]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def b(text: str, data: str) -> Btn:
    return Btn(text=text, callback_data=data)


def e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def money(value: Any) -> str:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    return f"{v:,.2f}".replace(",", " ") + " ₽"


def ago(ts: float) -> str:
    if not ts:
        return "ещё не было"
    s = int(time.time() - ts)
    if s < 60:
        return "только что"
    if s < 3600:
        return f"{s // 60} мин назад"
    if s < 86400:
        return f"{s // 3600} ч назад"
    return f"{s // 86400} дн назад"


def duration(seconds: float) -> str:
    s = max(0, int(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d} дн {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{m} мин"


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(5, "продажа", "продажи", "продаж") -> «5 продаж»."""
    n = int(n)
    tail = n % 100
    if 11 <= tail <= 14:
        word = many
    elif n % 10 == 1:
        word = one
    elif 2 <= n % 10 <= 4:
        word = few
    else:
        word = many
    return f"{n} {word}"


def bar(done: int, total: int, width: int = 12) -> str:
    total = max(1, total)
    filled = round(width * min(done, total) / total)
    return "▓" * filled + "░" * (width - filled)


def nav(prefix: str, page: int, total: int, per: int = PER_PAGE) -> List[Btn]:
    pages = max(1, (total + per - 1) // per)
    if pages == 1:
        return []
    row = []
    if page > 0:
        row.append(b("◀️", f"{prefix}:{page - 1}"))
    row.append(b(f"{page + 1} / {pages}", "noop"))
    if page + 1 < pages:
        row.append(b("▶️", f"{prefix}:{page + 1}"))
    return row


def back(target: str = "home", text: str = "◀️ Назад") -> InlineKeyboardMarkup:
    return kb([b(text, target)])


TIER_ICON = {"hot": "🔥", "popular": "⭐"}
TIER_NAME = {"hot": "хит", "popular": "популярная", "normal": "обычная"}


def num(n: Any) -> str:
    """43747 -> «43 747», 1400607 -> «1.4 млн»."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} млн".replace(".0 ", " ")
    return f"{n:,}".replace(",", " ")


def demand_line(tier: Optional[str], info: Dict[str, Any]) -> str:
    if not info or not info.get("peak") and not info.get("reviews"):
        return ""
    icon = TIER_ICON.get(tier or "", "▫️")
    return (f"{icon} Спрос: {TIER_NAME.get(tier or 'normal', 'обычная')} · пик "
            f"{num(info.get('peak'))} онлайн · {num(info.get('reviews'))} отзывов в Steam")


def offer_status(offer: Dict[str, Any]) -> Tuple[str, str]:
    """Иконка и подпись статуса лота."""
    mod = str(offer.get("moderationStatus") or "").upper()
    if mod in ("PENDING", "IN_REVIEW"):
        return "⏳", "на модерации"
    if mod in ("REJECTED", "DECLINED"):
        return "❌", "отклонён модерацией"
    if not offer.get("isActive", True):
        return "⏸", "снят с продажи"
    if int(offer.get("availability") or 0) <= 0:
        return "📭", "нет в наличии"
    return "✅", "в продаже"


def offer_name(offer: Dict[str, Any], game: Optional[str]) -> str:
    if game:
        return game
    brief = ((offer.get("descriptions") or {}).get("rus") or {}).get("briefDescription") or ""
    return brief[:40] or f"Лот {offer.get('id')}"


# ============================================================== главная

def home(ctx: Dict[str, Any]) -> Screen:
    dry = ctx.get("dry_run")
    enabled = ctx.get("enabled", True)
    state = "🟢 Работает" if enabled else "⏸ На паузе"
    lines = [
        "🤖 <b>KOSell × OptSMM × Starvell</b>",
        "",
        f"{state}" + (" · 🧪 <b>тестовый режим</b>" if dry else ""),
        f"👤 {e(ctx.get('username') or '—')} · ⏱ {duration(ctx.get('uptime', 0))}",
        "",
    ]
    bal = ctx.get("balance")
    if bal is None:
        lines.append("💰 Баланс KOSell: <i>нет ответа</i>")
    else:
        low = ctx.get("balance_alert") and bal < ctx["balance_alert"]
        lines.append(f"💰 Баланс KOSell: <b>{money(bal)}</b>" + (" 🪫 мало" if low else ""))
    if ctx.get("offers_total") is None:
        lines.append("📦 Лоты: <i>загружаются…</i>")
    else:
        lines.append(f"📦 Лоты: <b>{ctx['offers_total']}</b> · в продаже {ctx.get('offers_active', 0)}")
    lines.append(f"🎮 Аренд сейчас: <b>{ctx.get('rentals', 0)}</b>")
    if ctx.get("smm") is not None:
        lines.append(f"📣 SMM в работе: <b>{ctx['smm']}</b>")
    today = ctx.get("today") or {}
    lines.append(f"📈 Сегодня: <b>{plural(today.get('orders', 0), 'продажа', 'продажи', 'продаж')}</b>"
                 f" · {money(today.get('revenue', 0))}")

    if dry:
        lines += ["", "<i>🧪 Заказы разбираются, но аккаунты не выдаются и деньги не "
                      "тратятся. На вопросы покупателей (!наличие, приветствие) бот "
                      "отвечает как обычно. Выключите тест, когда будете готовы.</i>"]
    if not enabled:
        lines += ["", "<i>⏸ Новые заказы не обрабатываются.</i>"]
    if ctx.get("last_error"):
        lines += ["", f"⚠️ <i>{e(str(ctx['last_error'])[:120])}</i>"]
    upd = ctx.get("update")
    if upd:
        lines += ["", f"🆕 Доступна версия <b>{e(upd['version'])}</b> — «ℹ️ Версия»"]
    lines += ["", f"<i>v{e(ctx.get('version') or '?')}</i>"]

    return "\n".join(lines), kb(
        [b("📦 Лоты", "lots:0"), b("➕ Создать лоты", "new")],
        [b("🎮 Аренды", "rent"), b("📈 Статистика", "stats:day")],
        [b("🔗 Привязки", "maps:0"), b("📣 SMM", "smm")],
        [b("✏️ Тексты", "txt")],
        [b("⚙️ Настройки", "set"), b("❓ Помощь", "help")],
        [b("🧪 Тест: ВКЛ" if dry else "🧪 Тест: выкл", "tog_home:dry_run"),
         b("⏸ Пауза" if enabled else "▶️ Запустить", "tog_home:enabled")],
        [b("🔄 Обновить", "home"), b("🆕 Версия" if upd else "ℹ️ Версия", "ver")],
    )


def version_screen(info: Dict[str, Any]) -> Screen:
    """Экран «Версия»: текущая, последняя на GitHub, как обновиться."""
    lines = ["ℹ️ <b>Версия бота</b>", "", f"Установлена: <b>{e(info.get('current'))}</b>"]
    repo = info.get("repo")
    if not repo:
        lines += ["", "Репозиторий GitHub не указан — новые версии не проверяются.",
                  "Задайте его в ⚙️ → ⏱ Опрос и обновления → «Репозиторий GitHub»."]
    else:
        lines.append(f"Репозиторий: <code>{e(repo)}</code>")
        latest = info.get("latest")
        if info.get("error"):
            lines.append(f"⚠️ <i>{e(info['error'])}</i>")
        elif latest:
            if info.get("newer"):
                lines += [f"На GitHub: <b>{e(latest['version'])}</b> 🆕", "",
                          "<b>Как обновиться</b>"]
                if info.get("self_update"):
                    lines.append("• Нажмите «⬇️ Обновить сейчас» или перезапустите сервер.")
                else:
                    lines += ["• На ПК: закройте бота и запустите <code>update.bat</code>.",
                              "• На хостинге: перезапустите сервер — код подтянется сам."]
                notes = (latest.get("notes") or "").strip()
                if notes:
                    lines += ["", "<b>Что нового</b>", e(notes[:900])]
            else:
                lines.append("✅ Это последняя версия.")
        else:
            lines.append("Релизов на GitHub пока нет.")
        if info.get("checked"):
            lines.append(f"<i>Проверено: {ago(info['checked'])}</i>")
    if info.get("note"):
        lines += ["", e(info["note"])]
    rows = [[b("🔍 Проверить сейчас", "ver:check")]] if repo else []
    if info.get("self_update"):
        rows.append([b("⬇️ Обновить сейчас", "upd")])
    rows.append([b("⚙️ Настроить", "setg:tech"), b("◀️ Меню", "home")])
    return "\n".join(lines), kb(*rows)


def restarting_screen(message: str) -> Screen:
    return ("⬇️ <b>Новая версия скачана</b>\n\n"
            f"<i>{e(message)}</i>\n\n"
            "Перезапускаюсь — через 15–30 секунд бот снова на связи "
            "и пришлёт «🟢 Бот запущен».", kb([b("◀️ Меню", "home")]))


# ============================================================== лоты

def lots_list(
    offers: List[Dict[str, Any]], names: Dict[Any, str], linked: set,
    page: int, stock_info: Dict[str, Any], free: Optional[Dict[Any, Optional[int]]] = None,
    tiers: Optional[Dict[Any, Optional[str]]] = None, limit: Optional[int] = None,
) -> Screen:
    free = free or {}
    tiers = tiers or {}
    total = len(offers)
    counts = {"✅": 0, "⏸": 0, "⏳": 0, "📭": 0, "❌": 0}
    for o in offers:
        counts[offer_status(o)[0]] += 1

    lines = [f"📦 <b>Мои лоты</b> · {total}" + (f" из {limit}" if limit else ""), ""]
    if not offers:
        lines += ["Лотов аренды пока нет.",
                  "Создайте их кнопкой «➕ Создать лоты» в главном меню."]
        return "\n".join(lines), kb([b("➕ Создать лоты", "new")],
                                    [b("🔄 Обновить", "lots_r"), b("◀️ Меню", "home")])

    lines.append(
        f"✅ в продаже {counts['✅']} · ⏸ сняты {counts['⏸']} · 📭 пусто {counts['📭']}"
        + (f" · ⏳ модерация {counts['⏳']}" if counts["⏳"] else "")
        + (f" · ❌ отклонены {counts['❌']}" if counts["❌"] else "")
    )
    lines.append(f"🔗 Связаны с KOSell: {len(linked)} из {total}")
    lines.append(f"🔄 Остатки сверены: {ago(stock_info.get('ts', 0))}")
    lines += ["", "<i>👤 — свободных аккаунтов в KOSell · 🔥 хит · ⭐ популярная.",
              "Нажмите на лот, чтобы изменить цену или снять с продажи.</i>"]

    ordered = sorted(offers, key=lambda o: (offer_status(o)[0] != "✅", names.get(o.get("id"), "")))
    chunk = ordered[page * PER_PAGE:(page + 1) * PER_PAGE]
    rows = []
    for o in chunk:
        icon, _ = offer_status(o)
        name = names.get(o.get("id")) or offer_name(o, None)
        acc = free.get(o.get("id"))
        acc_text = f" · 👤{acc}" if acc is not None else " · 👤?"
        hot = TIER_ICON.get(tiers.get(o.get("id")) or "", "")
        rows.append([b(f"{icon}{hot} {name[:26]} · {float(o.get('price') or 0):.2f} ₽{acc_text}",
                       f"lot:{o.get('id')}")])
    return "\n".join(lines), kb(
        *rows,
        nav("lots", page, total),
        [b("🔄 Сверить остатки", "lots_stock"), b("💸 Пересчитать цены", "lots_price")],
        [b("⬆️ Поднять все", "lots_bump"), b("♻️ Заменить слабые", "rot")],
        [b("🔗 Связать с KOSell", "lots_link"), b("🔄 Обновить", "lots_r")],
        [b("◀️ Меню", "home")],
    )


def lot_card(offer: Dict[str, Any], game: Optional[str], mapping: Optional[Dict[str, Any]],
             product: Optional[Dict[str, Any]], hours_per_unit: int = 1,
             tier: Optional[str] = None, info: Optional[Dict[str, Any]] = None) -> Screen:
    icon, status = offer_status(offer)
    price = float(offer.get("price") or 0)
    brief = ((offer.get("descriptions") or {}).get("rus") or {}).get("briefDescription") or ""
    lines = [
        f"🎮 <b>{e(offer_name(offer, game))}</b>",
        f"<i>{e(brief)}</i>",
        "",
        f"💵 Цена: <b>{money(price)}</b> за штуку ({hours_per_unit} ч)",
        f"📦 За раз можно купить: до <b>{int(offer.get('availability') or 0)}</b> шт",
        f"{icon} Статус: {status}",
    ]
    if offer.get("viewsCount") is not None:
        lines.append(f"👀 Просмотров: {offer.get('viewsCount')}")
    lines.append("")
    if mapping and product:
        per_hour = float(product.get("price_per_hour_rub") or 0)
        cost = per_hour * max(1, hours_per_unit)
        margin = f" · наценка {((price / cost) - 1) * 100:+.0f}%" if cost > 0 else ""
        lines.append(f"🔗 KOSell #{product.get('id')} · {money(per_hour)}/ч{margin}")
        lines.append(f"👤 Свободных аккаунтов: <b>{int(product.get('available_accounts') or 0)}</b>")
        if mapping.get("hidden_by_bot"):
            lines.append("   <i>снят ботом: в KOSell закончились аккаунты</i>")
        dl = demand_line(tier, info or {})
        if dl:
            lines.append(dl)
    elif mapping:
        lines.append(f"🔗 KOSell #{mapping.get('product_id')}")
    else:
        lines.append("⚠️ Не связан с KOSell — заказы по нему не выдаются.")
    lines += ["", f"🌐 https://starvell.com/offers/{offer.get('id')}"]

    oid = offer.get("id")
    active = offer.get("isActive", True)
    return "\n".join(lines), kb(
        [b("💵 Цена", f"lotask:{oid}:price"), b("📦 Наличие", f"lotask:{oid}:stock")],
        [b("⏸ Снять с продажи" if active else "▶️ Вернуть в продажу", f"lotact:{oid}:toggle")],
        [b("🗑 Удалить", f"lotact:{oid}:del")],
        [b("◀️ К списку", "lots:0")],
    )


def lot_delete_confirm(offer: Dict[str, Any], game: Optional[str]) -> Screen:
    return (
        f"🗑 <b>Удалить лот?</b>\n\n{e(offer_name(offer, game))}\n\n"
        "Лот исчезнет со Starvell. Отменить это нельзя, но его можно создать заново.",
        kb([b("✅ Да, удалить", f"lotact:{offer.get('id')}:delok"),
            b("✖️ Нет", f"lot:{offer.get('id')}")]),
    )


# ============================================================== создание лотов

def wizard(info: Dict[str, Any]) -> Screen:
    available = info.get("available", 0)
    limit = info.get("limit")
    free = info.get("free_slots")
    lines = [
        "➕ <b>Создание лотов</b>",
        "",
        f"📦 У вас сейчас: <b>{info.get('existing', 0)}</b>"
        + (f" из {limit} — столько Starvell разрешает в разделе" if limit else " лотов"),
    ]
    if free is not None:
        lines.append(f"🆓 Свободных мест: <b>{free}</b>")
    lines.append(f"🎮 Подходящих игр в KOSell: {info.get('candidates', available)}")
    if info.get("demand_ready"):
        lines += ["", f"📊 Отбор по спросу в Steam (обновлено {ago(info.get('demand_ts', 0))}):",
                  "<i>   лучшие игры идут первыми — по пиковому онлайну и отзывам, "
                  "с учётом свободных аккаунтов</i>"]
        if info.get("top"):
            for p in info["top"][:5]:
                lines.append(f"   {TIER_ICON.get(p.get('tier') or '', '▫️')} {e(p['product_name'][:30])} — "
                             f"пик {num(p.get('players'))} · 👤{p['available']}")
    else:
        lines += ["", "📊 <i>Данные о спросе ещё не загружены — порядок пока по числу "
                      "свободных аккаунтов. Нажмите «📊 Спрос», это ~30 секунд.</i>"]
    lines += [
        "",
        f"💵 Цена за штуку = себестоимость × {1 + info.get('markup', 0) / 100:.2f}"
        + (f" ÷ {1 - info.get('commission', 0) / 100:.2f}" if info.get("commission") else ""),
        f"   но не ниже: 🔥 {money(info.get('floor_hot', 0))} · ⭐ {money(info.get('floor_pop', 0))} · "
        f"обычная {money(info.get('min_price', 0))}",
    ]
    if free == 0:
        lines += ["", "Мест нет. Освободите их кнопкой «♻️ Заменить слабые» в разделе лотов."]
        return "\n".join(lines), kb([b("♻️ Заменить слабые", "rot")],
                                    [b("📦 Лоты", "lots:0"), b("◀️ Меню", "home")])
    if not available:
        lines += ["", "Все подходящие игры уже выставлены 🎉"]
        return "\n".join(lines), kb(
            [b("📊 Спрос", "dem"), b("🔄 Каталог", "cat_scan")], [b("◀️ Меню", "home")])

    lines += ["", "Сколько лотов создать? Начните с малого и проверьте на сайте."]
    counts = [n for n in (5, 10, 25, 50) if n < available]
    rows = [[b(str(n), f"new_n:{n}") for n in counts]] if counts else []
    rows.append([b(f"Все лучшие {available}" if free is not None else f"Все {available}",
                   f"new_n:{available}")])
    rows.append([b("👁 Пример лота", "new_ex"), b("📊 Спрос", "dem")])
    rows.append([b("🔄 Каталог", "cat_scan"), b("◀️ Меню", "home")])
    return "\n".join(lines), kb(*rows)


def wizard_confirm(plan: List[Dict[str, Any]], pause: float) -> Screen:
    n = len(plan)
    lines = [f"➕ Создать <b>{n}</b> лотов?", ""]
    for i, p in enumerate(plan[:12], 1):
        icon = TIER_ICON.get(p.get("tier") or "", "")
        lines.append(f"{i}. {icon}{' ' if icon else ''}{e(p['product_name'][:32])} — "
                     f"<b>{p['price_rub']:.2f} ₽</b>/шт · 👤{p['available']}")
    if n > 12:
        lines.append(f"… и ещё {n - 12}")
    lines += ["", f"⏱ Займёт примерно {duration(n * (pause + 0.5) + 59)}. "
                  "Остановить можно в любой момент."]
    return "\n".join(lines), kb([b("🚀 Создать", f"new_go:{n}")], [b("◀️ Назад", "new")])


def lot_example(item: Dict[str, Any]) -> Screen:
    return (
        "👁 <b>Так будет выглядеть лот</b>\n\n"
        f"<b>Заголовок</b> ({item.get('title_len', '?')}/100):\n{e(item['title'])}\n\n"
        f"<b>Цена:</b> {money(item['price_rub'])} за штуку · за раз до {item.get('units', '?')} шт · "
        f"свободных аккаунтов {item['available']}\n"
        + (f"{demand_line(item.get('tier'), {'peak': item.get('players'), 'reviews': item.get('reviews')})}\n"
           if item.get("players") or item.get("reviews") else "")
        + "\n"
        f"<b>Описание:</b>\n{e(item['description'][:1500])}\n\n"
        "<i>Шаблоны меняются в разделе ✏️ Тексты.</i>",
        back("new"),
    )


# ============================================================== фоновые задачи

def progress(title: str, done: int, total: int, ok: int, failed: int, current: str = "") -> Screen:
    lines = [f"⏳ <b>{e(title)}</b>", "", f"{bar(done, total)}  {done}/{total}",
             f"✅ {ok} · ❌ {failed}"]
    if current:
        lines.append(f"Сейчас: {e(current[:50])}")
    return "\n".join(lines), kb([b("⏹ Остановить", "job_stop")])


def job_done(title: str, lines: List[str], back_to: str = "home") -> Screen:
    return "\n".join([f"<b>{e(title)}</b>", ""] + lines), kb(
        [b("📦 К лотам", "lots_r"), b("◀️ Меню", "home")] if back_to == "lots"
        else [b("◀️ Меню", "home")])


def grouped_errors(errors: List[str], limit: int = 6) -> List[str]:
    """«Игра: причина» -> «причина × N» — чтобы не листать сотню одинаковых строк."""
    from collections import Counter
    reasons = Counter(err.split(": ", 1)[-1] for err in errors)
    out = []
    for reason, n in reasons.most_common(limit):
        out.append(f"• {e(reason)}" + (f" × {n}" if n > 1 else ""))
    return out


# ============================================================== аренды

def rentals(items: List[Dict[str, Any]], tz: int) -> Screen:
    now = time.time()
    active = sorted([r for r in items if r.get("expires_ts", 0) > now],
                    key=lambda r: r.get("expires_ts", 0))
    if not active:
        return ("🎮 <b>Активные аренды</b>\n\nСейчас никто ничего не арендует.",
                kb([b("🔄 Обновить", "rent"), b("◀️ Меню", "home")]))
    lines = [f"🎮 <b>Активные аренды</b> · {len(active)}", ""]
    rows = []
    for i, r in enumerate(active[:20], 1):
        left = duration(r["expires_ts"] - now)
        lines.append(f"{i}. <b>{e(r.get('product_name'))}</b> · <code>{e(r.get('login'))}</code>\n"
                     f"    осталось {left} · до {fmt_expires(r['expires_ts'], tz)}")
        uid = str(r.get("rental_uid") or "")
        if uid and len(f"rentc:{uid}".encode()) <= 64:
            rows.append([b(f"🔐 {i}. код для {str(r.get('login'))[:20]}", f"rentc:{uid}")])
    return "\n".join(lines), kb(*rows, [b("🔄 Обновить", "rent"), b("◀️ Меню", "home")])


# ============================================================== статистика

PERIODS = {"day": "Сегодня", "week": "7 дней", "month": "30 дней", "all": "Всё время"}


def stats(summary: Dict[str, Any], period: str, commission: float) -> Screen:
    lines = [f"📈 <b>Статистика</b> · {PERIODS.get(period, period)}", ""]
    if not summary.get("orders") and not summary.get("refunds"):
        lines.append("Продаж за этот период нет.")
    else:
        lines += [
            f"🛒 Продажи: <b>{summary['orders']}</b>"
            + (f" (из них продлений {summary['extends']})" if summary.get("extends") else "")
            + (f" · 📣 SMM: {summary['smm']}" if summary.get("smm") else ""),
            f"↩️ Возвратов: {summary.get('refunds', 0)}",
            f"⏱ Часов аренды: {summary.get('hours', 0)}",
            "",
            f"💵 Выручка: <b>{money(summary['revenue'])}</b>",
            f"💸 Себестоимость (KOSell + OptSMM): {money(summary['cost'])}",
            f"💰 Прибыль ≈ <b>{money(summary['profit'])}</b>"
            + (f" <i>(с учётом комиссии {commission:g}%)</i>" if commission else ""),
        ]
        if summary.get("top"):
            lines += ["", "🏆 <b>Топ игр</b>"]
            for i, (game, n, rev) in enumerate(summary["top"], 1):
                lines.append(f"{i}. {e(game)} — {n} · {money(rev)}")
    lines += ["", "<i>Считаются только реальные выдачи — тестовый режим сюда не попадает.</i>"]
    row = [b(("• " if k == period else "") + v, f"stats:{k}") for k, v in PERIODS.items()]
    return "\n".join(lines), kb(row[:2], row[2:], [b("◀️ Меню", "home")])


# ============================================================== привязки

def maps_list(mappings: List[Dict[str, Any]], page: int, auto: bool) -> Screen:
    lines = ["🔗 <b>Привязки: игра → товар KOSell</b>", "",
             f"Автопоиск по названию: {'✅ включён' if auto else '❌ выключен'}", ""]
    if not mappings:
        lines += ["Пока пусто — и это нормально. При первом заказе бот сам найдёт игру "
                  "в каталоге KOSell и запишет привязку."]
    else:
        lines.append(f"Всего: {len(mappings)} · с лотом на Starvell: "
                     f"{sum(1 for m in mappings if m.get('offer_id'))}")
        lines.append("<i>Нажмите, чтобы сменить товар или отключить выдачу по игре.</i>")
    chunk = mappings[page * PER_PAGE:(page + 1) * PER_PAGE]
    rows = [[b(f"{'✅' if m.get('enabled', True) else '⏸'} "
               f"{str(m.get('game') or m.get('product_name'))[:32]}",
               f"map:{m.get('id') or mapping_id(m.get('key', ''))}")] for m in chunk]
    return "\n".join(lines), kb(
        *rows, nav("maps", page, len(mappings)),
        [b(("✅" if auto else "❌") + " Автопоиск", "tog_maps:auto_map_by_name"),
         b("➕ Добавить", "map_add")],
        [b("◀️ Меню", "home")],
    )


def map_card(m: Dict[str, Any]) -> Screen:
    mid = m.get("id") or mapping_id(m.get("key", ""))
    lines = [
        f"🔗 <b>{e(m.get('game') or m.get('product_name'))}</b>", "",
        f"Товар KOSell: <b>{e(m.get('product_name'))}</b> (#{m.get('product_id')})",
        f"Лот Starvell: {('https://starvell.com/offers/' + str(m['offer_id'])) if m.get('offer_id') else 'нет'}",
        f"Источник: {'автоматически' if m.get('auto') else 'вручную'}",
        f"Выдача по этой игре: {'✅ включена' if m.get('enabled', True) else '⏸ отключена'}",
    ]
    return "\n".join(lines), kb(
        [b("🎮 Сменить товар", f"mapp:{mid}")],
        [b("⏸ Отключить выдачу" if m.get("enabled", True) else "▶️ Включить выдачу", f"mapt:{mid}")],
        [b("🗑 Удалить привязку", f"mapd:{mid}")],
        [b("◀️ К списку", "maps:0")],
    )


# ============================================================== настройки

def settings_root() -> Screen:
    return (
        "⚙️ <b>Настройки</b>\n\nВыберите раздел.",
        kb(*[[b(title, f"setg:{gid}")] for gid, title, _ in GROUPS],
           [b("💾 Перенос данных", "bak"), b("ℹ️ Версия", "ver")],
           [b("◀️ Меню", "home")]),
    )


def backup_screen(note: str = "") -> Screen:
    text = (
        "💾 <b>Перенос данных</b>\n\n"
        "Нужен при переезде бота с ПК на хостинг (или обратно): переносит "
        "настройки цен, привязки лотов, активные аренды, тексты и статистику.\n\n"
        "<b>Как перенести</b>\n"
        "1. Здесь, на старом месте: «📤 Скачать копию» — придёт файл.\n"
        "2. Остановите старого бота.\n"
        "3. Перешлите файл новому боту — он предложит загрузить.\n\n"
        "<i>Ключи Starvell, KOSell и токены в файл не попадают — "
        "на новом месте остаются свои.</i>"
    )
    if note:
        text += "\n\n" + note
    return text, kb([b("📤 Скачать копию", "bak:get")], [b("◀️ Настройки", "set")])


def restore_confirm(info: Dict[str, Any]) -> Screen:
    lines = ["💾 <b>Загрузить резервную копию?</b>", "",
             f"Файл: <code>{e(info.get('name'))}</code>"]
    if info.get("created"):
        lines.append(f"Создан: {ago(info['created'])}")
    lines += ["", "Текущие настройки, привязки и аренды будут заменены данными из "
                  "файла. Ключи и токены останутся текущими."]
    return "\n".join(lines), kb([b("✅ Загрузить", "bak:apply"), b("✖️ Отмена", "bak:drop")])


def settings_group(gid: str, values: Dict[str, Any], note: str = "") -> Screen:
    title = GROUP_TITLES.get(gid, "Настройки")
    keys = next((k for g, _, k in GROUPS if g == gid), [])
    lines = [f"{title}", ""]
    rows = []
    for key in keys:
        f = FIELDS[key]
        val = values.get(key)
        if f.kind == "bool":
            rows.append([b(f"{'✅' if val else '❌'} {f.label}", f"tog:{key}:{gid}")])
        else:
            rows.append([b(f"{f.label}: {f.show(val)}", f"ask:{key}:{gid}")])
    if gid == "price" and values.get("_price_example"):
        lines.append(values["_price_example"])
        lines.append("")
    lines.append("<i>Переключатели меняются сразу. Для остального бот попросит значение.</i>")
    if note:
        lines += ["", note]
    return "\n".join(lines), kb(*rows, [b("◀️ К разделам", "set")])


def ask_prompt(key: str, value: Any) -> Screen:
    f = FIELDS[key]
    lines = [f"✏️ <b>{e(f.label)}</b>", "", f"Сейчас: <b>{e(f.show(value))}</b>", "", e(f.help)]
    if f.kind in ("int", "float") and (f.min is not None or f.max is not None):
        lines.append(f"<i>Допустимо: {f.min:g}–{f.max:g} {f.unit}</i>".strip())
    if f.kind == "choice":
        lines.append(f"<i>Варианты: {', '.join(f.choices)}</i>")
    if f.restart:
        lines.append("<i>Вступит в силу после перезапуска бота.</i>")
    lines += ["", "Пришлите новое значение сообщением."]
    return "\n".join(lines), kb([b("✖️ Отмена", "cancel")])


# ============================================================== тексты

def texts_list(custom: set, title_custom: bool, descr_custom: bool) -> Screen:
    lines = ["✏️ <b>Тексты</b>", "",
             "Сообщения, которые бот пишет покупателям, и шаблоны новых лотов.",
             "✏️ — изменён вами."]
    rows = [[b(("✏️ " if title_custom else "") + "🏷 Заголовок лота", "tpl:lots_title_template")],
            [b(("✏️ " if descr_custom else "") + "📄 Описание лота", "tpl:lots_description_template")]]
    keys = list(TEXT_LABELS)
    for i in range(0, len(keys), 2):
        rows.append([b(("✏️ " if k in custom else "") + TEXT_LABELS[k][0], f"txt:{k}")
                     for k in keys[i:i + 2]])
    return "\n".join(lines), kb(*rows, [b("◀️ Меню", "home")])


def text_card(key: str, current: str, is_custom: bool) -> Screen:
    label, placeholders = TEXT_LABELS.get(key, (key, ""))
    lines = [f"✏️ <b>{e(label)}</b>", "", "<b>Сейчас:</b>", f"<pre>{e(current)}</pre>"]
    if placeholders:
        lines += ["", f"<i>Подстановки: {e(placeholders)}</i>"]
    rows = [[b("✏️ Изменить", f"txte:{key}")]]
    if is_custom:
        rows.append([b("↩️ Вернуть стандартный", f"txtr:{key}")])
    rows.append([b("◀️ К текстам", "txt")])
    return "\n".join(lines), kb(*rows)


def template_card(key: str, current: str, example: str) -> Screen:
    f = FIELDS[key]
    lines = [f"✏️ <b>{e(f.label)}</b>", "",
             f"Шаблон: <b>{'свой' if current else 'стандартный'}</b>", "",
             "<b>Пример для игры PEAK:</b>", f"<pre>{e(example[:1500])}</pre>", "",
             e(f.help), "", "<i>Уже созданные лоты не меняются — шаблон действует на новые.</i>"]
    rows = [[b("✏️ Изменить", f"ask:{key}:tpl")]]
    if current:
        rows.append([b("↩️ Вернуть стандартный", f"tplr:{key}")])
    rows.append([b("◀️ К текстам", "txt")])
    return "\n".join(lines), kb(*rows)


def text_edit_prompt(key: str) -> Screen:
    label, placeholders = TEXT_LABELS.get(key, (key, ""))
    default = DEFAULT_TEXTS.get(key, "")
    return (
        f"✏️ <b>{e(label)}</b>\n\nПришлите новый текст сообщением."
        + (f"\n\n<i>Можно использовать: {e(placeholders)}</i>" if placeholders else "")
        + f"\n\n<b>Стандартный:</b>\n<pre>{e(default)}</pre>",
        kb([b("✖️ Отмена", "cancel")]),
    )


# ============================================================== помощь

def help_screen() -> Screen:
    return (
        "❓ <b>Как это работает</b>\n\n"
        "1️⃣ Покупатель оплачивает лот «🎮 АРЕНДА [Игра]» на Starvell.\n"
        "2️⃣ Бот видит заказ, находит игру в KOSell и арендует аккаунт на "
        "<b>количество штук × часов в штуке</b>.\n"
        "3️⃣ Логин и пароль приходят покупателю в чат заказа.\n"
        "4️⃣ Повторная оплата той же игры — продление, а не новый аккаунт.\n\n"
        "<b>Команды покупателя в чате</b>\n"
        "!наличие Игра — свободен ли аккаунт\n"
        "!код — код Steam Guard\n"
        "!прод логин — выбрать, какой аккаунт продлить\n"
        "!друг — следующая оплата выдаст отдельный аккаунт\n"
        "!мои — активные аренды\n\n"
        "<b>Команды этого бота</b>\n"
        "/menu — главное меню\n/lots — мои лоты\n/new — создать лоты\n"
        "/stats — статистика\n/balance — баланс KOSell\n/id — ваш Telegram ID\n\n"
        "<b>Если что-то не так</b>\n"
        "• Заказы не выдаются — проверьте, что выключен 🧪 тест и хватает баланса.\n"
        "• «cookie устарел» — обновите cookie Starvell в ⚙️ → 🔌 Подключения.\n"
        "• Панель пропадает — провайдер режет Telegram, нужен прокси там же.",
        back("home", "◀️ Меню"),
    )


# ============================================================== ротация

def rotation(pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]], weak_total: int,
             settings: Dict[str, Any]) -> Screen:
    days, views = settings.get("days", 7), settings.get("views", 5)
    lines = ["♻️ <b>Замена слабых лотов</b>", "",
             f"Слабый лот: старше {days} дн, не больше {views} просмотров и без продаж.", ""]
    if not weak_total:
        lines.append("Слабых лотов нет — все работают или ещё слишком новые.")
        return "\n".join(lines), kb([b("⚙️ Критерии", "setg:demand")], [b("◀️ К лотам", "lots:0")])
    if not pairs:
        lines.append(f"Слабых лотов: {weak_total}, но заменить их нечем — все подходящие "
                     "игры уже выставлены.")
        return "\n".join(lines), kb([b("◀️ К лотам", "lots:0")])
    lines.append(f"Заменю <b>{len(pairs)}</b> из {weak_total}:")
    for w, c in pairs[:12]:
        icon = TIER_ICON.get(c.get("tier") or "", "▫️")
        lines.append(f"❌ {e(w['game'][:24])} <i>({w['views']} просм., {w['age']:g} дн)</i>\n"
                     f"   → {icon} {e(c['product_name'][:28])} <i>(пик {num(c.get('players'))})</i>")
    if len(pairs) > 12:
        lines.append(f"… и ещё {len(pairs) - 12}")
    lines += ["", "Слабый лот удаляется, на его место создаётся новый. Статистика и "
                  "привязки сохранятся."]
    return "\n".join(lines), kb([b(f"♻️ Заменить {len(pairs)}", "rot_go")],
                                [b("⚙️ Критерии", "setg:demand"), b("◀️ Назад", "lots:0")])
