"""Экраны и команды SMM-модуля (OptSMM) в Telegram-панели.

Подмешивается в Panel: использует её self.smm, self.store, _show, Ask, Ctx.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from aiogram.filters import Command, CommandObject
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from core.optsmm import OptSmmError
from core.smm import LINK_TITLES, LINK_TYPES, STATE_ICON, STATE_TITLE
from tgpanel import views

b, kb, e, money = views.b, views.kb, views.e, views.money

RULE_HELP = (
    "Пришлите одной строкой:\n"
    "<code>слова из названия лота | id услуги | штук в 1 ед. | тип ссылки</code>\n\n"
    "Пример:\n<code>подписчики, telegram | 1234 | 100 | tg</code>\n"
    "→ лот, в названии которого есть «подписчики» и «telegram»; 1 шт лота = 100 подписчиков.\n\n"
    "Типы ссылок: " + ", ".join(f"<code>{k}</code> ({v})" for k, v in LINK_TITLES.items()) + "\n"
    "Для регулярного выражения начните с <code>re:</code>\n\n"
    "⚠️ Слова должны отличать SMM-лот от лотов аренды."
)


class SmmPanelMixin:
    smm: Any
    _smm_found: List[Dict[str, Any]]

    def _smm_routes(self) -> Dict[str, Any]:
        return {"smm": self.r_smm, "smm_rules": self.r_smm_rules, "smm_rule": self.r_smm_rule,
                "smm_rt": self.r_smm_rule_toggle, "smm_rd": self.r_smm_rule_delete,
                "smm_radd": self.r_smm_rule_add, "smm_find": self.r_smm_find,
                "smm_svc": self.r_smm_services, "smm_orders": self.r_smm_orders}

    def _register_smm_commands(self, dp: Any, Ctx: Any) -> None:
        @dp.message(Command("smm"))
        async def c_smm(message: Message, state) -> None:
            await state.clear()
            await self._show(message, await self.r_smm(Ctx(None, state, message), ""))

        @dp.message(Command("smm_link"))
        async def c_smm_link(message: Message, command: CommandObject) -> None:
            parts = (command.args or "").split(maxsplit=1)
            if len(parts) < 2:
                await message.answer("Формат: /smm_link &lt;id заказа&gt; &lt;ссылка&gt;", parse_mode="HTML")
                return
            await message.answer("📣 " + e(await self.smm.manual_link(parts[0], parts[1])), parse_mode="HTML")

        @dp.message(Command("smm_refund"))
        async def c_smm_refund(message: Message, command: CommandObject) -> None:
            ref = (command.args or "").strip()
            if not ref:
                await message.answer("Формат: /smm_refund &lt;id заказа&gt;", parse_mode="HTML")
                return
            await message.answer("📣 " + e(await self.smm.manual_refund(ref)), parse_mode="HTML")

        @dp.message(Command("smm_calc"))
        async def c_smm_calc(message: Message, command: CommandObject) -> None:
            a = (command.args or "").split()
            if len(a) < 2:
                await message.answer("Формат: /smm_calc &lt;id услуги&gt; &lt;штук&gt; [наценка %]",
                                     parse_mode="HTML")
                return
            await message.answer(await self._smm_calc(a[0], int(a[1]), float(a[2]) if len(a) > 2 else None),
                                 parse_mode="HTML")

    # ------------------------------------------------------------ расчёт цены

    async def _smm_calc(self, service_id: str, qty: int, markup: Optional[float]) -> str:
        try:
            svc = await self.smm.api.service(service_id)
            _, cur = await self.smm.api.balance()
        except OptSmmError as exc:
            return f"❌ OptSMM: {e(exc)}"
        if not svc:
            return "❌ Такой услуги нет."
        if markup is None:
            markup = float(self.store.get("lots_markup_percent", 60) or 60)
        fee = float(self.store.get("lots_commission_percent", 0) or 0) / 100
        cost = self.smm.to_rub(float(svc.get("rate") or 0) * qty / 1000, cur)
        price = cost * (1 + markup / 100) / (1 - fee) if fee < 1 else cost
        return (f"🧮 <b>{e(svc.get('name'))}</b>\n{qty} шт · лимиты {svc.get('min')}–{svc.get('max')}\n\n"
                f"Себестоимость: <b>{money(cost)}</b>\n"
                f"Цена лота (наценка {markup:g}%, комиссия {fee * 100:g}%): <b>{money(price)}</b>\n"
                f"Прибыль с продажи ≈ {money(price * (1 - fee) - cost)}")

    # ------------------------------------------------------------ главный экран SMM

    async def r_smm(self, ctx: Any, arg: str) -> views.Screen:
        smm = self.smm
        lines = ["📣 <b>SMM · OptSMM</b>", ""]
        if not smm.api.configured:
            lines += ["❗️ Не задан ключ OptSMM.", "⚙️ Настройки → 🔌 Подключения → Ключ OptSMM", ""]
        else:
            try:
                bal, cur = await self._soft(smm.api.balance(), 10) or (None, "")
            except Exception:
                bal, cur = None, ""
            lines.append(f"💰 Баланс OptSMM: <b>{bal:.2f} {e(cur)}</b>" if bal is not None
                         else "💰 Баланс OptSMM: <i>нет ответа</i>")
        on = self.store.get("smm_enabled", True)
        lines.append(f"{'🟢 Включён' if on else '⏸ Выключен'} · правил: <b>{len(smm.rules)}</b>")
        s = smm.summary()
        lines += [
            f"⏳ Ждут ссылку: {s['asked']} · 🚀 В работе: {s['running']}"
            + (f" · 💸 Ждут баланс: {s['wait_balance']}" if s["wait_balance"] else ""),
            f"✅ Выполнено: {s['done']} · выручка {money(s['revenue'])} · себест. {money(s['cost'])}",
        ]
        if s["manual"]:
            lines.append(f"✋ Требуют внимания: <b>{s['manual']}</b>")
        if smm.last_error:
            lines += ["", f"⚠️ <i>{e(smm.last_error[:120])}</i>"]
        if not smm.rules:
            lines += ["", "Добавьте правило: какой лот → какая услуга OptSMM."]
        lines += ["", "<i>Команды: /smm_link id ссылка · /smm_refund id · /smm_calc услуга штук [наценка]</i>"]
        return "\n".join(lines), kb(
            [b("📋 Правила", "smm_rules"), b("🧾 Заказы", "smm_orders")],
            [b("🔎 Услуги OptSMM", "smm_find"), b("➕ Правило", "smm_radd")],
            [b("⚙️ Настройки SMM", "setg:smm"), b("🔄 Обновить", "smm")],
            [b("◀️ Меню", "home")])

    async def r_smm_orders(self, ctx: Any, arg: str) -> views.Screen:
        items = sorted(self.smm.orders.values(), key=lambda o: o.get("created", 0), reverse=True)[:20]
        lines = ["🧾 <b>SMM-заказы</b> (последние 20)", ""]
        if not items:
            lines.append("Пока пусто.")
        for o in items:
            lines.append(
                f"{STATE_ICON.get(o['state'], '•')} <code>{e(o.get('short'))}</code> "
                f"{e(o.get('buyer'))} · {o.get('qty')} шт · {money(o.get('revenue'))}"
                + (f" · #{o['smm_id']}" if o.get("smm_id") else "")
                + f"\n   <i>{e(STATE_TITLE.get(o['state'], o['state']))}"
                + (f": {e(str(o.get('note'))[:60])}" if o.get("note") else "") + "</i>")
        return "\n".join(lines), kb([b("🔄 Обновить", "smm_orders"), b("◀️ SMM", "smm")])

    # ------------------------------------------------------------ правила

    async def r_smm_rules(self, ctx: Any, arg: str) -> views.Screen:
        lines = ["📋 <b>Правила SMM</b>", "",
                 "Лот, в названии которого есть все слова правила, обрабатывается через OptSMM, "
                 "а не как аренда.", ""]
        rows = []
        for r in self.smm.rules:
            mark = "✅" if r.get("enabled", True) else "⏸"
            lines.append(f"{mark} <b>{e(r.get('name'))}</b> → #{r['service']} · ×{r['per_unit']} · "
                         f"{LINK_TITLES.get(r.get('link'), r.get('link'))}")
            rows.append([b(f"{mark} {r.get('name')}"[:40], f"smm_rule:{r['id']}")])
        if not self.smm.rules:
            lines.append("Правил пока нет.")
        return "\n".join(lines), kb(*rows, [b("➕ Правило", "smm_radd"), b("◀️ SMM", "smm")])

    async def r_smm_rule(self, ctx: Any, rid: str) -> views.Screen:
        r = self.smm.rule_by_id(rid)
        if not r:
            return await self.r_smm_rules(ctx, "")
        svc = None
        try:
            svc = await self._soft(self.smm.api.service(r["service"]), 10)
        except Exception:
            pass
        lines = [f"📋 <b>{e(r.get('name'))}</b>", "",
                 f"Слова в названии: <code>{e(r.get('match'))}</code>",
                 f"Услуга: #{r['service']}" + (f" — {e(svc.get('name'))}" if svc else " — <i>не найдена</i>"),
                 f"1 шт лота = {r['per_unit']} шт услуги",
                 f"Ссылка: {LINK_TITLES.get(r.get('link'), r.get('link'))}",
                 f"Статус: {'✅ включено' if r.get('enabled', True) else '⏸ выключено'}"]
        if svc:
            unit_cost = self.smm.to_rub(float(svc.get("rate") or 0) * r["per_unit"] / 1000, "RUB")
            lines += ["", f"Себестоимость 1 шт лота ≈ <b>{money(unit_cost)}</b>",
                      f"Лимиты услуги: {svc.get('min')}–{svc.get('max')} → "
                      f"от {-(-int(svc.get('min') or 1) // r['per_unit'])} шт лота"]
        return "\n".join(lines), kb(
            [b("⏸ Выключить" if r.get("enabled", True) else "▶️ Включить", f"smm_rt:{rid}"),
             b("🗑 Удалить", f"smm_rd:{rid}")],
            [b("◀️ Правила", "smm_rules")])

    async def r_smm_rule_toggle(self, ctx: Any, rid: str) -> views.Screen:
        r = self.smm.rule_by_id(rid)
        if r:
            r["enabled"] = not r.get("enabled", True)
            self.smm.save()
        return await self.r_smm_rule(ctx, rid)

    async def r_smm_rule_delete(self, ctx: Any, rid: str) -> views.Screen:
        self.smm.delete_rule(rid)
        return await self.r_smm_rules(ctx, "")

    async def r_smm_rule_add(self, ctx: Any, arg: str) -> views.Screen:
        from tgpanel.panel import Ask
        await ctx.state.set_state(Ask.value)
        await ctx.state.update_data(mode="smm_rule", back="smm_rules")
        return "➕ <b>Новое правило SMM</b>\n\n" + RULE_HELP, kb([b("✖️ Отмена", "cancel")])

    # ------------------------------------------------------------ поиск услуг

    async def r_smm_find(self, ctx: Any, arg: str) -> views.Screen:
        from tgpanel.panel import Ask
        await ctx.state.set_state(Ask.value)
        await ctx.state.update_data(mode="smm_search", back="smm_svc")
        return ("🔎 <b>Поиск услуг OptSMM</b>\n\nПришлите слова для поиска, например:\n"
                "<code>telegram подписчики</code> или <code>tiktok просмотры</code>",
                kb([b("✖️ Отмена", "cancel")]))

    async def r_smm_services(self, ctx: Any, arg: str) -> views.Screen:
        found = getattr(self, "_smm_found", []) or []
        lines = [f"🔎 <b>Найдено услуг: {len(found)}</b>", "",
                 "<i>id · название · цена за 1000 · мин–макс</i>", ""]
        for s in found[:35]:
            lines.append(f"<code>{e(s.get('service'))}</code> {e(str(s.get('name'))[:60])} — "
                         f"<b>{e(s.get('rate'))}</b> · {s.get('min')}–{s.get('max')}"
                         + (" ♻️" if s.get("refill") else ""))
        if len(found) > 35:
            lines.append(f"\n…ещё {len(found) - 35}, уточните запрос")
        if not found:
            lines.append("Ничего не нашлось.")
        return "\n".join(lines), kb([b("🔎 Искать ещё", "smm_find"), b("➕ Правило", "smm_radd")],
                                    [b("◀️ SMM", "smm")])

    # ------------------------------------------------------------ ввод значений

    async def _smm_apply_value(self, mode: str, raw: str) -> str:
        if mode == "smm_search":
            words = raw.lower().split()
            try:
                services = await self.smm.api.services(force=True)
            except OptSmmError as exc:
                raise ValueError(f"OptSMM: {exc}") from None
            self._smm_found = [s for s in services if all(
                w in f"{s.get('name', '')} {s.get('category', '')} {s.get('type', '')}".lower()
                for w in words)]
            return ""
        if mode == "smm_rule":
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) < 2:
                raise ValueError("нужно минимум «слова | id услуги»")
            match = parts[0]
            try:
                service = int(parts[1])
                per_unit = int(parts[2]) if len(parts) > 2 and parts[2] else 1
            except ValueError:
                raise ValueError("id услуги и количество — числа") from None
            link = (parts[3].lower() if len(parts) > 3 and parts[3] else "any")
            if link not in LINK_TYPES:
                raise ValueError(f"тип ссылки: {', '.join(LINK_TYPES)}")
            svc = None
            try:
                svc = await self.smm.api.service(service)
            except OptSmmError:
                pass
            name = (svc or {}).get("name") or match
            rule = self.smm.add_rule(match, service, per_unit, link, name=str(name)[:60])
            return (f"✅ Правило добавлено: «{e(match)}» → #{service}"
                    + ("" if svc else " <i>(услуга не проверена — OptSMM не ответил или её нет)</i>")
                    + f"\nid правила: {rule['id']}")
        raise ValueError("неизвестное действие")
