import dataclasses

import pytest

from arbitrage.analyzer import Analyzer
from arbitrage.economics import Calibration, DealContext, build_strategies, negotiation_prices
from arbitrage.categories import get_category
from arbitrage.defects import analyze_defects
from arbitrage.market import Comp, MarketIndex, trim_outliers
from arbitrage.models import Analysis
from conftest import make_listing, market_of

PRICES = [16000, 17000, 15500, 16500, 18000, 16000, 15000, 17500, 16200]


def analyze(settings, target, comps=PRICES, title="iPhone 12 128GB"):
    listings = market_of(title, comps) + [target]
    results = Analyzer(settings).analyze_all(listings)
    return next(a for a in results if a.listing_id == target.listing_id)


def test_profit_equals_resale_minus_full_cost(settings):
    a = analyze(settings, make_listing("iPhone 12 128GB", 9000, "Аккумулятор 75%, остальное работает"))
    for strat in a.strategies:
        for scn in strat.scenarios.values():
            assert scn.profit == scn.resale - scn.costs.total
            # ожидаемая прибыль лежит между исходами успеха и неудачи
            lo, hi = sorted((scn.profit_if_success, scn.profit_if_failure))
            assert lo - 1 <= scn.profit <= hi + 1


def test_scenarios_are_ordered(settings):
    a = analyze(settings, make_listing("iPhone 12 128GB", 9000, "Разбит экран, остальное работает"))
    for strat in a.strategies:
        s = strat.scenarios
        assert s["conservative"].profit <= s["base"].profit <= s["optimistic"].profit


@pytest.mark.parametrize("desc", ["Отличное состояние", "Аккумулятор 75%", "Не заряжается"])
def test_max_purchase_price_meets_requirements(settings, desc):
    a = analyze(settings, make_listing("iPhone 12 128GB", 9000, desc))
    for strat in a.strategies:
        if not strat.feasible or not strat.max_purchase:
            continue
        cons = strat.scenarios["conservative"]
        at_max = cons.profit + (a.asking_price - strat.max_purchase)  # покупка влияет 1:1
        total_at_max = cons.costs.total - a.asking_price + strat.max_purchase
        assert at_max - cons.time_cost >= settings.min_net_profit - 1
        assert at_max / total_at_max >= settings.min_roi - 1e-6
        assert strat.max_purchase <= settings.max_purchase_price


def test_working_item_below_market_is_profitable(settings):
    a = analyze(settings, make_listing("iPhone 12 128GB", 9500, "Всё работает, состояние отличное"))
    assert a.best_strategy == "working"
    assert a.best.scenarios["conservative"].profit >= settings.min_net_profit
    assert a.recommendation.startswith(("ПРИОРИТЕТ", "ПЕРСПЕКТИВНО", "НАБЛЮДАТЬ"))
    assert a.start_price <= a.target_price <= a.max_deal_price <= a.asking_price


def test_overpriced_item_is_skipped(settings):
    a = analyze(settings, make_listing("iPhone 12 128GB", 17500, "Всё работает"))
    assert a.recommendation.startswith("ПРОПУСТИТЬ")


def test_critical_flag_overrides_rating(settings):
    a = analyze(settings, make_listing("iPhone 12 128GB", 3000, "Заблокирован iCloud, на запчасти"))
    assert a.critical and a.rating <= 20 and a.risk >= 90
    assert a.recommendation.startswith("НЕ ПОКУПАТЬ")


def test_suspicious_price_capped(settings):
    a = analyze(settings, make_listing("iPhone 12 128GB", 4000, "Как новый, всё работает"))
    assert a.rating <= 50
    assert a.recommendation.startswith("ПРОВЕРИТЬ")


def test_no_comps_means_insufficient_data(settings):
    lone = make_listing("Синтезатор Yamaha PSR-E373", 9000, "Рабочий")
    a = Analyzer(settings).analyze_all([lone])[0]
    assert a.insufficient_data and a.rating <= 40
    assert a.recommendation.startswith("НЕДОСТАТОЧНО ДАННЫХ")


def test_settings_change_economics(settings):
    target = make_listing("iPhone 12 128GB", 9500, "Всё работает")
    strict = dataclasses.replace(settings, min_net_profit=5000)
    assert analyze(strict, target).best.max_purchase < analyze(settings, target).best.max_purchase


def test_disallowed_strategy_is_not_chosen(settings):
    only_service = dataclasses.replace(settings, allowed_strategies=["working", "service"])
    a = analyze(only_service, make_listing("iPhone 12 128GB", 6000, "Разбит экран"))
    assert a.best_strategy in (None, "service")
    assert not next(s for s in a.strategies if s.code == "diy").feasible


def test_negotiation_prices():
    start, target, max_deal = negotiation_prices(10000, 8000, defective=False)
    assert start <= target <= max_deal == 8000
    start, target, max_deal = negotiation_prices(7000, 9000, defective=False)
    assert max_deal == 7000 and target < 7000 and start <= target
    assert negotiation_prices(7000, 0, defective=True) == (None, None, None)


def test_trim_outliers():
    assert 90000 not in trim_outliers([10000, 10500, 11000, 9800, 10200, 90000])


def test_market_family_fallback_and_self_exclusion(settings):
    idx = MarketIndex([Comp("smartphone:apple:iphone 12:64gb", "smartphone:apple:iphone 12:", p, True, f"x{p}")
                       for p in (15000, 16000, 17000)])
    est = idx.estimate("smartphone:apple:iphone 12:128gb", "smartphone:apple:iphone 12:", settings)
    assert est.match_level == "family" and est.comps_count == 3
    est = idx.estimate("smartphone:apple:iphone 12:64gb", "smartphone:apple:iphone 12:", settings, exclude_id="x15000")
    assert est.comps_count == 2


def test_capital_in_use_limits_purchase(settings):
    target = make_listing("iPhone 12 128GB", 9500, "Всё работает")
    listings = market_of("iPhone 12 128GB", PRICES) + [target]
    busy = Analyzer(settings, capital_in_use=25_000).analyze_all(listings)
    a = next(x for x in busy if x.listing_id == target.listing_id)
    assert a.best.max_purchase <= 5_000


def test_calibration_lowers_resale(settings):
    target = make_listing("iPhone 12 128GB", 9500, "Всё работает")
    listings = market_of("iPhone 12 128GB", PRICES) + [target]
    calibrated = Analyzer(settings, calibrations={"smartphone": Calibration(resale=0.8)}).analyze_all(listings)
    plain = Analyzer(settings).analyze_all(listings)
    pick = lambda res: next(x for x in res if x.listing_id == target.listing_id)  # noqa: E731
    assert pick(calibrated).market.base < pick(plain).market.base


def test_analysis_roundtrip(settings):
    a = analyze(settings, make_listing("iPhone 12 128GB", 9000, "Разбит экран"))
    restored = Analysis.from_dict(a.to_dict())
    assert restored == a
    assert restored.best.scenarios["base"].costs.total == a.best.scenarios["base"].costs.total


def test_untested_failure_falls_back_to_parts(settings):
    a = analyze(settings, make_listing("iPhone 12 128GB", 6000, "Не проверял, достал из шкафа"))
    working = next(s for s in a.strategies if s.code == "working")
    assert working.scenarios["base"].p_success == settings.untested_success
    assert "ремонт не удался (продажа на запчасти)" in working.stress


def test_deal_context_direct(settings):
    cat = get_category("laptop")
    market = MarketIndex([Comp("k", "f", p, True, str(p)) for p in (15000, 16000, 17000, 16500)]) \
        .estimate("k", "f", settings)
    defects = analyze_defects("Ноутбук", "Не видит диск", "laptop", market_price=market.working_median)
    ctx = DealContext(8000, cat, market, defects, settings, 6, Calibration())
    codes = {s.code: s for s in build_strategies(ctx)}
    assert codes["diy"].feasible and codes["service"].feasible
    assert codes["diy"].scenarios["base"].time_cost > 0
