"""Содержимое рабочего документа (раздел 13), независимое от формата.

Один и тот же набор листов записывается в XLSX (xlsx.py) и в Google Sheets
(gsheets.py). Формулы — в синтаксисе A1 с запятыми, их понимают оба формата.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from openpyxl.utils import get_column_letter

from ..analyzer import fmt_rub
from ..config import Settings
from ..models import Analysis, Listing, NegotiationStatus, UserStatus

USER_STATUSES = [s.value for s in UserStatus]
NEGOTIATION_STATUSES = [s.value for s in NegotiationStatus]


@dataclass
class Column:
    header: str
    width: int = 14
    fmt: str = "text"  # text / rub / pct / int / url / wrap


@dataclass
class Table:
    title: str | None
    columns: list[Column]
    rows: list[list[Any]]


@dataclass
class Sheet:
    name: str
    tables: list[Table]
    freeze_header: bool = False
    autofilter: bool = False
    validations: dict[str, list[str]] = field(default_factory=dict)  # заголовок → варианты
    highlight: dict[str, str] = field(default_factory=dict)           # заголовок → правило (risk/rating/reco)
    banner: str | None = None


@dataclass
class WorkbookInput:
    settings: Settings
    listings: dict[str, Listing]
    analyses: list[Analysis]
    statuses: dict[str, str] = field(default_factory=dict)
    negotiations: dict[str, dict] = field(default_factory=dict)
    messages: dict[str, list[dict]] = field(default_factory=dict)
    deals: dict[str, dict] = field(default_factory=dict)
    price_history: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    last_checked: dict[str, str] = field(default_factory=dict)  # когда объявление видели в источнике
    campaign: dict[str, Any] = field(default_factory=dict)
    capital_in_use: int = 0
    category_performance: list[dict] = field(default_factory=list)
    duplicates: dict[str, str] = field(default_factory=dict)  # id → основная запись

    @property
    def has_synthetic(self) -> bool:
        return any(l.is_synthetic for l in self.listings.values())


SYNTHETIC_BANNER = ("ТЕСТОВЫЕ ДАННЫЕ: объявления синтетические (проверка работоспособности), "
                    "это не реальные предложения рынка.")

ALL_DEALS_COLUMNS = [
    Column("ID записи", 13), Column("Дата обнаружения", 12), Column("Дата последней проверки", 12),
    Column("Категория", 16), Column("Бренд", 11), Column("Модель", 20), Column("Комплектация", 16),
    Column("Город", 12), Column("Район", 12), Column("Цена продавца", 12, "rub"),
    Column("Ссылка на объявление", 30, "url"), Column("Дата публикации", 12), Column("Состояние", 14),
    Column("Описание", 40, "wrap"), Column("Выявленные дефекты", 30, "wrap"),
    Column("Предполагаемая причина поломки", 32, "wrap"), Column("Уверенность в диагнозе", 18),
    Column("Рыночная цена исправного товара", 14, "rub"), Column("Консервативная цена перепродажи", 14, "rub"),
    Column("Базовая цена перепродажи", 14, "rub"), Column("Цена быстрой продажи", 13, "rub"),
    Column("Количество аналогов", 10, "int"), Column("Уверенность рыночной оценки", 13),
    Column("Стоимость диагностики", 12, "rub"), Column("Стоимость запчастей", 12, "rub"),
    Column("Стоимость работы", 12, "rub"), Column("Транспортные расходы", 12, "rub"),
    Column("Прочие расходы", 12, "rub"), Column("Резерв на дополнительные расходы", 13, "rub"),
    Column("Расходы на продажу", 12, "rub"), Column("Полная себестоимость", 13, "rub"),
    Column("Консервативная прибыль", 13, "rub"), Column("Базовая прибыль", 13, "rub"),
    Column("ROI (консерв.)", 9, "pct"), Column("Предполагаемый срок продажи, дн.", 11, "int"),
    Column("Ликвидность (1–10)", 10, "int"), Column("Риск (0–100)", 9, "int"),
    Column("Сложность ремонта (1–10)", 10, "int"), Column("Максимальная цена покупки", 13, "rub"),
    Column("Рекомендуемая стартовая цена торга", 13, "rub"), Column("Целевая цена сделки", 13, "rub"),
    Column("Максимальная цена сделки", 13, "rub"), Column("Общий рейтинг", 9, "int"),
    Column("Основные риски", 36, "wrap"), Column("Что проверить до покупки", 40, "wrap"),
    Column("Рекомендация агента", 34, "wrap"), Column("Статус пользователя", 14),
    Column("Статус переговоров", 15), Column("Согласованная цена", 12, "rub"),
    Column("Фактическая цена покупки", 12, "rub"), Column("Фактические расходы", 12, "rub"),
    Column("Фактическая цена продажи", 12, "rub"), Column("Фактическая чистая прибыль", 13, "rub"),
    Column("Комментарии", 50, "wrap"),
]
assert len(ALL_DEALS_COLUMNS) == 54


def _col(header: str) -> str:
    idx = next(i for i, c in enumerate(ALL_DEALS_COLUMNS) if c.header == header)
    return get_column_letter(idx + 1)


def _join(items: list[str], sep: str = "; ", limit: int | None = None) -> str:
    items = [i for i in items if i]
    return sep.join(items[:limit] if limit else items)


def _quick_price(a: Analysis, settings: Settings) -> int | None:
    best = a.best
    if not best or not best.scenarios:
        return a.market.quick
    if best.code == "as_is":
        return int(best.scenarios["conservative"].resale * settings.quick_sale_factor)
    return int(a.market.quick * a.defects.resale_factor) if a.market.quick else None


def _defects_text(a: Analysis) -> str:
    parts = [f"{i.label} («{i.evidence}»)" for i in a.defects.items]
    if a.defects.is_untested:
        parts.append("Не проверялся продавцом")
    if a.defects.is_parts_listing:
        parts.append("Продаётся на запчасти/под восстановление")
    return _join(parts, "\n") or "Не заявлены"


def _causes_text(a: Analysis) -> str:
    rows = []
    for i in a.defects.items:
        if i.accessory:
            continue
        alt = f" (альтернативы: {', '.join(i.alt_causes)})" if i.alt_causes else ""
        rows.append(f"Гипотеза [{i.defect_class}]: {', '.join(i.causes)}{alt}")
    return _join(rows, "\n")


def all_deals_rows(data: WorkbookInput) -> list[list[Any]]:
    s = data.settings
    rows = []
    for r, a in enumerate(data.analyses, start=2):
        l = data.listings[a.listing_id]
        best = a.best
        cons = best.scenarios.get("conservative") if best else None
        base = best.scenarios.get("base") if best else None
        neg = data.negotiations.get(a.listing_id, {})
        deal = (data.deals.get(a.listing_id) or {}).get("data", {})
        history = data.price_history.get(a.listing_id, [])
        comments = list(a.notes)
        if l.is_synthetic:
            comments.insert(0, "ТЕСТОВЫЕ ДАННЫЕ (синтетическое объявление)")
        if len(history) > 1:
            comments.append("История цены: " + " → ".join(fmt_rub(p) for _, p in history))
        if best:
            comments.append(f"Стратегия: {best.label}; макс. потеря капитала {fmt_rub(best.max_loss)}")
        if l.search_query:
            comments.append(f"Найдено по запросу: «{l.search_query}»")

        price_cell = f"{_col('Цена продавца')}{r}"
        cost_cells = [f"{_col(h)}{r}" for h in (
            "Стоимость диагностики", "Стоимость запчастей", "Стоимость работы", "Транспортные расходы",
            "Прочие расходы", "Резерв на дополнительные расходы", "Расходы на продажу")]
        total_cell = f"{_col('Полная себестоимость')}{r}"
        cons_cell = f"{_col('Консервативная прибыль')}{r}"
        buy, extra, sold = (f"{_col(h)}{r}" for h in (
            "Фактическая цена покупки", "Фактические расходы", "Фактическая цена продажи"))

        rows.append([
            a.listing_id, (l.discovered_at or "")[:10], data.last_checked.get(a.listing_id, a.analyzed_at)[:10],
            a.category_name,
            a.brand or "неизвестно", a.model or "неизвестно",
            l.configuration or a.market.model_key.split(":")[-1] or "не указано",
            l.city, l.district, l.price, l.url, (l.published_at or "")[:10], l.condition or "не указано",
            l.description[:1000], _defects_text(a), _causes_text(a), a.defects.diagnosis_confidence,
            a.market.working_median, cons.resale if cons else None, base.resale if base else None,
            _quick_price(a, s), a.market.comps_count, a.market.confidence,
            cons.costs.diagnostics if cons else None, cons.costs.parts if cons else None,
            cons.costs.labor if cons else None, cons.costs.transport if cons else None,
            cons.costs.other if cons else None, cons.costs.reserve if cons else None,
            cons.costs.selling if cons else None,
            f"={price_cell}+" + "+".join(cost_cells) if cons else None,
            f"={_col('Консервативная цена перепродажи')}{r}-{total_cell}" if cons else None,
            base.profit if base else None,
            f'=IF({total_cell}>0,{cons_cell}/{total_cell},"")' if cons else None,
            base.days_held if base else a.days_to_sell, a.liquidity, a.risk, a.repair_complexity,
            best.max_purchase if best else None, a.start_price, a.target_price, a.max_deal_price,
            a.rating, _join(a.risks, "\n", 5), _join([f"• {c}" for c in a.checks], "\n"),
            a.recommendation, data.statuses.get(a.listing_id, ""),
            neg.get("status", NegotiationStatus.NOT_STARTED.value), neg.get("agreed_price"),
            deal.get("purchase_price"), deal.get("costs_so_far"), deal.get("sold_price"),
            f'=IF(AND({sold}<>"",{buy}<>""),{sold}-{buy}-N({extra}),"")',
            _join(comments, "\n"),
        ])
    return rows


def top_rows(data: WorkbookInput) -> list[list[Any]]:
    candidates = [a for a in data.analyses if a.best and not a.critical and not a.insufficient_data
                  and data.statuses.get(a.listing_id) != UserStatus.REJECTED.value]
    candidates.sort(key=lambda a: (a.rating, a.best.scenarios["conservative"].profit), reverse=True)
    rows = []
    for a in candidates[: data.settings.top_size]:
        l = data.listings[a.listing_id]
        cons, base = a.best.scenarios["conservative"], a.best.scenarios["base"]
        rows.append([a.listing_id, l.title, l.url, l.price, a.market.working_median, cons.costs.total,
                     cons.profit, base.profit, cons.roi, a.best.max_purchase, a.liquidity, a.risk, a.rating,
                     a.main_argument, a.main_risk, a.recommendation])
    return rows


TOP_COLUMNS = [
    Column("ID", 13), Column("Товар", 30, "wrap"), Column("Ссылка", 30, "url"), Column("Цена", 11, "rub"),
    Column("Рыночная стоимость (медиана исправного)", 14, "rub"), Column("Полная себестоимость", 13, "rub"),
    Column("Консервативная прибыль", 13, "rub"), Column("Базовая прибыль", 13, "rub"),
    Column("ROI (консерв.)", 9, "pct"), Column("Максимальная цена покупки", 13, "rub"),
    Column("Ликвидность", 10, "int"), Column("Риск", 8, "int"), Column("Рейтинг", 8, "int"),
    Column("Главный аргумент", 40, "wrap"), Column("Основной риск", 34, "wrap"),
    Column("Рекомендуемое действие", 34, "wrap"),
]

REPAIR_COLUMNS = [
    Column("ID", 13), Column("Ссылка", 26, "url"), Column("Модель", 22), Column("Симптомы", 30, "wrap"),
    Column("Возможная причина", 30, "wrap"), Column("Альтернативные причины", 30, "wrap"),
    Column("Самостоятельный ремонт", 32, "wrap"), Column("Сервисный ремонт", 32, "wrap"),
    Column("Стоимость деталей", 16), Column("Стоимость работ", 16), Column("Доступность компонентов", 22, "wrap"),
    Column("Сложность (1–10)", 10, "int"), Column("Вероятность успешного ремонта", 18, "wrap"),
    Column("Цена при неудаче (запчасти)", 13, "rub"), Column("Продажа на запчасти", 22, "wrap"),
    Column("Максимальная цена покупки", 13, "rub"), Column("Итоговое решение", 36, "wrap"),
]


def _rng(lo: float, hi: float) -> str:
    return fmt_rub(lo) if int(lo) == int(hi) else f"{fmt_rub(lo)} – {fmt_rub(hi)}"


def repair_rows(data: WorkbookInput) -> list[list[Any]]:
    rows = []
    for a in data.analyses:
        d = a.defects
        if not (d.items or d.is_untested or d.is_parts_listing):
            continue
        l = data.listings[a.listing_id]
        strat = {s.code: s for s in a.strategies}
        repairs = [i for i in d.items if not i.accessory]

        def describe(code: str) -> str:
            st = strat.get(code)
            if not st:
                return "—"
            if not st.feasible:
                return f"Нет: {st.reason or 'не применимо'}"
            cons, base = st.scenarios["conservative"], st.scenarios["base"]
            text = (f"Да. Себестоимость (база) {fmt_rub(base.costs.total)}, прибыль база {fmt_rub(base.profit)}, "
                    f"конс. {fmt_rub(cons.profit)}")
            if code == "diy":
                text += f"; ~{st.hours:g} ч, с учётом времени {fmt_rub(base.profit_time_adjusted)}"
            return text

        parts_lo = sum(i.service_parts[0] for i in repairs)
        parts_hi = sum(i.service_parts[1] for i in repairs)
        labor_lo = sum(i.service_labor[0] + i.service_diag[0] for i in repairs)
        labor_hi = sum(i.service_labor[1] + i.service_diag[1] for i in repairs)
        success = []
        for code, name in (("diy", "DIY"), ("service", "сервис")):
            st = strat.get(code)
            if st and st.feasible and st.scenarios:
                success.append(f"{name} ~{round(st.scenarios['base'].p_success * 100)}%")
        as_is = strat.get("as_is")
        as_is_text = "—"
        if as_is and as_is.feasible and as_is.scenarios:
            as_is_text = (f"Да, ~{fmt_rub(as_is.scenarios['base'].resale)}; прибыль база "
                          f"{fmt_rub(as_is.scenarios['base'].profit)} (спрос не гарантирован)")
        salvage = next((st.scenarios["base"].salvage for st in a.strategies if st.scenarios), None)
        availability = {"A": "Типовые детали, обычно доступны (проверить наличие)",
                        "B": "Под заказ/уточнить совместимость", "C": "Не ясно до диагностики"}
        rows.append([
            a.listing_id, l.url, f"{a.brand} {a.model}".strip() or l.title,
            _join([f"{i.label}: «{i.evidence}»" for i in d.items], "\n")
            or ("Не проверялся" if d.is_untested else "На запчасти, симптомы не описаны"),
            _join([", ".join(i.causes) for i in repairs], "\n"),
            _join([", ".join(i.alt_causes) for i in repairs if i.alt_causes], "\n"),
            describe("diy"), describe("service"),
            _rng(parts_lo, parts_hi) if repairs else "—", _rng(labor_lo, labor_hi) if repairs else "—",
            availability.get(d.defect_class or "", "—"), a.repair_complexity,
            (", ".join(success) + " — оценка справочника, не статистика") if success else "—",
            salvage, as_is_text, a.best.max_purchase if a.best else None,
            f"{a.recommendation}" + (f" (стратегия: {a.best.label})" if a.best else ""),
        ])
    return rows


NEGOTIATION_COLUMNS = [
    Column("ID сделки", 13), Column("Ссылка", 26, "url"), Column("Идентификатор продавца", 16),
    Column("Первоначальная цена", 12, "rub"), Column("Стартовое предложение", 12, "rub"),
    Column("Целевая цена", 12, "rub"), Column("Максимальная цена (НЕ раскрывать продавцу)", 14, "rub"),
    Column("Стратегия торга", 30, "wrap"), Column("Первое сообщение", 50, "wrap"),
    Column("Ответ продавца", 40, "wrap"), Column("Встречная цена", 12, "rub"), Column("Следующий шаг", 36, "wrap"),
    Column("Статус", 15), Column("Дата контакта", 12), Column("Результат переговоров", 30, "wrap"),
]


def negotiation_rows(data: WorkbookInput) -> list[list[Any]]:
    by_id = {a.listing_id: a for a in data.analyses}
    ids = [lid for lid, st in data.statuses.items() if st == UserStatus.APPROVED.value]
    ids += [lid for lid in data.negotiations if lid not in ids]
    rows = []
    for lid in ids:
        l = data.listings.get(lid)
        if not l:
            continue
        a = by_id.get(lid)
        neg = data.negotiations.get(lid, {})
        msgs = data.messages.get(lid, [])
        first_out = next((m for m in msgs if m["direction"] == "out"), None)
        last_in = next((m for m in reversed(msgs) if m["direction"] == "in"), None)
        sent = [m for m in msgs if m["direction"] == "out" and m["sent"]]
        status = neg.get("status", NegotiationStatus.NOT_STARTED.value)
        result = ""
        if status == NegotiationStatus.AGREED.value:
            result = f"Согласовано {fmt_rub(neg.get('agreed_price'))} — требуется проверка и подтверждение покупки"
        elif status in (NegotiationStatus.DECLINED.value, NegotiationStatus.CLOSED.value,
                        NegotiationStatus.DEFERRED.value):
            result = neg.get("next_step", "")
        if not neg.get("authorized_at"):
            next_step = "Ждём явной команды пользователя на переговоры"
        else:
            next_step = neg.get("next_step", "")
        first_text = first_out["text"] if first_out else ""
        if first_out and not first_out["sent"]:
            first_text = "[ЧЕРНОВИК — не отправлено]\n" + first_text
        rows.append([
            lid, l.url, l.seller_id or l.seller_name or "неизвестно", l.price,
            neg.get("start_price") or (a.start_price if a else None),
            neg.get("target_price") or (a.target_price if a else None),
            neg.get("max_price") or (a.max_deal_price if a else None),
            neg.get("strategy", ""), first_text, last_in["text"] if last_in else "",
            neg.get("seller_price"), next_step, status, (sent[0]["ts"][:10] if sent else ""), result,
        ])
    return rows


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def market_tables(data: WorkbookInput) -> list[Table]:
    s = data.settings
    analyses = data.analyses
    camp = data.campaign
    profitable = [a for a in analyses if a.best and not a.critical and a.best.max_purchase
                  and a.asking_price <= a.best.max_purchase]
    high_risk = [a for a in analyses if a.risk >= 70]
    insufficient = [a for a in analyses if a.insufficient_data]
    n = len(analyses) or 1
    summary = Table("Сводка кампании", [Column("Показатель", 50), Column("Значение", 20)], [
        ["Найдено объявлений (с повторами)", camp.get("found", len(analyses))],
        ["Отклонено при чтении (нет ссылки/цены/заголовка)", camp.get("invalid", 0)],
        ["Точных дубликатов", camp.get("exact_duplicates", 0)],
        ["Повторных публикаций", camp.get("reposts", 0)],
        ["Уникальных объявлений в анализе", len(analyses)],
        ["Прошли первичную фильтрацию (без критических рисков и с оценкой)",
         sum(1 for a in analyses if not a.critical and not a.insufficient_data)],
        ["Потенциально выгодных при текущей цене", len(profitable)],
        ["Выгодны после торга", sum(1 for a in analyses if a.action == "Торговаться")],
        ["Требуют диагностики (класс B/C или не проверялись)",
         sum(1 for a in analyses if a.defects.defect_class in ("B", "C") or a.defects.is_untested)],
        ["Лучшая стратегия — самостоятельный ремонт", sum(1 for a in analyses if a.best_strategy == "diy")],
        ["Лучшая стратегия — сервисный ремонт", sum(1 for a in analyses if a.best_strategy == "service")],
        ["Лучшая стратегия — продажа без ремонта/на запчасти", sum(1 for a in analyses if a.best_strategy == "as_is")],
        ["Высокий риск (≥70)", len(high_risk)],
        ["Критические риски (исключены)", sum(1 for a in analyses if a.critical)],
        ["Доля объявлений с недостаточными данными", f"{round(100 * len(insufficient) / n)}%"],
        ["Целевой объём кампании", f"{s.campaign_target_min}–{s.campaign_target_max}"],
        ["Тестовые (синтетические) данные", "ДА" if data.has_synthetic else "нет"],
    ])

    by_cat: dict[str, list[Analysis]] = defaultdict(list)
    for a in analyses:
        by_cat[a.category_name].append(a)
    cat_rows = []
    for name, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        profits = [a.best.scenarios["conservative"].profit for a in items if a.best]
        discounts = [a.discount for a in items if a.discount is not None]
        cat_rows.append([
            name, len(items), _median(discounts), _median(profits),
            sum(1 for a in items if a in profitable), round(statistics.mean(a.risk for a in items)),
            sum(1 for a in items if a.critical), _median([a.days_to_sell for a in items]),
        ])
    categories = Table("По категориям", [
        Column("Категория", 26), Column("Объявлений", 11, "int"), Column("Медианный дисконт", 12, "pct"),
        Column("Медиана конс. прибыли", 14, "rub"), Column("Выгодных", 10, "int"), Column("Средний риск", 10, "int"),
        Column("Критических", 10, "int"), Column("Медианный срок продажи, дн.", 12, "int")], cat_rows)

    by_model: dict[str, list[Analysis]] = defaultdict(list)
    for a in analyses:
        if a.model:
            by_model[f"{a.brand} {a.model}".strip()].append(a)
    model_rows = []
    for name, items in by_model.items():
        profits = [a.best.scenarios["conservative"].profit for a in items if a.best and not a.critical]
        model_rows.append([name, len(items), _median([a.asking_price for a in items]),
                           _median([a.market.working_median for a in items if a.market.working_median]),
                           sum(1 for a in items if a in profitable), max(profits) if profits else None])
    model_rows.sort(key=lambda r: (r[4], r[5] or -10**9), reverse=True)
    models = Table("Самые перспективные модели", [
        Column("Модель", 30), Column("Объявлений", 11, "int"), Column("Медиана цены", 12, "rub"),
        Column("Медиана рынка (исправные)", 14, "rub"), Column("Выгодных", 10, "int"),
        Column("Лучшая конс. прибыль", 14, "rub")], model_rows[:30])

    symptom_counter: Counter = Counter()
    symptom_info: dict[str, tuple[str, list[int], list[bool]]] = {}
    for a in analyses:
        for i in a.defects.items:
            symptom_counter[i.label] += 1
            cls, parts, diy = symptom_info.setdefault(i.label, (i.defect_class, [], []))
            parts.append((i.service_parts[0] + i.service_parts[1]) // 2)
            diy.append(i.diy_allowed)
    defect_rows = [[label, cnt, symptom_info[label][0], _median(symptom_info[label][1]),
                    f"{round(100 * sum(symptom_info[label][2]) / cnt)}%"]
                   for label, cnt in symptom_counter.most_common()]
    defects = Table("Типичные неисправности и детали (оценка по справочнику)", [
        Column("Неисправность", 30), Column("Объявлений", 11, "int"), Column("Класс", 8),
        Column("Медиана стоимости деталей", 14, "rub"), Column("Доля, где допустим DIY", 12)], defect_rows)

    by_query: dict[str, list[Analysis]] = defaultdict(list)
    for a in analyses:
        q = data.listings[a.listing_id].search_query or "(запрос не указан)"
        by_query[q].append(a)
    query_rows = [[q, len(items), sum(1 for a in items if a in profitable),
                   sum(1 for a in items if a.critical), max(a.rating for a in items)]
                  for q, items in sorted(by_query.items(), key=lambda kv: -len(kv[1]))]
    queries = Table("Эффективность поисковых запросов", [
        Column("Запрос", 40), Column("Уникальных объявлений", 12, "int"), Column("Выгодных", 10, "int"),
        Column("Критических", 10, "int"), Column("Лучший рейтинг", 10, "int")], query_rows)

    listings = [data.listings[a.listing_id] for a in analyses]
    pct = lambda k: f"{round(100 * k / n)}%"  # noqa: E731
    quality = Table("Полнота и качество данных", [Column("Показатель", 50), Column("Значение", 20)], [
        ["С описанием от 40 символов", pct(sum(1 for l in listings if len(l.description.strip()) >= 40))],
        ["С указанным городом", pct(sum(1 for l in listings if l.city))],
        ["С датой публикации", pct(sum(1 for l in listings if l.published_at))],
        ["Модель определена с высокой уверенностью", pct(sum(1 for a in analyses if a.model_confidence == "высокая"))],
        ["Модель определена с низкой уверенностью", pct(sum(1 for a in analyses if a.model_confidence.startswith("низкая")))],
        ["Без аналогов для оценки", pct(len(insufficient))],
        ["Оценка по семейству модели (не точная модель)", pct(sum(1 for a in analyses if a.market.match_level == "family"))],
        ["Использован LLM-анализ", pct(sum(1 for a in analyses if a.defects.llm_used))],
        ["Основа рыночной оценки", "цены предложений, не завершённых сделок"],
    ])
    tables = [summary, categories, models, defects, queries, quality]
    if data.category_performance:
        tables.append(Table("Фактические результаты по категориям (завершённые сделки)", [
            Column("Категория", 20), Column("Сделок", 8, "int"), Column("Прибыль", 12, "rub"),
            Column("Прибыль на 1 ₽ вложений", 12), Column("Средний срок, дн.", 10),
            Column("Прибыль в день", 12, "rub")],
            [[r["category"], r["deals"], r["profit"], r["profit_per_rub"], r["avg_days"], r["profit_per_day"]]
             for r in data.category_performance]))
    return tables


SETTING_LABELS = {
    "region": "Регион", "nearby_areas": "Соседние районы", "capital_total": "Бюджет (капитал), ₽",
    "capital_reserve": "Резервный капитал, ₽", "max_purchase_price": "Максимальная цена одной покупки, ₽",
    "min_net_profit": "Минимальная чистая прибыль, ₽", "min_roi": "Минимальный ROI",
    "max_days_to_sell": "Допустимый срок перепродажи, дн.", "max_loss_share_of_capital": "Макс. потеря на сделку (доля капитала)",
    "risk_appetite": "Допуск риска", "allowed_categories": "Разрешённые категории (пусто = все)",
    "banned_categories": "Запрещённые категории", "allowed_strategies": "Разрешённые стратегии",
    "low_confidence_haircut": "Минимальный запас прочности (дисконт при слабой оценке)",
    "negotiation_max_rounds": "Лимит раундов торга", "automation_mode": "Режим автоматизации",
    "auto_purchase": "Автоматическая покупка",
}


def settings_rows(data: WorkbookInput) -> list[list[Any]]:
    s = data.settings
    rows: list[list[Any]] = []
    for key, value in s.as_rows():
        if key == "database_url":
            continue
        if isinstance(value, (list, dict)):
            value = ", ".join(f"{k}: {v}" for k, v in value.items()) if isinstance(value, dict) else ", ".join(value)
        if key == "auto_purchase":
            value = "ЗАПРЕЩЕНА — только по отдельному подтверждению"
        rows.append([SETTING_LABELS.get(key, key), value])
    rows += [
        ["— Капитал —", ""],
        ["Капитал в открытых сделках, ₽", data.capital_in_use],
        ["Доступно для новых покупок, ₽", max(0, s.capital_available - data.capital_in_use)],
        ["Переговоры", "Только после статуса APPROVED и явной команды пользователя"],
        ["Отправка сообщений", "Черновики; отправляет пользователь вручную"],
    ]
    return rows


def build_sheets(data: WorkbookInput) -> list[Sheet]:
    banner = SYNTHETIC_BANNER if data.has_synthetic else None
    return [
        Sheet("ALL DEALS", [Table(None, ALL_DEALS_COLUMNS, all_deals_rows(data))], freeze_header=True,
              autofilter=True,
              validations={"Статус пользователя": USER_STATUSES, "Статус переговоров": NEGOTIATION_STATUSES},
              highlight={"Риск (0–100)": "risk", "Общий рейтинг": "rating", "Рекомендация агента": "reco"}),
        Sheet("TOP OPPORTUNITIES", [Table(None, TOP_COLUMNS, top_rows(data))], freeze_header=True, autofilter=True,
              highlight={"Риск": "risk", "Рейтинг": "rating"}, banner=banner),
        Sheet("REPAIR LAB", [Table(None, REPAIR_COLUMNS, repair_rows(data))], freeze_header=True, autofilter=True),
        Sheet("SELLER NEGOTIATIONS", [Table(None, NEGOTIATION_COLUMNS, negotiation_rows(data))],
              freeze_header=True, autofilter=True, validations={"Статус": NEGOTIATION_STATUSES}),
        Sheet("MARKET INTELLIGENCE", market_tables(data), banner=banner),
        Sheet("SETTINGS & CAPITAL", [Table(None, [Column("Параметр", 50), Column("Значение", 40)],
                                           settings_rows(data))], banner=banner),
    ]
