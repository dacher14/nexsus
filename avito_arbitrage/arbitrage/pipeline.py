"""Исследовательская кампания (раздел 25): сбор → дубликаты → анализ → база → таблица."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .analyzer import Analyzer
from .categories import infer_category
from .config import Settings
from .dedup import deduplicate
from .defects import load_kb
from .export import WorkbookInput, write_xlsx
from .identify import identify_model
from .learning import calibrations, category_performance
from .llm import LLMAnalyzer
from .market import Comp
from .models import Analysis, ListingStatus, parse_price
from .sources import load_listings, source_for, sources_for
from .storage import Storage

log = logging.getLogger(__name__)
WORKING_WORDS = ("working", "исправ", "рабоч", "отличн", "хорош", "нов")


@dataclass
class CampaignResult:
    found: int = 0
    invalid: int = 0
    exact_duplicates: int = 0
    reposts: int = 0
    unique: int = 0
    created: int = 0
    price_changed: int = 0
    marked_unavailable: int = 0
    analyses: list[Analysis] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    workbook: Path | None = None
    sheets_url: str | None = None

    def stats(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if isinstance(v, int)}


def load_comps(path: str | Path) -> list[Comp]:
    """Внешние аналоги: title, price, condition (исправен/неисправен); category и city — необязательно."""
    comps = []
    for raw in source_for(path).fetch():
        price = parse_price(raw.get("price"))
        title = str(raw.get("title") or "")
        if not price or not title:
            continue
        category = infer_category(title, "", str(raw.get("category") or ""))
        mid = identify_model(title, "", category)
        cond = str(raw.get("condition") or "working").lower()
        working = any(w in cond for w in WORKING_WORDS) and "неисправ" not in cond
        comps.append(Comp(mid.key(category), mid.family_key(category), price, working,
                          source=str(raw.get("source") or Path(path).name), city=str(raw.get("city") or "")))
    return comps


def make_llm(storage: Storage, settings: Settings) -> LLMAnalyzer:
    kb = load_kb()
    return LLMAnalyzer([r.code for r in kb.symptoms], [r.code for r in kb.red_flags],
                       model=settings.llm_model, effort=settings.llm_effort,
                       max_calls=settings.llm_max_calls_per_campaign,
                       cache_get=storage.llm_cache_get, cache_put=storage.llm_cache_put)


def make_analyzer(storage: Storage, settings: Settings, use_llm: bool = False) -> Analyzer:
    return Analyzer(settings, calibrations=calibrations(storage), capital_in_use=storage.capital_in_use(),
                    llm=make_llm(storage, settings) if use_llm and settings.llm_enabled else None)


def run_campaign(storage: Storage, settings: Settings, inputs: Iterable[str | Path],
                 comps_path: str | Path | None = None, out_path: str | Path | None = None,
                 use_llm: bool = False, full_recheck: bool = False) -> CampaignResult:
    """full_recheck=True: объявления из базы, отсутствующие в новой выгрузке, помечаются
    как недоступные (не как проданные) — используйте только для полной повторной проверки."""
    result = CampaignResult()
    inputs = list(inputs)
    listings, errors = load_listings(sources_for(inputs))
    result.errors = errors
    result.invalid = len(errors)
    dd = deduplicate(listings)
    result.found = dd.total_found + result.invalid
    result.exact_duplicates = dd.exact_duplicates
    result.reposts = len(dd.reposts)
    result.unique = len(dd.unique)

    with storage.engine.begin() as conn:
        for listing in dd.unique:
            outcome = storage.upsert_listing(listing, conn=conn)
            result.created += outcome == "created"
            result.price_changed += outcome == "price_changed"
    by_id = {l.listing_id: l for l in listings}
    for dup_id, main_id, reason in dd.reposts:
        storage.upsert_listing(by_id[dup_id])
        storage.mark_status(by_id[dup_id].listing_id, ListingStatus.DUPLICATE, duplicate_of=main_id)

    if full_recheck:
        seen = {l.listing_id for l in dd.unique}
        for row in storage.listing_rows():
            if row["listing_id"] not in seen:
                storage.mark_status(row["listing_id"], ListingStatus.UNAVAILABLE)
                result.marked_unavailable += 1

    analyzer = make_analyzer(storage, settings, use_llm)
    external = load_comps(comps_path) if comps_path else []
    result.analyses = analyzer.analyze_all(storage.all_listings(), external)
    storage.save_analyses(result.analyses)
    storage.log("campaign", None, {**result.stats(), "inputs": [str(p) for p in inputs],
                                   "comps": str(comps_path) if comps_path else None})
    if out_path:
        result.workbook = export_workbook(storage, settings, out_path)
    return result


def workbook_input(storage: Storage, settings: Settings) -> WorkbookInput:
    rows = {r["listing_id"]: r for r in storage.listing_rows()}
    analyses = [a for lid, a in storage.latest_analyses(rows).items()]
    analyses.sort(key=lambda a: (a.critical, -a.rating, a.risk))
    listings = {lid: storage.get_listing(lid) for lid in rows}
    campaigns = storage.events(kind="campaign")
    return WorkbookInput(
        settings=settings, listings=listings, analyses=analyses, statuses=storage.user_statuses(),
        negotiations=storage.negotiations(),
        messages={lid: storage.messages(lid) for lid in storage.negotiations()},
        deals={d["listing_id"]: d for d in storage.deals()},
        price_history={lid: storage.price_history(lid) for lid in rows},
        last_checked={lid: r["last_checked"] for lid, r in rows.items()},
        campaign=campaigns[-1]["payload"] if campaigns else {},
        capital_in_use=storage.capital_in_use(),
        category_performance=category_performance(storage),
    )


def export_workbook(storage: Storage, settings: Settings, out_path: str | Path) -> Path:
    return write_xlsx(workbook_input(storage, settings), out_path)
