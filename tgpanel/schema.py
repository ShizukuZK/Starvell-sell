"""Описание настроек и текстов для панели: подписи, подсказки, границы."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str                       # bool | int | float | str | secret | choice | admins | proxy | template | repo
    help: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    unit: str = ""
    choices: Tuple[str, ...] = field(default_factory=tuple)
    restart: bool = False           # вступает в силу после перезапуска

    def show(self, value: Any) -> str:
        if self.kind == "bool":
            return "вкл" if value else "выкл"
        if self.kind == "secret":
            v = str(value or "").strip()
            return f"…{v[-4:]}" if v else "не задан"
        if self.kind == "proxy":
            v = str(value or "").strip()
            if not v:
                return "нет"
            return f"{v.split('://')[0]}://***@{v.split('@')[-1]}" if "@" in v else v
        if self.kind == "admins":
            return ", ".join(str(a) for a in value or []) or "нет"
        if self.kind == "repo":
            return str(value or "").strip() or "не задан"
        if self.kind == "template":
            return "свой" if str(value or "").strip() else "стандартный"
        if value in (None, ""):
            return "—"
        if self.kind == "float":
            v = float(value)
            text = f"{v:g}"
        else:
            text = str(value)
        return f"{text} {self.unit}".strip()

    def parse(self, raw: str) -> Any:
        """Проверяет ввод. Бросает ValueError с понятным текстом."""
        raw = (raw or "").strip()
        if self.kind in ("int", "float"):
            try:
                value = float(raw.replace(",", ".").replace(" ", ""))
            except ValueError:
                raise ValueError("нужно число") from None
            if self.kind == "int":
                if value != int(value):
                    raise ValueError("нужно целое число")
                value = int(value)
            if self.min is not None and value < self.min:
                raise ValueError(f"не меньше {self.min:g}")
            if self.max is not None and value > self.max:
                raise ValueError(f"не больше {self.max:g}")
            return value
        if self.kind == "choice":
            up = raw.upper()
            if up not in self.choices:
                raise ValueError("варианты: " + ", ".join(self.choices))
            return up
        if self.kind == "admins":
            parts = raw.replace(",", " ").split()
            try:
                ids = [int(p) for p in parts]
            except ValueError:
                raise ValueError("нужны числовые Telegram ID через пробел") from None
            if not ids:
                raise ValueError("нужен хотя бы один ID")
            return ids
        if self.kind == "proxy":
            if raw in ("-", "—", "нет", "0"):
                return ""
            if not raw.startswith(("socks5://", "socks4://", "http://", "https://")):
                raise ValueError("формат: socks5://логин:пароль@хост:порт или http://…")
            return raw
        if self.kind == "repo":
            if raw in ("-", "—", "нет", "0"):
                return ""
            from core.updates import normalize_repo
            repo = normalize_repo(raw)
            if not repo:
                raise ValueError("формат: владелец/репозиторий или ссылка github.com/…")
            return repo
        if self.kind == "template":
            if raw in ("-", "—"):
                return ""
            if "{game}" not in raw:
                raise ValueError("в шаблоне должен быть {game} — туда подставится название")
            return raw
        if not raw:
            raise ValueError("пустое значение")
        return raw


FIELDS: Dict[str, Field] = {f.key: f for f in [
    # --- продажи и выдача ---
    Field("min_quantity", "Минимальный заказ", "int",
          "Сколько штук минимум можно купить. Заказы меньше бот вернёт сам "
          "(если включён возврат ниже минимума). Попадает в заголовок новых лотов.",
          1, 100, "шт"),
    Field("hours_per_unit", "Часов в одной штуке", "int",
          "Сколько часов аренды даёт одна купленная штука. Обычно 1.", 1, 24, "ч"),
    Field("auto_refund_below_min", "Возврат ниже минимума", "bool",
          "Автоматически возвращать деньги за заказы меньше минимума."),
    Field("auto_refund", "Возврат при сбое", "bool",
          "Если выдать аккаунт не удалось (нет свободных, мало денег) — вернуть деньги."),
    Field("auto_confirm_order", "Подтверждать заказ", "bool",
          "Сразу подтверждать заказ после выдачи. Обычно покупатель делает это сам."),
    Field("greeting_enabled", "Приветствие покупателю", "bool",
          "На первое сообщение покупателя — приветствие с инструкцией и наличием "
          "игры, которую он смотрит. Текст меняется в ✏️ Тексты."),
    Field("greeting_cooldown_hours", "Приветствие не чаще", "float",
          "Раз в сколько часов можно снова поприветствовать того же покупателя.",
          1, 720, "ч"),
    Field("friend_minutes", "Окно «для друга»", "int",
          "Сколько минут действует команда !друг — следующая оплата даст отдельный аккаунт.",
          1, 120, "мин"),
    Field("code_cooldown_seconds", "Антиспам !код", "int",
          "Минимальный интервал между запросами Steam Guard от одного покупателя.",
          0, 600, "сек"),
    Field("currency", "Валюта оплаты KOSell", "choice",
          "С какого баланса KOSell списывать аренду.", choices=("RUB", "USD")),
    # --- цены ---
    Field("lots_markup_percent", "Наценка", "float",
          "На сколько процентов цена выше себестоимости часа в KOSell.", 0, 5000, "%"),
    Field("lots_commission_percent", "Комиссия Starvell", "float",
          "Закладывается в цену, чтобы наценка осталась после комиссии.", 0, 50, "%"),
    Field("lots_min_price_rub", "Мин. цена обычной", "float",
          "Цена за штуку не опустится ниже этого значения (для игр без отметки спроса).",
          0.5, 10000, "₽"),
    Field("lots_round_to", "Округление цены", "float",
          "Шаг, до которого цена округляется вверх: 0.01 — до копейки, 1 — до рубля.",
          0.01, 100, "₽"),
    # --- обслуживание лотов ---
    Field("stock_sync_enabled", "Сверка остатков", "bool",
          "Регулярно ставить в лотах столько штук, сколько свободных аккаунтов в KOSell."),
    Field("stock_sync_minutes", "Интервал сверки", "int",
          "Как часто сверять остатки.", 5, 720, "мин"),
    Field("auto_hide_no_stock", "Скрывать пустые лоты", "bool",
          "Когда в KOSell 0 аккаунтов — снять лот с продажи, а когда появятся — вернуть. "
          "Лоты, скрытые вами вручную, бот не трогает."),
    Field("auto_bump_enabled", "Автоподнятие", "bool",
          "Периодически поднимать лоты в списке Starvell."),
    Field("auto_bump_hours", "Интервал поднятия", "float",
          "Раз в сколько часов поднимать. У Starvell есть перерыв между поднятиями.",
          1, 48, "ч"),
    Field("auto_map_by_name", "Автопоиск товара", "bool",
          "Находить игру в каталоге KOSell по названию из лота без ручной привязки."),
    Field("lots_batch_pause", "Пауза между лотами", "float",
          "Пауза при массовом создании, чтобы Starvell не ограничил запросы.",
          0.5, 30, "сек"),
    # --- спрос и цены ---
    Field("demand_enabled", "Учитывать спрос Steam", "bool",
          "Брать из Steam онлайн и отзывы игр: лучшие игры выставляются первыми, "
          "а хиты стоят дороже."),
    Field("demand_hot_players", "🔥 Хит — от", "int",
          "Пиковый онлайн в Steam за последние ~2 суток, начиная с которого игра "
          "считается хитом.", 100, 1000000, "онлайн"),
    Field("demand_popular_players", "⭐ Популярная — от", "int",
          "Пиковый онлайн, начиная с которого игра популярная. Игры с 20 тыс.+ "
          "отзывов тоже считаются популярными.", 10, 1000000, "онлайн"),
    Field("price_floor_hot", "Мин. цена хита", "float",
          "Цена за штуку для 🔥 хитов не опустится ниже этого.", 0.5, 10000, "₽"),
    Field("price_floor_popular", "Мин. цена популярной", "float",
          "Цена за штуку для ⭐ популярных игр не опустится ниже этого.", 0.5, 10000, "₽"),
    Field("lots_min_stock", "Мин. свободных аккаунтов", "int",
          "Выставлять игру, только если в KOSell сейчас свободно не меньше стольких "
          "аккаунтов.", 1, 100, "шт"),
    Field("lots_max_cost_rub", "Макс. себестоимость часа", "float",
          "Не выставлять игры, которые KOSell сдаёт дороже этого за час. У некоторых "
          "игр там стоит 74 ₽/ч — по такой цене их не арендуют, а место в лимите они займут. "
          "0 — без ограничения.", 0, 100000, "₽/ч"),
    Field("auto_reprice_enabled", "Автопересчёт цен", "bool",
          "Периодически пересчитывать цены лотов по наценке и спросу."),
    Field("auto_reprice_hours", "Интервал пересчёта", "float",
          "Раз в сколько часов пересчитывать цены.", 1, 168, "ч"),
    Field("rotate_after_days", "Слабый лот — через", "int",
          "Лот оценивается не раньше чем через столько дней после создания.", 1, 90, "дн"),
    Field("rotate_max_views", "Слабый лот — просмотров до", "int",
          "Лот слабый, если у него не больше стольких просмотров и не было продаж.",
          0, 1000, ""),
    # --- уведомления ---
    Field("notify_sales", "О продажах", "bool", "Присылать сообщение о каждой продаже."),
    Field("notify_rental_end", "Покупателю об окончании", "bool",
          "Писать покупателю за 10 минут до конца аренды и по её окончании."),
    Field("balance_alert_rub", "Порог баланса KOSell", "float",
          "Предупредить, когда баланс опустится ниже. 0 — не предупреждать.",
          0, 1000000, "₽"),
    # --- подключения ---
    Field("kosell_api_key", "Ключ KOSell", "secret",
          "API-ключ из профиля на kosell.store.", restart=True),
    Field("starvell_session", "Cookie Starvell", "secret",
          "Браузер → F12 → Application → Cookies → starvell.com → значение session."),
    Field("tg_proxy", "Прокси Telegram", "proxy",
          "Нужен, если провайдер блокирует api.telegram.org.\n"
          "Формат: socks5://логин:пароль@хост:1080\nОтправьте «-», чтобы убрать.",
          restart=True),
    Field("proxy_url", "Прокси площадок", "proxy",
          "Для Starvell и KOSell. Обычно не нужен. «-» — убрать.", restart=True),
    Field("tg_admins", "Администраторы", "admins",
          "Telegram ID через пробел. Свой ID покажет команда /id."),
    Field("optsmm_api_key", "Ключ OptSMM", "secret",
          "API-ключ со страницы optsmm.ru/developer."),
    # --- SMM ---
    Field("smm_enabled", "SMM-модуль", "bool",
          "Обрабатывать SMM-лоты через OptSMM (нужны ключ и хотя бы одно правило)."),
    Field("smm_auto_refund", "SMM: авто-возврат", "bool",
          "Возвращать деньги, если OptSMM отменил заказ или количество меньше минимума услуги."),
    Field("smm_remind_minutes", "SMM: напомнить о ссылке", "int",
          "Через сколько минут напомнить покупателю прислать ссылку.", 5, 1440, "мин"),
    Field("smm_link_timeout_hours", "SMM: ждать ссылку", "int",
          "Через сколько часов без ссылки сообщить вам.", 1, 168, "ч"),
    Field("smm_usd_rate", "SMM: курс USD", "float",
          "Нужен, только если баланс OptSMM в долларах.", 1, 1000, "₽"),
    Field("smm_poll_seconds", "SMM: проверка статусов", "int",
          "Как часто спрашивать OptSMM о статусе заказов.", 15, 600, "сек"),
    # --- опрос ---
    Field("orders_poll_interval", "Опрос заказов", "int",
          "Как часто проверять новые заказы.", 5, 300, "сек"),
    Field("chats_poll_interval", "Опрос чатов", "int",
          "Как часто проверять команды покупателей (!код, !наличие).", 3, 120, "сек"),
    Field("tz_offset_hours", "Часовой пояс", "int",
          "Смещение от UTC для времени окончания аренды. Москва — 3.", -12, 14, "ч"),
    Field("github_repo", "Репозиторий GitHub", "repo",
          "Откуда проверять новые версии: владелец/репозиторий или ссылка "
          "на GitHub. Если пусто — берётся из git сам. «-» — очистить."),
    Field("github_token", "Токен GitHub", "secret",
          "Нужен только для приватного репозитория. github.com → Settings → "
          "Developer settings → Fine-grained tokens: доступ к одному репозиторию, "
          "право Contents: Read-only."),
    Field("update_check_enabled", "Проверять обновления", "bool",
          "Раз в 6 часов смотреть релизы на GitHub и сообщать о новой версии."),
    # --- шаблоны лотов ---
    Field("lots_title_template", "Заголовок лота", "template",
          "Свой шаблон заголовка. Обязательно {game}. Можно {unit} (1 ЧАС) и "
          "{min} ( (ОТ 3-Х ШТ)).\nЕсли не влезет в 100 символов — бот возьмёт "
          "более короткий встроенный вариант. «-» — вернуть стандартный."),
    Field("lots_description_template", "Описание лота", "template",
          "Свой шаблон описания. Обязательно {game}. Можно {unit_text} (1 час), "
          "{min_line} (строка про минимум), {min} (число).\n«-» — вернуть стандартный."),
]}


GROUPS: List[Tuple[str, str, List[str]]] = [
    ("sale", "🛒 Продажи и выдача", [
        "min_quantity", "hours_per_unit", "greeting_enabled", "greeting_cooldown_hours",
        "auto_refund_below_min", "auto_refund",
        "auto_confirm_order", "friend_minutes", "code_cooldown_seconds", "currency"]),
    ("price", "💵 Цены", [
        "lots_markup_percent", "lots_commission_percent", "lots_min_price_rub",
        "price_floor_popular", "price_floor_hot", "lots_round_to",
        "auto_reprice_enabled", "auto_reprice_hours"]),
    ("demand", "📊 Спрос и отбор", [
        "demand_enabled", "demand_hot_players", "demand_popular_players",
        "lots_min_stock", "lots_max_cost_rub", "rotate_after_days", "rotate_max_views"]),
    ("lots", "📦 Обслуживание лотов", [
        "stock_sync_enabled", "stock_sync_minutes", "auto_hide_no_stock",
        "auto_bump_enabled", "auto_bump_hours", "auto_map_by_name", "lots_batch_pause"]),
    ("notify", "🔔 Уведомления", ["notify_sales", "notify_rental_end", "balance_alert_rub"]),
    ("smm", "📣 SMM (OptSMM)", [
        "smm_enabled", "smm_auto_refund", "smm_remind_minutes", "smm_link_timeout_hours",
        "smm_usd_rate", "smm_poll_seconds"]),
    ("conn", "🔌 Подключения", [
        "kosell_api_key", "optsmm_api_key", "starvell_session", "tg_proxy", "proxy_url", "tg_admins"]),
    ("tech", "⏱ Опрос и обновления", [
        "orders_poll_interval", "chats_poll_interval", "tz_offset_hours",
        "github_repo", "github_token", "update_check_enabled"]),
]
GROUP_TITLES = {gid: title for gid, title, _ in GROUPS}


TEXT_LABELS: Dict[str, Tuple[str, str]] = {
    "greeting": ("👋 Приветствие", "{unit} {min_line} {stock_line}"),
    "delivery": ("🎮 Выдача аккаунта", "{game} {login} {password} {hours} {expires}"),
    "extension": ("🔁 Продление", "{login} {game} {hours} {expires}"),
    "code": ("🔐 Код Steam Guard", "{login} {code} {ttl}"),
    "code_ask_login": ("🔐 Какой аккаунт? (код)", "{logins}"),
    "code_not_found": ("🔐 Аккаунт не найден", ""),
    "code_cooldown": ("🔐 Слишком часто", "{seconds}"),
    "stock_ask_game": ("🔎 !наличие без игры", ""),
    "stock_unknown": ("🔎 Игра не найдена", "{game}"),
    "stock_free": ("🔎 Свободен", "{game} {count}"),
    "stock_busy": ("🔎 Занят (со временем)", "{game} {minutes}"),
    "stock_busy_unknown": ("🔎 Занят (без времени)", "{game}"),
    "below_min": ("⚠️ Меньше минимума", "{minimum} {ordered}"),
    "friend_activated": ("👥 Режим «для друга»", "{minutes}"),
    "friend_already": ("👥 Уже включён", ""),
    "ask_which_account": ("🔁 Какой аккаунт продлить?", "{game} {logins}"),
    "extend_applied": ("🔁 Продление применено", "{login} {hours} {expires}"),
    "extend_no_pending": ("🔁 Нечего продлевать", ""),
    "partial": ("⚠️ Выдано не всё", "{delivered} {ordered}"),
    "problem": ("⚠️ Сбой выдачи", ""),
    "rental_soon_end": ("⏳ Скоро конец аренды", "{login} {game} {minutes}"),
    "rental_ended": ("⌛️ Аренда закончилась", "{game} {login}"),
    "help": ("❓ Ответ на !помощь", ""),
    "my_rentals": ("📋 Список аренд (!мои)", "{items}"),
    "my_rentals_empty": ("📋 Аренд нет", ""),
    "smm_greeting": ("📣 SMM: приветствие", "{hint} {name}"),
    "smm_ask": ("📣 SMM: запрос ссылки", "{qty} {hint} {name}"),
    "smm_bad_link": ("📣 SMM: нет ссылки", "{hint}"),
    "smm_remind": ("📣 SMM: напоминание", "{hint}"),
    "smm_wait": ("📣 SMM: ждём баланс", ""),
    "smm_started": ("📣 SMM: запущен", "{smm_id} {link} {qty}"),
    "smm_done": ("📣 SMM: выполнен", ""),
    "smm_partial": ("📣 SMM: частично", "{remains}"),
    "smm_failed": ("📣 SMM: сбой", ""),
    "smm_refunded": ("📣 SMM: возврат", ""),
    "smm_limits": ("📣 SMM: вне лимитов", "{qty} {min} {max}"),
}
