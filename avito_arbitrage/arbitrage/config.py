"""Настройки пользователя (раздел 3 мастер-промпта).

Значения по умолчанию — стартовые параметры, а не универсальные ограничения.
Переопределяются YAML-файлом (см. config.example.yaml) и переменными окружения.
"""
from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

STRATEGIES = ("working", "diy", "service", "as_is")


@dataclass
class Settings:
    currency: str = "RUB"
    marketplace: str = "Avito"
    # Домашний город: здесь перепродаём. Точное местоположение не нужно.
    region: str = ""
    # Города, куда готовы съездить за товаром: км в одну сторону (дорога = 2 × км × travel_cost_per_km).
    pickup_distances_km: dict[str, int] = field(default_factory=dict)
    max_pickup_km: int = 350
    travel_cost_per_km: float = 7.0
    # Доставка из дальних городов, ₽ по габаритам; None — не возим (крупногабарит).
    delivery_cost: dict[str, int | None] = field(
        default_factory=lambda: {"small": 450, "medium": 900, "large": 2500, "bulky": None})
    delivery_days: int = 5
    inspection_value: int = 1500          # сколько стоит возможность осмотреть до оплаты (выбор поездка/доставка)
    remote_purchase_risk: int = 10        # надбавка к риску: нельзя проверить до оплаты
    # Где искать (план поиска и ссылки для ручного поиска); пусто — домашний город + вся Россия.
    search_cities: list[str] = field(default_factory=list)

    # Капитал и лимиты
    capital_total: int = 30_000
    capital_reserve: int = 0
    max_purchase_price: int = 10_000
    min_net_profit: int = 1_500
    min_roi: float = 0.20
    max_days_to_sell: int = 14
    # Сделка исключается, если максимальная потеря превышает эту долю капитала.
    max_loss_share_of_capital: float = 0.25
    # Допуск риска: conservative — решение по консервативному сценарию;
    # moderate — допускается сделка, прибыльная в базовом сценарии.
    risk_appetite: str = "conservative"

    campaign_target_min: int = 200
    campaign_target_max: int = 400
    top_size: int = 50

    allowed_strategies: list[str] = field(default_factory=lambda: list(STRATEGIES))
    allowed_categories: list[str] = field(default_factory=list)  # пусто = все
    banned_categories: list[str] = field(default_factory=list)

    # Экономика перепродажи
    ask_to_sale_ratio: float = 0.92       # цена выставления ≠ цена сделки
    quick_sale_factor: float = 0.88
    low_confidence_haircut: float = 0.08  # доп. дисконт консервативной цены при слабой оценке
    sale_fee_rate: float = 0.0            # комиссия площадки/доставки на стороне продавца
    sale_fixed_cost: int = 150            # продвижение объявления
    storage_cost_per_day: int = 0
    returns_reserve_rate: float = 0.03    # резерв на возвраты и скрытые дефекты
    hourly_rate: int = 400                # стоимость часа времени пользователя (для DIY)
    untested_success: float = 0.6         # вероятность, что «не проверялся» окажется исправным
    suspicious_price_ratio: float = 0.4   # цена ниже этой доли рынка у «исправного» — подозрительно
    transport_cost: dict[str, int] = field(
        default_factory=lambda: {"small": 300, "medium": 600, "large": 1500, "bulky": 3000})
    service_logistics_cost: dict[str, int] = field(
        default_factory=lambda: {"small": 400, "medium": 800, "large": 1500, "bulky": 1500})
    packaging_cost: dict[str, int] = field(
        default_factory=lambda: {"small": 100, "medium": 200, "large": 0, "bulky": 0})
    diy_skills: list[str] = field(default_factory=lambda: ["basic"])  # basic / soldering / expert

    # Переговоры
    negotiation_max_rounds: int = 3
    negotiation_contact_name: str = ""    # как подписывать сообщения (по желанию пользователя)

    # Автономность
    automation_mode: str = "manual"       # manual: агент готовит черновики, пользователь отправляет
    auto_purchase: bool = False           # покупка — только по отдельному подтверждению

    # LLM-анализ описаний (только для неоднозначных объявлений)
    llm_enabled: bool = False
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "low"
    llm_max_calls_per_campaign: int = 50

    database_url: str = "sqlite:///data/arbitrage.db"

    @property
    def capital_available(self) -> int:
        return max(0, self.capital_total - self.capital_reserve)

    def category_allowed(self, code: str) -> bool:
        if code in self.banned_categories:
            return False
        return not self.allowed_categories or code in self.allowed_categories

    def as_rows(self) -> list[tuple[str, Any]]:
        return [(f.name, getattr(self, f.name)) for f in fields(self)]


def load_settings(path: str | Path | None = None) -> Settings:
    data: dict[str, Any] = {}
    if path:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    known = {f.name for f in fields(Settings)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Неизвестные параметры в {path}: {', '.join(sorted(unknown))}")
    if data.get("auto_purchase"):
        warnings.warn("auto_purchase игнорируется: покупка возможна только после отдельного "
                      "подтверждения пользователя.", stacklevel=2)
        data["auto_purchase"] = False
    unknown_strategies = set(data.get("allowed_strategies", [])) - set(STRATEGIES)
    if unknown_strategies:
        raise ValueError(f"Неизвестные стратегии: {', '.join(sorted(unknown_strategies))}")
    defaults = Settings()
    for key, value in list(data.items()):
        if value is None and isinstance(getattr(defaults, key), (dict, list)):
            data[key] = getattr(defaults, key)  # «ключ:» без значения в YAML — значение по умолчанию
    settings = Settings(**data)
    if os.environ.get("DATABASE_URL"):
        settings.database_url = os.environ["DATABASE_URL"]
    return settings
