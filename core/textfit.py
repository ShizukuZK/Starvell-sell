"""Подгонка текстов лотов под лимиты Starvell.

Starvell валидирует поля через class-validator (validator.js isLength),
который считает длину так:  длина в UTF-16
                           − суррогатные пары (эмодзи вне BMP)
                           − селекторы начертания U+FE0E / U+FE0F.
На практике это «число кодпоинтов минус вариационные селекторы».
Пример: «⏱️» = 2 кодпоинта, но 1 символ для Starvell.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

BRIEF_LIMIT = 100          # briefDescription (заголовок лота)
DESCRIPTION_LIMIT = 5000   # подробное описание — с большим запасом

_VARIATION = ("︎", "️")


def starvell_len(text: str) -> int:
    """Длина строки так, как её считает Starvell."""
    text = text or ""
    return len(text) - sum(text.count(v) for v in _VARIATION)


def fits(text: str, limit: int = BRIEF_LIMIT) -> bool:
    return starvell_len(text) <= limit


def min_qty_label(min_quantity: int) -> str:
    """«(ОТ 3-Х ШТ)» — только если минимум больше одной штуки."""
    n = int(min_quantity or 1)
    if n <= 1:
        return ""
    if n in (2, 3, 4):
        return f" (ОТ {n}-Х ШТ)"
    return f" (ОТ {n} ШТ)"


def unit_label(hours_per_unit: int) -> str:
    h = int(hours_per_unit or 1)
    return "1 ЧАС" if h == 1 else f"{h} Ч."


# Шаблоны заголовка от самого полного к самому короткому.
# Бот берёт первый, который влезает в лимит. Во всех есть «АРЕНДА [игра]» —
# по нему бот потом узнаёт игру в заказе.
DEFAULT_TITLE_TEMPLATES: List[str] = [
    "🎮 АРЕНДА [{game}] ⚡ АВТОВЫДАЧА 24/7 ⚡ ⏱️ 1 ШТ = {unit}{min} 🚀 СВОБОДЕН? ПИШИ !наличие",
    "🎮 АРЕНДА [{game}] ⚡ АВТОВЫДАЧА 24/7 ⚡ ⏱️ 1 ШТ = {unit}{min} 🚀 !наличие",
    "🎮 АРЕНДА [{game}] ⚡ АВТОВЫДАЧА 24/7 ⏱️ 1 ШТ = {unit}{min}",
    "🎮 АРЕНДА [{game}] ⚡ АВТОВЫДАЧА ⏱️ 1 ШТ = {unit}",
    "🎮 АРЕНДА [{game}] ⏱️ 1 ШТ = {unit}",
    "🎮 АРЕНДА [{game}]",
]


def build_title(
    game: str,
    *,
    hours_per_unit: int = 1,
    min_quantity: int = 1,
    templates: Optional[Iterable[str]] = None,
    limit: int = BRIEF_LIMIT,
) -> str:
    """Самый информативный заголовок, который влезает в лимит.

    Если даже минимальный шаблон не влезает (очень длинное название),
    название игры аккуратно укорачивается с многоточием.
    """
    tpls = [t for t in (templates or DEFAULT_TITLE_TEMPLATES) if "{game}" in t]
    if not tpls:
        tpls = DEFAULT_TITLE_TEMPLATES
    params = {"unit": unit_label(hours_per_unit), "min": min_qty_label(min_quantity)}

    for tpl in tpls:
        title = _render(tpl, game=game, **params)
        if fits(title, limit):
            return title

    # крайний случай: режем название игры
    shortest = min(tpls, key=lambda t: starvell_len(_render(t, game="", **params)))
    budget = limit - starvell_len(_render(shortest, game="", **params)) - 1
    cut = game
    while cut and starvell_len(cut) > budget:
        cut = cut[:-1]
    cut = cut.rstrip(" -–—:,.") + "…"
    return _render(shortest, game=cut, **params)


DEFAULT_DESCRIPTION_TEMPLATE = (
    "🎮 АВТОМАТИЧЕСКИЙ СЕРВИС АРЕНДЫ\n\n"
    "Вы приобретаете индивидуальный доступ к лицензионному аккаунту "
    "с игрой: {game}\n\n"
    "🔎 ПРОВЕРКА НАЛИЧИЯ ПЕРЕД ОПЛАТОЙ\n"
    "Напишите в чат команду !наличие — робот сразу ответит, свободен ли "
    "аккаунт, а если занят, назовёт время освобождения.\n\n"
    "📊 КАК ОФОРМИТЬ ЗАКАЗ\n"
    "• 1 штука товара = {unit_text} аренды.\n"
    "{min_line}"
    "\n🚀 ПРЕИМУЩЕСТВА\n"
    "✅ Данные выдаются в чат сразу после оплаты\n"
    "✅ В ваше время на аккаунте играете только вы\n"
    "✅ Прогресс сохраняется локально на вашем ПК\n"
    "✅ Steam Guard по команде !код за пару секунд\n"
    "✅ Продление — просто оплатите нужное количество штук ещё раз\n\n"
    "⏱️ Время аренды начинает течь с момента первого успешного входа.\n"
    "Запрещено менять пароль, почту и другие данные аккаунта."
)


def build_description(
    game: str,
    *,
    hours_per_unit: int = 1,
    min_quantity: int = 1,
    template: Optional[str] = None,
) -> str:
    h = int(hours_per_unit or 1)
    n = int(min_quantity or 1)
    min_line = (
        f"• ⚠️ Минимальный заказ — {n} шт. Заказы меньше отменяются роботом "
        f"автоматически.\n"
        if n > 1 else ""
    )
    text = _render(
        template or DEFAULT_DESCRIPTION_TEMPLATE,
        game=game,
        unit_text="1 час" if h == 1 else f"{h} ч.",
        min_line=min_line,
        min=n,
        unit=unit_label(h),
    )
    if starvell_len(text) > DESCRIPTION_LIMIT:
        text = text[:DESCRIPTION_LIMIT - 1] + "…"
    return text


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _render(template: str, **values: object) -> str:
    try:
        return template.format_map(_Safe(**values))
    except (ValueError, IndexError):
        return template
