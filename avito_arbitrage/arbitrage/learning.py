"""Учёт фактических результатов и калибровка прогнозов (раздел 21).

Прогноз ≠ факт: прибыль считается реализованной только для сделок со
статусом sold. Поправки применяются с «усадкой» к 1.0, чтобы 1–2 сделки
не перекашивали оценки.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date

from .economics import Calibration
from .storage import Storage

DEAL_STATUSES = ("bought", "repairing", "listed", "sold", "written_off")
NUMERIC_FIELDS = ("purchase_price", "costs_so_far", "repair_cost", "repair_hours", "sold_price")


def _days_between(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days


def _forecast_snapshot(storage: Storage, listing_id: str) -> dict:
    a = storage.latest_analysis(listing_id)
    if not a or not a.best:
        return {}
    base = a.best.scenarios.get("base")
    cons = a.best.scenarios.get("conservative")
    return {
        "analysis_at": a.analyzed_at,
        "strategy": a.best.code,
        "category": a.category,
        "diagnosis": "; ".join(f"{i.label}: {', '.join(i.causes)}" for i in a.defects.items) or "исправен",
        "resale_base": base.resale if base else None,
        "resale_conservative": cons.resale if cons else None,
        "repair_base": (base.costs.diagnostics + base.costs.parts + base.costs.labor) if base else None,
        "days_held_base": base.days_held if base else None,
        "profit_base": base.profit if base else None,
        "profit_conservative": cons.profit if cons else None,
        "rating": a.rating,
        "risk": a.risk,
    }


def update_deal(storage: Storage, listing_id: str, status: str, **fields) -> dict:
    if status not in DEAL_STATUSES:
        raise ValueError(f"Статус сделки должен быть одним из: {', '.join(DEAL_STATUSES)}")
    if storage.get_listing(listing_id) is None:
        raise KeyError(f"Объявление {listing_id} не найдено в базе")
    existing = storage.deal(listing_id)
    data = dict(existing["data"]) if existing else {}
    if not data.get("forecast"):
        data["forecast"] = _forecast_snapshot(storage, listing_id)
    for key, value in fields.items():
        if value is None:
            continue
        data[key] = float(value) if key == "repair_hours" else int(value) if key in NUMERIC_FIELDS else value
    if status == "sold":
        if data.get("sold_price") is None or data.get("purchase_price") is None:
            raise ValueError("Для статуса sold нужны purchase_price и sold_price")
        data.setdefault("sold_at", date.today().isoformat())
        data["net_profit"] = int(data["sold_price"]) - int(data["purchase_price"]) - int(data.get("costs_so_far") or 0)
        data["days_held"] = _days_between(data.get("bought_at"), data.get("sold_at"))
        data["errors"] = _forecast_errors(data)
    elif status == "written_off":
        data["net_profit"] = int(data.get("sold_price") or 0) - int(data.get("purchase_price") or 0) \
            - int(data.get("costs_so_far") or 0)
    storage.save_deal(listing_id, status, data)
    return data


def _forecast_errors(data: dict) -> dict:
    f = data.get("forecast") or {}
    errors: dict[str, float] = {}
    if f.get("resale_base"):
        errors["resale_ratio"] = round(data["sold_price"] / f["resale_base"], 3)
    if f.get("repair_base") and data.get("repair_cost") is not None:
        errors["repair_ratio"] = round(data["repair_cost"] / f["repair_base"], 3)
    if f.get("days_held_base") and data.get("days_held"):
        errors["days_ratio"] = round(data["days_held"] / f["days_held_base"], 3)
    if f.get("profit_base") is not None:
        errors["profit_error"] = data["net_profit"] - f["profit_base"]
    return errors


def _shrunk(values: list[float], weight: int = 5) -> float:
    if not values:
        return 1.0
    k = len(values) / (len(values) + weight)
    return round(max(0.6, min(1.4, 1 + k * (statistics.median(values) - 1))), 3)


def calibrations(storage: Storage) -> dict[str, Calibration]:
    by_cat: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for d in storage.deals():
        if d["status"] != "sold":
            continue
        cat = (d["data"].get("forecast") or {}).get("category")
        for key, value in (d["data"].get("errors") or {}).items():
            if cat and key.endswith("_ratio"):
                by_cat[cat][key].append(value)
    return {cat: Calibration(resale=_shrunk(v["resale_ratio"]), repair=_shrunk(v["repair_ratio"]),
                             days=_shrunk(v["days_ratio"]))
            for cat, v in by_cat.items()}


def category_performance(storage: Storage) -> list[dict]:
    """Фактическая эффективность категорий: прибыль на вложенный рубль и на день."""
    stats: dict[str, dict] = defaultdict(lambda: {"deals": 0, "profit": 0, "invested": 0, "days": []})
    for d in storage.deals():
        if d["status"] not in ("sold", "written_off"):
            continue
        data = d["data"]
        cat = (data.get("forecast") or {}).get("category", "other")
        s = stats[cat]
        s["deals"] += 1
        s["profit"] += int(data.get("net_profit") or 0)
        s["invested"] += int(data.get("purchase_price") or 0) + int(data.get("costs_so_far") or 0)
        if data.get("days_held"):
            s["days"].append(data["days_held"])
    rows = []
    for cat, s in stats.items():
        avg_days = statistics.mean(s["days"]) if s["days"] else None
        rows.append({
            "category": cat, "deals": s["deals"], "profit": s["profit"],
            "profit_per_rub": round(s["profit"] / s["invested"], 3) if s["invested"] else None,
            "avg_days": round(avg_days, 1) if avg_days else None,
            "profit_per_day": round(s["profit"] / sum(s["days"]), 1) if s["days"] else None,
        })
    return sorted(rows, key=lambda r: (r["profit_per_rub"] or 0), reverse=True)
