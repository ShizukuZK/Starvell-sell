"""Журнал продаж и сводная статистика."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from core.storage import DATA_DIR

SALES_FILE = os.path.join(DATA_DIR, "sales.json")
_LOCK = threading.RLock()
MAX_RECORDS = 20000


class SalesLog:
    def __init__(self, path: str = SALES_FILE) -> None:
        self.path = path
        self.items: List[Dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.items = data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            self.items = []

    def _save(self) -> None:
        with _LOCK:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.items[-MAX_RECORDS:], f, ensure_ascii=False)
            os.replace(tmp, self.path)

    def record(
        self, *, order_id: str, game: str, quantity: int, hours: int,
        revenue_rub: float, cost_rub: float, kind: str = "rent",
        buyer: str = "",
    ) -> None:
        """kind: rent | extend | smm | refund."""
        if any(i.get("order_id") == order_id and i.get("kind") == kind for i in self.items):
            return
        self.items.append({
            "ts": time.time(),
            "order_id": order_id,
            "game": game,
            "quantity": int(quantity),
            "hours": int(hours),
            "revenue": round(float(revenue_rub or 0), 2),
            "cost": round(float(cost_rub or 0), 2),
            "kind": kind,
            "buyer": buyer,
        })
        self._save()

    def summary(self, since: Optional[float] = None, commission_percent: float = 0.0) -> Dict[str, Any]:
        rows = [i for i in self.items if since is None or i.get("ts", 0) >= since]
        sales = [r for r in rows if r.get("kind") in ("rent", "extend", "smm")]
        refunds = [r for r in rows if r.get("kind") == "refund"]
        revenue = sum(r["revenue"] for r in sales)
        cost = sum(r["cost"] for r in sales)
        net = revenue * (1 - max(0.0, commission_percent) / 100.0)
        games: Counter = Counter()
        money: Dict[str, float] = defaultdict(float)
        for r in sales:
            games[r["game"]] += 1
            money[r["game"]] += r["revenue"]
        return {
            "orders": len(sales),
            "extends": sum(1 for r in sales if r.get("kind") == "extend"),
            "smm": sum(1 for r in sales if r.get("kind") == "smm"),
            "refunds": len(refunds),
            "hours": sum(r["hours"] for r in sales),
            "revenue": round(revenue, 2),
            "cost": round(cost, 2),
            "profit": round(net - cost, 2),
            "top": [(g, games[g], round(money[g], 2)) for g, _ in games.most_common(5)],
        }


def day_start(tz_offset_hours: int = 3) -> float:
    """Начало текущих суток в часовом поясе продавца (unix)."""
    now = time.time() + tz_offset_hours * 3600
    return now - (now % 86400) - tz_offset_hours * 3600
