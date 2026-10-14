"""Оценка рыночной стоимости по аналогам (раздел 7).

Аналоги — актуальные предложения той же модели (из текущей кампании и
внешнего файла аналогов). Цена выставления ≠ цена сделки, поэтому к ценам
применяется коэффициент ask_to_sale_ratio, а консервативная оценка строится
по нижнему квартилю.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass

from .config import Settings
from .geo import normalize_city
from .models import MarketEstimate, now_iso

_CONF_STEPS = ["нет данных", "низкая", "средняя", "высокая"]


@dataclass
class Comp:
    model_key: str
    family_key: str
    price: int
    working: bool
    listing_id: str | None = None
    source: str = "campaign"
    city: str = ""


def _quantiles(prices: list[int]) -> tuple[float, float, float]:
    if len(prices) == 1:
        return prices[0], prices[0], prices[0]
    q1, med, q3 = statistics.quantiles(prices, n=4, method="inclusive")
    return q1, med, q3


def trim_outliers(prices: list[int]) -> list[int]:
    """Отсекает выбросы по IQR: одна дорогая публикация — не доказательство цены."""
    prices = sorted(prices)
    if len(prices) < 5:
        return prices
    q1, _, q3 = _quantiles(prices)
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return [p for p in prices if lo <= p <= hi]


def _downgrade(conf: str, steps: int = 1) -> str:
    idx = max(1, _CONF_STEPS.index(conf) - steps) if conf != "нет данных" else 0
    return _CONF_STEPS[idx]


class MarketIndex:
    def __init__(self, comps: list[Comp] | None = None):
        self.by_model: dict[str, list[Comp]] = defaultdict(list)
        self.by_family: dict[str, list[Comp]] = defaultdict(list)
        for c in comps or []:
            self.add(c)

    def add(self, comp: Comp) -> None:
        self.by_model[comp.model_key].append(comp)
        self.by_family[comp.family_key].append(comp)

    def _prices(self, comps: list[Comp], working: bool, exclude_id: str | None,
                city: str | None = None) -> list[int]:
        return [c.price for c in comps if c.working == working and c.listing_id != exclude_id
                and (city is None or normalize_city(c.city) == city)]

    def _working(self, model_key: str, family_key: str, exclude_id: str | None,
                 city: str | None) -> tuple[list[int], str]:
        working = self._prices(self.by_model.get(model_key, []), True, exclude_id, city)
        if len(working) < 2 and family_key != model_key:
            fam = self._prices(self.by_family.get(family_key, []), True, exclude_id, city)
            if len(fam) > len(working):
                return fam, "family"
        return working, "model"

    def estimate(self, model_key: str, family_key: str, settings: Settings,
                 exclude_id: str | None = None, resale_calibration: float = 1.0) -> MarketEstimate:
        """Оценка цены перепродажи. При заданном домашнем городе предпочтительны его аналоги:
        перепродаём там, а цены в других городах могут заметно отличаться."""
        est = MarketEstimate(model_key=model_key, estimated_at=now_iso())
        home = normalize_city(settings.region) or None
        working, level = self._working(model_key, family_key, exclude_id, None)
        other_cities = False
        if home:
            local, local_level = self._working(model_key, family_key, exclude_id, home)
            if len(trim_outliers(local)) >= 4:
                working, level = local, local_level
                est.basis = f"цены предложений в городе {settings.region} (не цены завершённых сделок)"
            elif len(working) > len(local):
                other_cities = True
                est.basis = (f"цены предложений, в т.ч. других городов — в городе {settings.region} "
                             f"аналогов: {len(local)} (не цены завершённых сделок)")
        broken = self._prices(self.by_model.get(model_key, []), False, exclude_id)
        if len(broken) < 2:
            broken = self._prices(self.by_family.get(family_key, []), False, exclude_id) or broken
        if broken:
            est.broken_count = len(broken)
            est.broken_median = int(statistics.median(trim_outliers(broken)))

        if "?" in model_key.split(":")[2:3]:
            working = []  # модель не определена — аналоги ненадёжны
        working = trim_outliers(working)
        if not working:
            return est

        q1, med, q3 = _quantiles(working)
        n = len(working)
        cv = statistics.pstdev(working) / med if med else 1.0
        conf = "высокая" if n >= 8 and cv <= 0.25 else "средняя" if n >= 4 else "низкая"
        if level == "family":
            conf = _downgrade(conf)
        if other_cities:
            conf = _downgrade(conf)

        ratio = settings.ask_to_sale_ratio * resale_calibration
        haircut = {"высокая": 0.0, "средняя": settings.low_confidence_haircut / 2,
                   "низкая": settings.low_confidence_haircut}[conf]
        est.match_level = level
        est.comps_count = n
        est.working_median = int(med)
        est.working_p25 = int(q1)
        est.working_p75 = int(q3)
        est.base = int(med * ratio)
        est.conservative = int(min(q1, med) * ratio * (1 - haircut))
        est.optimistic = int(max(med, q3 * ratio) * resale_calibration)
        est.quick = int(est.conservative * settings.quick_sale_factor)
        est.confidence = conf
        return est
