"""Ликвидность, риск 0–100 и рейтинг 0–100 (раздел 12)."""
from __future__ import annotations

from datetime import date

from .categories import Category
from .config import Settings
from .models import DefectAnalysis, MarketEstimate, Strategy

RATING_BANDS = [(85, "Приоритетная проверка"), (70, "Перспективная сделка"),
                (55, "Наблюдать или торговаться"), (0, "Обычно пропустить")]


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def liquidity(cat: Category, market: MarketEstimate, defects: DefectAnalysis,
              today: date | None = None) -> int:
    """Ликвидность 1–10: базовая по категории с поправками на спрос и состояние."""
    month = (today or date.today()).month
    liq = cat.liquidity
    if market.comps_count >= 10:
        liq += 1
    elif market.comps_count < 3:
        liq -= 1
    if month in cat.low_season:
        liq -= 2
    if defects.defect_class == "C" or defects.is_parts_listing:
        liq -= 1
    return int(clamp(liq, 1, 10))


def days_to_sell(cat: Category, liq: int) -> int:
    return max(1, round(cat.days_to_sell * (1 + 0.15 * (cat.liquidity - liq))))


def risk_score(defects: DefectAnalysis, market: MarketEstimate, best: Strategy | None,
               settings: Settings, suspicious_price: bool, days: int) -> tuple[int, list[str]]:
    risks: list[str] = []
    risk = {None: 8, "A": 20, "B": 35, "C": 55}[defects.defect_class]
    if defects.defect_class:
        risks.append(f"Неисправность класса {defects.defect_class}, диагноз — гипотеза")
    if defects.is_untested:
        risk += 15
        risks.append("Продавец не проверял товар")
    if defects.is_parts_listing:
        risk += 10
    for flag in defects.red_flags:
        risk += flag.risk
        risks.append(("КРИТИЧНО: " if flag.critical else "") + flag.label)
    if suspicious_price:
        risk += 25
        risks.append("Подозрительно низкая цена для исправного товара — возможное мошенничество")
    if market.confidence == "нет данных":
        risk += 15
        risks.append("Нет аналогов для рыночной оценки")
    elif market.confidence == "низкая":
        risk += 10
        risks.append("Мало аналогов — оценка ненадёжна")
    if market.match_level == "family":
        risk += 5
    if days > settings.max_days_to_sell:
        risk += 10
        risks.append(f"Ожидаемый срок продажи {days} дн. больше допустимого")
    if best and best.max_loss > settings.capital_total * 0.15:
        risk += 10
        risks.append(f"Максимальная потеря капитала {best.max_loss} ₽")
    if defects.critical_flags:
        risk = max(risk, 90)
    return int(clamp(risk, 0, 100)), risks


def rating(best: Strategy | None, market: MarketEstimate, defects: DefectAnalysis, liq: int,
           settings: Settings, photos_count: int | None, description: str,
           risk: int = 0, suspicious_price: bool = False) -> tuple[int, dict[str, float]]:
    comp: dict[str, float] = {}
    if best and best.scenarios:
        cons = best.scenarios["conservative"]
        base = best.scenarios["base"]
        comp["прибыль"] = 25 * clamp(cons.profit_time_adjusted / (3 * settings.min_net_profit), 0, 1)
        margin = cons.profit / cons.costs.total if cons.costs.total else 0
        safety = 15 * clamp(margin / 0.4, 0, 1)
        if min(best.stress.values(), default=0) < 0:
            safety *= 0.5
        comp["запас прочности"] = safety
        # быстрый оборот ценен только для прибыльной сделки
        comp["оборот капитала"] = 10 * clamp((30 - base.days_held) / 27, 0, 1) if cons.profit > 0 else 0.0
    else:
        comp["прибыль"] = comp["запас прочности"] = comp["оборот капитала"] = 0.0

    market_pts = {"высокая": 20, "средняя": 14, "низкая": 7, "нет данных": 0}[market.confidence]
    if market.match_level == "family":
        market_pts = max(0, market_pts - 3)
    if best and best.code == "as_is":
        # цена «как есть» опирается на неисправные аналоги, а не на исправные
        market_pts = min(market_pts, 14 if market.broken_count >= 6 else 10 if market.broken_count >= 3 else 5)
        liq = max(1, liq - 2)
    comp["надёжность оценки"] = market_pts
    comp["ликвидность"] = 2 * liq

    verify = {None: 10, "A": 7, "B": 5, "C": 2}[defects.defect_class]
    if defects.is_untested:
        verify = min(verify, 3)
    if defects.is_parts_listing:
        verify = min(verify, 2)
    if photos_count == 0:
        verify -= 2
    if len(description.strip()) < 40:
        verify -= 2
    comp["проверяемость"] = clamp(verify, 0, 10)

    total = round(sum(comp.values()))
    # Сомнительное объявление не должно попадать наверх только из-за большого дисконта.
    caps = [(defects.critical_flags, 20), (market.confidence == "нет данных", 40),
            (suspicious_price, 50), (risk >= 60, 69)]
    for condition, cap in caps:
        if condition and total > cap:
            total = cap
            comp["ограничение"] = cap
    return int(clamp(total, 0, 100)), {k: round(v, 1) for k, v in comp.items()}


def band(score: int) -> str:
    return next(label for threshold, label in RATING_BANDS if score >= threshold)
