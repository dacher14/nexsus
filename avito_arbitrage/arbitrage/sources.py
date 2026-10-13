"""Источники объявлений (раздел 5).

Используются только разрешённые способы получения данных. Система не
обходит CAPTCHA и защитные механизмы и не имитирует сбор данных: если
источник не подключён, это сообщается явно.

Поддерживаемые источники:
  * JSON/CSV-файлы — выгрузка из разрешённого источника или ручной сбор
    (формат описан в README, пример — examples/test_listings.json);
  * AvitoAuthorizedSource — точка подключения официальной/лицензированной
    интеграции. Без неё работа с реальными объявлениями не запускается.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Protocol

from .models import Listing


class SourceNotConfigured(RuntimeError):
    pass


class ListingSource(Protocol):
    name: str

    def fetch(self) -> Iterable[dict[str, Any]]: ...


class JsonFileSource:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.name = f"json:{self.path.name}"

    def fetch(self) -> Iterable[dict[str, Any]]:
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        items = data.get("listings", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError(f"{self.path}: ожидается список объявлений")
        return items


class CsvFileSource:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.name = f"csv:{self.path.name}"

    def fetch(self) -> Iterable[dict[str, Any]]:
        with open(self.path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            if "is_synthetic" in row:
                row["is_synthetic"] = str(row["is_synthetic"]).strip().lower() in ("1", "true", "да", "yes")
        return rows


class AvitoAuthorizedSource:
    """Заглушка для разрешённой интеграции с Avito.

    Официальный API Avito ориентирован на работу с собственными объявлениями и
    чатами аккаунта; поиск чужих объявлений через него не предусмотрен
    (проверьте актуальные условия на developers.avito.ru). Для массового сбора
    нужен источник, условия которого это разрешают. Реализуйте fetch(),
    возвращающий словари в формате Listing.from_dict.
    """

    name = "avito"

    def fetch(self) -> Iterable[dict[str, Any]]:
        raise SourceNotConfigured(
            "Интеграция с Avito не подключена. Подключите разрешённый источник данных "
            "(см. README, раздел «Источники данных») или загрузите выгрузку в JSON/CSV.")


def source_for(path: str | Path) -> ListingSource:
    path = Path(path)
    if path.suffix.lower() == ".json":
        return JsonFileSource(path)
    if path.suffix.lower() == ".csv":
        return CsvFileSource(path)
    raise ValueError(f"Неподдерживаемый формат: {path.suffix} (ожидается .json или .csv)")


def load_listings(sources: Iterable[ListingSource]) -> tuple[list[Listing], list[str]]:
    """Читает и валидирует объявления. Записи без ссылки, цены или заголовка не включаются."""
    listings: list[Listing] = []
    errors: list[str] = []
    for source in sources:
        for n, raw in enumerate(source.fetch(), start=1):
            try:
                listings.append(Listing.from_dict(raw, source=source.name))
            except (ValueError, TypeError) as exc:
                errors.append(f"{source.name} #{n}: {exc}")
    return listings, errors
