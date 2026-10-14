"""Экономика сделки: три стратегии восстановления, три сценария, предельная цена (разделы 9–11).

Ключевая идея: риск неудачного ремонта закладывается в резерв.
  резерв на неудачу = (1 − p) × (прибыль при успехе − прибыль при неудаче)
Тогда «цена перепродажи − полная себестоимость» равна ожидаемой прибыли,
а формулы в таблице остаются простыми и честными.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .categories import Category
from .config import Settings
from .models import Costs, DefectAnalysis, Logistics, MarketEstimate, Scenario, Strategy

SCENARIOS = ("conservative", "base", "optimistic")
STRATEGY_LABELS = {
    "working": "Покупка исправного / перепродажа",
    "diy": "A. Самостоятельный ремонт",
    "service": "B. Ремонт в сервисе",
    "as_is": "C. Перепродажа без ремонта / на запчасти",
}
OVERRUN_BY_CLASS = {None: 0.0, "A": 0.2, "B": 0.3, "C": 0.5}


def floor100(value: float) -> int:
    return int(math.floor(value / 100.0) * 100) if value > 0 else 0


@dataclass
class Calibration:
    """Поправки по фактическим сделкам (learning.py); 1.0 — без поправок."""
    resale: float = 1.0
    repair: float = 1.0
    days: float = 1.0


@dataclass
class RepairPlan:
    code: str
    feasible: bool
    reason: str = ""
    diag: tuple[int, int] = (0, 0)
    parts: tuple[int, int] = (0, 0)
    tools: int = 0
    labor: tuple[int, int] = (0, 0)
    hours: float = 0.0
    p_success: float = 1.0
    repair_days: int = 0
    logistics: int = 0
    fail_spend: str = "none"  # что теряется при неудаче: parts / diag / none


@dataclass
class DealContext:
    asking: int
    category: Category
    market: MarketEstimate
    defects: DefectAnalysis
    settings: Settings
    days_to_sell: int
    calibration: Calibration
    capital_in_use: int = 0
    logistics: Logistics = field(default_factory=lambda: Logistics("local"))

    @property
    def acquisition_transport(self) -> int:
        """Сколько стоит забрать товар: по городу, поездкой в соседний город или доставкой."""
        lg = self.logistics
        local = self.settings.transport_cost.get(self.category.size, 0)
        if lg.mode in ("pickup", "delivery"):
            return lg.extra_cost  # для самовывоза уже включает погрузку крупногабарита
        return local


def _sum_ranges(ranges: list[tuple[int, int]]) -> tuple[int, int]:
    return sum(r[0] for r in ranges), sum(r[1] for r in ranges)


def build_plans(ctx: DealContext) -> list[RepairPlan]:
    s = ctx.settings
    d = ctx.defects
    repairs = [i for i in d.items if not i.accessory]
    accessories = [i for i in d.items if i.accessory]
    acc_parts = _sum_ranges([i.diy_parts for i in accessories])
    allowed = set(s.allowed_strategies)
    plans: list[RepairPlan] = []

    if not repairs:
        p = s.untested_success if d.is_untested else 1.0
        plans.append(RepairPlan("working", "working" in allowed,
                                "" if "working" in allowed else "стратегия отключена в настройках",
                                parts=acc_parts, p_success=p,
                                fail_spend="parts" if accessories else "none"))
        if d.is_untested or d.is_parts_listing:
            plans.append(RepairPlan("as_is", "as_is" in allowed, p_success=1.0))
        return plans

    diy_ok = all(i.diy_allowed for i in repairs)
    reason = ""
    if "diy" not in allowed:
        reason = "стратегия отключена в настройках"
    elif not diy_ok:
        reason = "; ".join(sorted({i.diy_note or f"{i.label}: только сервис" for i in repairs if not i.diy_allowed}))
    diy_parts = _sum_ranges([i.diy_parts for i in repairs] + [acc_parts])
    hours = sum(i.diy_hours for i in repairs)
    plans.append(RepairPlan(
        "diy", diy_ok and "diy" in allowed, reason,
        parts=diy_parts, tools=max((i.diy_tools for i in repairs), default=0), hours=hours,
        p_success=math.prod(i.diy_success for i in repairs),
        repair_days=(5 if diy_parts[1] > 0 else 0) + math.ceil(hours / 3), fail_spend="parts"))

    plans.append(RepairPlan(
        "service", "service" in allowed, "" if "service" in allowed else "стратегия отключена в настройках",
        diag=(max(i.service_diag[0] for i in repairs), max(i.service_diag[1] for i in repairs)),
        parts=_sum_ranges([i.service_parts for i in repairs] + [acc_parts]),
        labor=_sum_ranges([i.service_labor for i in repairs]),
        p_success=math.prod(i.service_success for i in repairs),
        repair_days=max(i.service_days for i in repairs),
        logistics=s.service_logistics_cost.get(ctx.category.size, 0), fail_spend="diag"))

    plans.append(RepairPlan("as_is", "as_is" in allowed,
                            "" if "as_is" in allowed else "стратегия отключена в настройках"))
    return plans


def as_is_value(ctx: DealContext) -> int | None:
    """Цена продажи «как есть».

    Покупатель неисправного товара сам рассчитывает на прибыль, поэтому цена
    ограничена сверху: базовая цена исправного − ремонт в сервисе − 20% его маржи,
    а снизу — стоимостью на запчасти. Неисправные аналоги (≥3) уточняют оценку.
    """
    m = ctx.market
    if not m.working_median:
        return None
    ratio = ctx.settings.ask_to_sale_ratio * ctx.calibration.resale
    repairs = [i for i in ctx.defects.items if not i.accessory]
    floor = m.working_median * ratio * ctx.category.parts_ratio
    base = m.base or m.working_median * ratio
    repair_mid = ctx.calibration.repair * sum(
        (sum(i.service_diag) + sum(i.service_parts) + sum(i.service_labor)) / 2 for i in repairs)
    if repairs and all(i.as_is_factor for i in repairs):
        # пригоден к использованию, но с дефектом (износ АКБ, нет Face ID): покупатель
        # вычтет как минимум стоимость устранения
        usable = m.working_median * ratio * min(i.as_is_factor for i in repairs)  # type: ignore[type-var]
        return int(max(floor, min(usable, base - repair_mid)))
    if ctx.defects.is_untested and not repairs:
        estimate = m.working_median * ratio * 0.5
    else:
        estimate = m.broken_median * ratio if m.broken_count >= 3 and m.broken_median else floor
    cap = base - repair_mid - 0.2 * base
    return int(min(max(estimate, floor), max(floor, cap)))


def parts_value(ctx: DealContext) -> int:
    if not ctx.market.working_median:
        return 0
    return int(ctx.market.working_median * ctx.settings.ask_to_sale_ratio * ctx.category.parts_ratio)


def _pick(rng: tuple[int, int], scenario: str) -> float:
    # Консервативно — 75-й перцентиль диапазона: сверх него работает отдельный резерв на перерасход.
    lo, hi = rng
    return lo + (hi - lo) * {"conservative": 0.75, "base": 0.5, "optimistic": 0.0}[scenario]


def _selling(resale: int, s: Settings) -> int:
    return round(resale * s.sale_fee_rate + s.sale_fixed_cost) if resale > 0 else 0


def evaluate(plan: RepairPlan, scenario: str, ctx: DealContext, purchase: int) -> Scenario | None:
    s, cat, m = ctx.settings, ctx.category, ctx.market
    if plan.code == "as_is":
        value = as_is_value(ctx)
        if value is None:
            return None
        resale = int(value * {"conservative": 0.85, "base": 1.0, "optimistic": 1.1}[scenario])
    else:
        market_resale = {"conservative": m.conservative, "base": m.base, "optimistic": m.optimistic}[scenario]
        if market_resale is None:
            return None
        resale = int(market_resale * ctx.defects.resale_factor)

    k = ctx.calibration.repair
    diag = round(_pick(plan.diag, scenario) * k)
    parts = round(_pick(plan.parts, scenario) * k) + plan.tools
    labor = round(_pick(plan.labor, scenario) * k)

    p = plan.p_success
    if p < 1.0:
        p = {"conservative": p * 0.85, "base": p, "optimistic": min(1.0, p + 0.05)}[scenario]

    sell_days = ctx.days_to_sell * (1.5 if plan.code == "as_is" else 1.0)
    days_held = math.ceil(plan.repair_days + ctx.logistics.extra_days + sell_days * ctx.calibration.days
                          * {"conservative": 2.0, "base": 1.0, "optimistic": 0.75}[scenario])
    transport = ctx.acquisition_transport + ctx.defects.extra_transport + plan.logistics
    other = s.packaging_cost.get(cat.size, 0) + s.storage_cost_per_day * days_held
    selling = _selling(resale, s)
    overrun = round((diag + parts + labor) * OVERRUN_BY_CLASS[ctx.defects.defect_class]) \
        if scenario == "conservative" else 0
    returns = round(resale * s.returns_reserve_rate)

    profit_success = resale - (purchase + diag + parts + labor + transport + other + selling + overrun + returns)
    # при неудаче товар остаётся в исходном состоянии; «не проверялся» оказывается неисправным
    fallback = parts_value(ctx) if plan.code == "working" else (as_is_value(ctx) or 0)
    salvage = int(fallback * (0.8 if scenario == "conservative" else 1.0))
    spent_on_fail = {"parts": parts, "diag": diag, "none": 0}[plan.fail_spend]
    profit_failure = salvage - (purchase + spent_on_fail + transport + other + _selling(salvage, s))
    failure_reserve = round((1 - p) * max(0, profit_success - profit_failure))

    costs = Costs(purchase=purchase, diagnostics=diag, parts=parts, labor=labor, transport=transport,
                  other=other, reserve=overrun + returns + failure_reserve, selling=selling)
    hours = plan.hours * (1.5 if scenario == "conservative" else 1.0)
    return Scenario(name=scenario, resale=resale, costs=costs, p_success=round(p, 3),
                    profit=resale - costs.total, profit_if_success=profit_success,
                    profit_if_failure=profit_failure, days_held=days_held,
                    time_cost=round(hours * s.hourly_rate), salvage=salvage)


def max_purchase_price(scn: Scenario, ctx: DealContext) -> int:
    """Консервативная цена − все прочие расходы − требуемая прибыль; с учётом ROI и лимитов."""
    s = ctx.settings
    other_costs = scn.costs.without_purchase + scn.time_cost
    by_profit = scn.resale - other_costs - s.min_net_profit
    by_roi = scn.resale / (1 + s.min_roi) - other_costs
    available = s.capital_available - ctx.capital_in_use
    return floor100(min(by_profit, by_roi, s.max_purchase_price, available))


def stress_tests(scn: Scenario, ctx: DealContext) -> dict[str, int]:
    """Раздел 11: что будет, если всё пойдёт хуже базового сценария."""
    c = scn.costs
    m = ctx.market.working_median or scn.resale
    extra_defect = round(m * (0.15 if ctx.defects.has_defects else 0.10))
    tests = {
        "ремонт дороже на 50%": scn.profit - round((c.diagnostics + c.parts + c.labor) * 0.5),
        "дополнительная неисправность": scn.profit - extra_defect,
        "продажа на 15% дешевле": scn.profit - round(scn.resale * 0.15 * (1 - ctx.settings.sale_fee_rate)),
        "продажа вдвое дольше": scn.profit - ctx.settings.storage_cost_per_day * ctx.days_to_sell,
    }
    if scn.p_success < 1.0:
        tests["ремонт не удался (продажа на запчасти)"] = scn.profit_if_failure
    return tests


def build_strategies(ctx: DealContext) -> list[Strategy]:
    decision_scenario = "base" if ctx.settings.risk_appetite == "moderate" else "conservative"
    strategies = []
    for plan in build_plans(ctx):
        if ctx.logistics.mode == "unavailable":
            plan.feasible, plan.reason = False, ctx.logistics.note
        strat = Strategy(code=plan.code, label=STRATEGY_LABELS[plan.code], feasible=plan.feasible,
                         reason=plan.reason, hours=plan.hours)
        for name in SCENARIOS:
            scn = evaluate(plan, name, ctx, ctx.asking)
            if scn:
                strat.scenarios[name] = scn
        if not strat.scenarios:
            strat.feasible = False
            strat.reason = strat.reason or "нет рыночной оценки"
        else:
            decision = strat.scenarios.get(decision_scenario) or strat.scenarios["conservative"]
            strat.max_purchase = max_purchase_price(decision, ctx)
            cons = strat.scenarios["conservative"]
            strat.stress = stress_tests(strat.scenarios["base"], ctx)
            downside = min(cons.profit_if_success, *strat.stress.values())
            if plan.p_success < 1.0:
                downside = min(downside, cons.profit_if_failure)
            strat.max_loss = max(0, -downside)
        strategies.append(strat)
    return strategies


def choose_best(strategies: list[Strategy]) -> Strategy | None:
    feasible = [s for s in strategies if s.feasible and s.scenarios]
    if not feasible:
        return None
    return max(feasible, key=lambda s: (s.scenarios["conservative"].profit_time_adjusted,
                                        s.scenarios["base"].profit_time_adjusted))


def negotiation_prices(asking: int, max_purchase: int | None, defective: bool,
                       motivated: bool = False, no_bargain: bool = False) -> tuple[int | None, int | None, int | None]:
    """Стартовая, целевая и максимальная цена (раздел 17). Максимум продавцу не раскрывается."""
    if not max_purchase or max_purchase <= 0:
        return None, None, None
    max_deal = asking if asking <= max_purchase else max_purchase
    discount = 0.05 if no_bargain else 0.10 if motivated else 0.07
    if asking <= max_purchase:
        target = floor100(min(asking * (1 - discount), max_deal))
    else:
        target = floor100(max_deal * 0.92)
    floor_ratio = (0.6 if defective else 0.72) - (0.05 if motivated else 0.0)
    start = floor100(max(asking * floor_ratio, target * 0.88))
    start = min(start, target)
    return start, target, max_deal
