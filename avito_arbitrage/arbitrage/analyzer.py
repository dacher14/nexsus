"""Полный анализ объявлений: модель → аналоги → дефекты → экономика → риск → рейтинг."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .categories import CATEGORIES, get_category, infer_category
from .config import Settings
from .defects import RepairKnowledgeBase, analyze_defects, load_kb, repair_complexity
from .geo import logistics_for
from .economics import (Calibration, DealContext, build_strategies, choose_best,
                        negotiation_prices)
from .identify import ModelId, identify_model
from .llm import LLMAnalyzer
from .market import Comp, MarketIndex
from .models import Analysis, DefectAnalysis, Listing, now_iso
from .scoring import band, days_to_sell, liquidity, rating, risk_score


@dataclass
class Prepared:
    listing: Listing
    category: str
    model: ModelId
    model_key: str
    family_key: str
    defects: DefectAnalysis
    llm_symptoms: list[tuple[str, str]]
    llm_flags: list[tuple[str, str]]

    @property
    def is_working_comp(self) -> bool:
        d = self.defects
        return not (d.has_defects or d.is_untested or d.is_parts_listing or d.critical_flags)


def fmt_rub(value: int | float | None) -> str:
    return "—" if value is None else f"{int(value):,} ₽".replace(",", " ")


class Analyzer:
    def __init__(self, settings: Settings, kb: RepairKnowledgeBase | None = None,
                 calibrations: dict[str, Calibration] | None = None, capital_in_use: int = 0,
                 llm: LLMAnalyzer | None = None, today: date | None = None):
        self.settings = settings
        self.kb = kb or load_kb()
        self.calibrations = calibrations or {}
        self.capital_in_use = capital_in_use
        self.llm = llm
        self.today = today

    # --- шаг 1: модель и дефекты без рыночной цены (для отбора аналогов) ---
    def prepare(self, listing: Listing) -> Prepared:
        category = infer_category(listing.title, listing.description, listing.category)
        mid = identify_model(listing.title, listing.description, category, listing.brand, listing.model)
        defects = self._defects(listing, category, None, [], [])
        llm_symptoms: list[tuple[str, str]] = []
        llm_flags: list[tuple[str, str]] = []
        needs_llm = mid.confidence == "низкая" or any(i.symptom == "unknown_fault" for i in defects.items)
        if self.llm and self.settings.llm_enabled and needs_llm:
            found = self.llm.analyze(listing.title, listing.description, listing.price)
            if found:
                if found.model_confident and found.model:
                    if found.category in CATEGORIES:
                        category = found.category
                    mid = ModelId(found.brand.lower(), found.model.lower(), found.variant.lower(), "средняя (LLM)")
                llm_symptoms, llm_flags = found.symptoms, found.red_flags
                defects = self._defects(listing, category, None, llm_symptoms, llm_flags)
                defects.llm_used = True
        return Prepared(listing, category, mid, mid.key(category), mid.family_key(category),
                        defects, llm_symptoms, llm_flags)

    def _defects(self, listing: Listing, category: str, market_price: int | None,
                 extra_symptoms: list, extra_flags: list) -> DefectAnalysis:
        return analyze_defects(listing.title, listing.description, category, listing.condition,
                               market_price=market_price, diy_skills=self.settings.diy_skills,
                               kb=self.kb, extra_symptoms=extra_symptoms, extra_flags=extra_flags)

    def build_index(self, prepared: Iterable[Prepared], external: Iterable[Comp] = ()) -> MarketIndex:
        index = MarketIndex(list(external))
        for p in prepared:
            if p.defects.critical_flags:
                continue
            index.add(Comp(p.model_key, p.family_key, p.listing.price, p.is_working_comp,
                           p.listing.listing_id, city=p.listing.city))
        return index

    # --- шаг 2: полный анализ ---
    def analyze(self, p: Prepared, index: MarketIndex) -> Analysis:
        s = self.settings
        listing = p.listing
        cat = get_category(p.category)
        calib = self.calibrations.get(p.category, Calibration())
        market = index.estimate(p.model_key, p.family_key, s, exclude_id=listing.listing_id,
                                resale_calibration=calib.resale)
        defects = self._defects(listing, p.category, market.working_median, p.llm_symptoms, p.llm_flags)
        defects.llm_used = p.defects.llm_used
        liq = liquidity(cat, market, defects, self.today)
        days = days_to_sell(cat, liq)
        logistics = logistics_for(listing.city, cat.size, s)

        ctx = DealContext(listing.price, cat, market, defects, s, days, calib, self.capital_in_use, logistics)
        strategies = build_strategies(ctx)
        best = choose_best(strategies)

        suspicious = bool(market.working_median and not defects.has_defects and not defects.is_untested
                          and not defects.is_parts_listing
                          and listing.price < s.suspicious_price_ratio * market.working_median)
        held_days = best.scenarios["base"].days_held if best else days
        risk, risks = risk_score(defects, market, best, s, suspicious, held_days, logistics)
        score, components = rating(best, market, defects, liq, s, listing.photos_count, listing.description,
                                   risk=risk, suspicious_price=suspicious, logistics=logistics)

        a = Analysis(
            listing_id=listing.listing_id, analyzed_at=now_iso(), category=p.category,
            category_name=cat.name, brand=p.model.brand, model=p.model.model, model_key=p.model_key,
            model_confidence=p.model.confidence, asking_price=listing.price, market=market,
            defects=defects, logistics=logistics, strategies=strategies, best_strategy=best.code if best else None,
            liquidity=liq, days_to_sell=days, risk=risk, repair_complexity=repair_complexity(defects),
            rating=score, rating_components=components, critical=bool(defects.critical_flags),
            insufficient_data=market.confidence == "нет данных", risks=risks,
        )
        if market.working_median:
            a.discount = round((market.working_median - listing.price) / market.working_median, 3)
        if not s.category_allowed(p.category):
            a.notes.append("Категория исключена настройками пользователя")
        if best:
            a.start_price, a.target_price, a.max_deal_price = negotiation_prices(
                listing.price, best.max_purchase, defects.has_defects or defects.is_untested,
                motivated="motivated_seller" in defects.signals, no_bargain="no_bargain" in defects.signals)
        a.checks = defects.checks + _logistics_checks(logistics, defects.has_defects)
        self._recommend(a, best, suspicious)
        self._explain(a, best)
        return a

    def analyze_all(self, listings: Iterable[Listing], external: Iterable[Comp] = ()) -> list[Analysis]:
        prepared = [self.prepare(l) for l in listings]
        index = self.build_index(prepared, external)
        return [self.analyze(p, index) for p in prepared]

    # --- рекомендация (раздел 24: безопасность и достоверность важнее прибыли) ---
    def _recommend(self, a: Analysis, best, suspicious: bool) -> None:
        s = self.settings
        if a.critical:
            labels = "; ".join(f.label for f in a.defects.critical_flags)
            a.recommendation, a.action = f"НЕ ПОКУПАТЬ: {labels}", "Исключить"
            return
        if not s.category_allowed(a.category):
            a.recommendation, a.action = "ПРОПУСТИТЬ: категория исключена настройками", "Исключить"
            return
        if a.logistics.mode == "unavailable":
            a.recommendation, a.action = f"ПРОПУСТИТЬ: {a.logistics.note}", "Пропустить"
            return
        if a.insufficient_data:
            a.recommendation = "НЕДОСТАТОЧНО ДАННЫХ: нет аналогов для оценки — уточнить модель, найти аналоги"
            a.action = "Дособрать данные"
            return
        if best is None:
            a.recommendation, a.action = "ПРОПУСТИТЬ: нет выполнимой стратегии", "Пропустить"
            return
        loss_limit = s.capital_total * s.max_loss_share_of_capital
        fail_prob = 1 - best.scenarios["conservative"].p_success
        if best.max_loss > loss_limit and fail_prob >= 0.2:
            a.recommendation = (f"ПРОПУСТИТЬ: возможный убыток {fmt_rub(best.max_loss)} несоразмерен "
                                f"капиталу (лимит {fmt_rub(loss_limit)})")
            a.action = "Пропустить"
            return
        cons, opt = best.scenarios["conservative"], best.scenarios["optimistic"]
        max_buy = best.max_purchase or 0
        defective = a.defects.has_defects or a.defects.is_untested
        if suspicious:
            a.recommendation = "ПРОВЕРИТЬ: цена подозрительно низкая — высокий риск мошенничества"
            a.action = "Проверить продавца; только личная встреча"
        elif a.asking_price <= max_buy:
            if a.days_to_sell > s.max_days_to_sell:
                a.recommendation, a.action = "НАБЛЮДАТЬ: прибыльно, но долгий срок продажи", "Наблюдать"
            elif a.rating >= 85:
                a.recommendation, a.action = "ПРИОРИТЕТ: проверить товар и договориться об осмотре", "Проверить"
            elif a.rating >= 70:
                a.recommendation, a.action = "ПЕРСПЕКТИВНО: проверить и попробовать поторговаться", "Проверить и торговаться"
            else:
                a.recommendation, a.action = "НАБЛЮДАТЬ: экономика проходит, но рейтинг невысокий", "Наблюдать или торговаться"
        elif max_buy > 0 and max_buy >= a.asking_price * (0.6 if defective else 0.7):
            a.recommendation = f"ТОРГОВАТЬСЯ: выгодно при цене до {fmt_rub(max_buy)}"
            a.action = "Торговаться"
        elif opt.profit >= s.min_net_profit and cons.profit < s.min_net_profit:
            a.recommendation = "ПРОПУСТИТЬ: прибыль только в оптимистичном сценарии"
            a.action = "Пропустить"
        else:
            a.recommendation = "ПРОПУСТИТЬ: недостаточная прибыль при текущей цене"
            a.action = "Пропустить"
        if a.asking_price > s.max_purchase_price:
            a.notes.append(f"Цена выше лимита одной покупки ({fmt_rub(s.max_purchase_price)})")

    def _explain(self, a: Analysis, best) -> None:
        m = a.market
        parts = []
        if a.discount is not None and m.working_median:
            parts.append(f"дисконт {round(a.discount * 100)}% к медиане {fmt_rub(m.working_median)} "
                         f"({m.comps_count} анал.)")
        if best and best.scenarios:
            cons = best.scenarios["conservative"]
            parts.append(f"{best.label}: конс. прибыль {fmt_rub(cons.profit)}")
            base, opt = best.scenarios["base"], best.scenarios["optimistic"]
            a.notes.append(f"Сценарии ({best.label}): конс. {fmt_rub(cons.profit)} / "
                           f"база {fmt_rub(base.profit)} / опт. {fmt_rub(opt.profit)}")
            if best.code == "diy":
                a.notes.append(f"DIY с учётом времени ({best.hours:g} ч × {self.settings.hourly_rate} ₽): "
                               f"база {fmt_rub(base.profit_time_adjusted)}, "
                               f"{fmt_rub(base.profit / max(best.hours, 0.5))}/час")
            if base.days_held:
                a.notes.append(f"Прибыль на день владения (база): {fmt_rub(base.profit / base.days_held)}")
        if a.defects.items:
            parts.append("дефект: " + ", ".join(i.label for i in a.defects.items))
        if a.logistics.mode in ("pickup", "delivery"):
            parts.append(a.logistics.note)
            a.notes.append(f"Логистика: {a.logistics.note}")
        a.main_argument = "; ".join(parts) or "—"
        a.main_risk = a.risks[0] if a.risks else "Явных рисков в тексте не найдено"
        if a.defects.llm_used:
            a.notes.append("Использован LLM-анализ описания (гипотезы)")
        a.notes.append(f"Оценка: {m.basis}; уверенность {m.confidence}; {band(a.rating)}")


def _logistics_checks(logistics, defective: bool) -> list[str]:
    checks = ["Не вносить предоплату до осмотра и проверки товара"]
    if logistics.mode == "delivery":
        checks = ["Покупать только через доставку с оплатой на платформе Avito; не переводить деньги продавцу напрямую",
                  "До заказа запросить видео включения и работы" + (" и самого дефекта" if defective else ""),
                  "Проверить товар при получении в пункте выдачи, до подтверждения получения"]
    elif logistics.mode == "pickup":
        checks.append(f"Согласовать встречу заранее: {logistics.note}")
    elif logistics.mode == "unknown":
        checks.append("Уточнить город и способ передачи товара")
    return checks
