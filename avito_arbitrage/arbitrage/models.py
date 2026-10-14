"""Модели данных: объявление, результаты анализа, статусы."""
from __future__ import annotations

import dataclasses
import hashlib
import re
import types
import typing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class UserStatus(str, Enum):
    APPROVED = "APPROVED"          # одобрено для переговоров
    WATCHLIST = "WATCHLIST"        # продолжать наблюдение
    REJECTED = "REJECTED"          # исключить
    NEEDS_REVIEW = "NEEDS REVIEW"  # требуется дополнительная проверка


class NegotiationStatus(str, Enum):
    NOT_STARTED = "NOT STARTED"
    DRAFT_READY = "DRAFT READY"      # сообщение подготовлено, не отправлено
    SENT = "SENT"                    # пользователь подтвердил отправку
    COUNTER_OFFER = "COUNTER OFFER"
    AGREED = "AGREED"                # цена согласована; товар не проверен и не куплен
    DEFERRED = "DEFERRED"            # отложено, следим за ценой
    DECLINED = "DECLINED"            # продавец отказался
    CLOSED = "CLOSED"                # контакт прекращён


class ListingStatus(str, Enum):
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"      # исчезло из выдачи — НЕ означает «продано»
    DUPLICATE = "duplicate"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_PRICE_RE = re.compile(r"[^\d]")
_AVITO_ID_RE = re.compile(r"_(\d{6,})(?:[/?#]|$)")


def parse_price(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = _PRICE_RE.sub("", str(value))
    return int(digits) if digits else None


def listing_id_from_url(url: str) -> str:
    """ID объявления Avito — число в конце пути (…/iphone_12_128gb_1234567890)."""
    m = _AVITO_ID_RE.search(url or "")
    if m:
        return m.group(1)
    normalized = re.sub(r"[?#].*$", "", (url or "").strip().lower()).rstrip("/")
    return "u" + hashlib.sha1(normalized.encode()).hexdigest()[:12]


@dataclass
class Listing:
    listing_id: str
    url: str
    title: str
    price: int
    description: str = ""
    category: str = ""
    brand: str = ""
    model: str = ""
    configuration: str = ""
    city: str = ""
    district: str = ""
    published_at: str = ""
    seller_id: str = ""
    seller_name: str = ""
    condition: str = ""
    photos_count: int | None = None
    search_query: str = ""
    source: str = ""
    is_synthetic: bool = False
    discovered_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str = "") -> "Listing":
        url = str(data.get("url") or "").strip()
        title = str(data.get("title") or "").strip()
        price = parse_price(data.get("price"))
        if not url:
            raise ValueError("нет ссылки на объявление")
        if not title:
            raise ValueError("нет заголовка")
        if price is None or price <= 0:
            raise ValueError("нет цены")
        photos = data.get("photos_count")
        return cls(
            listing_id=str(data.get("listing_id") or listing_id_from_url(url)),
            url=url,
            title=title,
            price=price,
            description=str(data.get("description") or ""),
            category=str(data.get("category") or ""),
            brand=str(data.get("brand") or ""),
            model=str(data.get("model") or ""),
            configuration=str(data.get("configuration") or ""),
            city=str(data.get("city") or ""),
            district=str(data.get("district") or ""),
            published_at=str(data.get("published_at") or ""),
            seller_id=str(data.get("seller_id") or ""),
            seller_name=str(data.get("seller_name") or ""),
            condition=str(data.get("condition") or ""),
            photos_count=int(photos) if photos not in (None, "") else None,
            search_query=str(data.get("search_query") or ""),
            source=str(data.get("source") or source),
            is_synthetic=bool(data.get("is_synthetic", False)),
            discovered_at=str(data.get("discovered_at") or now_iso()),
        )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


KEEP_IF_EMPTY = ("description", "seller_id", "seller_name", "condition", "published_at", "photos_count",
                 "configuration", "district", "brand", "model", "city", "category", "search_query")


def merge_listing_data(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Повторно найденное объявление дополняет запись, а не затирает её.

    Цена и заголовок берутся свежие; пустые поля новой записи не стирают известные;
    фрагмент описания из выдачи поиска не заменяет полное описание со страницы объявления.
    """
    merged = dict(new)
    for key in KEEP_IF_EMPTY:
        if new.get(key) in (None, "") and old.get(key) not in (None, ""):
            merged[key] = old[key]
    old_desc, new_desc = str(old.get("description") or ""), str(new.get("description") or "")
    snippet = str(new.get("source") or "").startswith("saved-search")
    if old_desc and (new_desc in old_desc or (snippet and len(old_desc) > len(new_desc))):
        merged["description"] = old_desc
    if old.get("discovered_at"):
        merged["discovered_at"] = old["discovered_at"]
    if not new.get("title"):
        merged["title"] = old.get("title", "")
    return merged


@dataclass
class MarketEstimate:
    model_key: str
    match_level: str = "none"          # model / family / declared / none
    comps_count: int = 0
    working_median: int | None = None
    working_p25: int | None = None
    working_p75: int | None = None
    broken_median: int | None = None
    broken_count: int = 0
    conservative: int | None = None
    base: int | None = None
    optimistic: int | None = None
    quick: int | None = None
    confidence: str = "нет данных"     # высокая / средняя / низкая / нет данных
    basis: str = "цены предложений (не цены завершённых сделок)"
    estimated_at: str = ""


@dataclass
class Logistics:
    mode: str                         # local / pickup / delivery / unavailable / unknown
    city: str = ""
    extra_cost: int = 0               # дорога или доставка сверх обычного самовывоза по городу
    extra_days: int = 0
    inspect_before_payment: bool = True
    note: str = ""


@dataclass
class RedFlag:
    code: str
    label: str
    critical: bool = False
    risk: int = 0
    evidence: str = ""


@dataclass
class RepairItem:
    symptom: str
    label: str
    defect_class: str
    evidence: str
    causes: list[str] = field(default_factory=list)
    alt_causes: list[str] = field(default_factory=list)
    diy_allowed: bool = False
    diy_note: str = ""
    diy_skill: str = "basic"
    diy_parts: tuple[int, int] = (0, 0)
    diy_tools: int = 0
    diy_hours: float = 0.0
    diy_success: float = 0.0
    service_diag: tuple[int, int] = (0, 0)
    service_parts: tuple[int, int] = (0, 0)
    service_labor: tuple[int, int] = (0, 0)
    service_success: float = 0.0
    service_days: int = 0
    as_is_factor: float | None = None
    accessory: bool = False
    checks: list[str] = field(default_factory=list)


@dataclass
class DefectAnalysis:
    items: list[RepairItem] = field(default_factory=list)
    red_flags: list[RedFlag] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)  # мотивация продавца, отказ от торга и т.п.
    defect_class: str | None = None                   # A / B / C / None (дефектов не заявлено)
    diagnosis_confidence: str = "—"
    is_parts_listing: bool = False
    is_untested: bool = False
    resale_factor: float = 1.0
    extra_transport: int = 0
    checks: list[str] = field(default_factory=list)
    llm_used: bool = False

    @property
    def has_defects(self) -> bool:
        return any(not i.accessory for i in self.items)

    @property
    def critical_flags(self) -> list[RedFlag]:
        return [f for f in self.red_flags if f.critical]


@dataclass
class Costs:
    purchase: int = 0
    diagnostics: int = 0
    parts: int = 0
    labor: int = 0
    transport: int = 0
    other: int = 0
    reserve: int = 0
    selling: int = 0

    @property
    def total(self) -> int:
        return (self.purchase + self.diagnostics + self.parts + self.labor + self.transport
                + self.other + self.reserve + self.selling)

    @property
    def without_purchase(self) -> int:
        return self.total - self.purchase


@dataclass
class Scenario:
    name: str                 # conservative / base / optimistic
    resale: int
    costs: Costs
    p_success: float
    profit: int               # ожидаемая денежная прибыль (с учётом вероятности неудачи)
    profit_if_success: int
    profit_if_failure: int
    days_held: int
    time_cost: int = 0
    salvage: int = 0          # выручка при продаже на запчасти в случае неудачи

    @property
    def roi(self) -> float | None:
        return self.profit / self.costs.total if self.costs.total else None

    @property
    def profit_time_adjusted(self) -> int:
        return self.profit - self.time_cost


@dataclass
class Strategy:
    code: str                 # working / diy / service / as_is
    label: str
    feasible: bool
    reason: str = ""
    hours: float = 0.0
    scenarios: dict[str, Scenario] = field(default_factory=dict)
    max_purchase: int | None = None
    max_loss: int = 0
    stress: dict[str, int] = field(default_factory=dict)

    def scenario(self, name: str) -> Scenario | None:
        return self.scenarios.get(name)


@dataclass
class Analysis:
    listing_id: str
    analyzed_at: str
    category: str
    category_name: str
    brand: str
    model: str
    model_key: str
    model_confidence: str
    asking_price: int
    market: MarketEstimate
    defects: DefectAnalysis
    logistics: Logistics = field(default_factory=lambda: Logistics("local"))
    strategies: list[Strategy] = field(default_factory=list)
    best_strategy: str | None = None
    liquidity: int = 5
    days_to_sell: int = 14
    discount: float | None = None
    risk: int = 50
    repair_complexity: int = 1
    rating: int = 0
    rating_components: dict[str, float] = field(default_factory=dict)
    critical: bool = False
    insufficient_data: bool = False
    recommendation: str = ""
    action: str = ""
    main_argument: str = ""
    main_risk: str = ""
    risks: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    start_price: int | None = None
    target_price: int | None = None
    max_deal_price: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def best(self) -> Strategy | None:
        for s in self.strategies:
            if s.code == self.best_strategy:
                return s
        return None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Analysis":
        return _from_dict(cls, data)


def _from_dict(tp: Any, value: Any) -> Any:
    """Восстановление вложенных dataclass из словаря (для чтения истории из БД)."""
    if value is None:
        return None
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        return _from_dict(args[0], value) if len(args) == 1 else value
    if dataclasses.is_dataclass(tp):
        hints = typing.get_type_hints(tp)
        kwargs = {f.name: _from_dict(hints[f.name], value[f.name])
                  for f in dataclasses.fields(tp) if f.name in value}
        return tp(**kwargs)
    if origin is list:
        (arg,) = typing.get_args(tp)
        return [_from_dict(arg, v) for v in value]
    if origin is dict:
        _, arg = typing.get_args(tp)
        return {k: _from_dict(arg, v) for k, v in value.items()}
    if origin is tuple:
        return tuple(value)
    return value
