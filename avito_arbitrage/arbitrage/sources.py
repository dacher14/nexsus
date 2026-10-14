"""Источники объявлений (раздел 5).

Используются только разрешённые способы получения данных. Система не
обходит CAPTCHA и защитные механизмы и не имитирует сбор данных: если
источник не подключён, это сообщается явно.

Поддерживаемые источники:
  * страницы Avito, сохранённые пользователем в своём браузере (.html/.mhtml) —
    поиск или отдельные объявления (html_import.py);
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

from .html_import import HTML_SUFFIXES, SavedPageSource
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


SUPPORTED = (".json", ".csv") + HTML_SUFFIXES


def source_for(path: str | Path) -> ListingSource:
    path = Path(path)
    if path.suffix.lower() == ".json":
        return JsonFileSource(path)
    if path.suffix.lower() == ".csv":
        return CsvFileSource(path)
    if path.suffix.lower() in HTML_SUFFIXES:
        return SavedPageSource([path])
    raise ValueError(f"Неподдерживаемый формат: {path.suffix} (ожидается {', '.join(SUPPORTED)})")


def sources_for(paths: Iterable[str | Path]) -> list[ListingSource]:
    """Файлы и папки: папка разворачивается во все поддерживаемые файлы внутри,
    сохранённые страницы Avito объединяются в один источник."""
    files: list[Path] = []
    for p in map(Path, paths):
        if not p.exists():
            raise FileNotFoundError(f"Не найден файл или папка: {p}")
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in SUPPORTED
                            and not any(part.startswith(".") for part in f.relative_to(p).parts))
        else:
            files.append(p)
    pages = [f for f in files if f.suffix.lower() in HTML_SUFFIXES]
    sources = [source_for(f) for f in files if f.suffix.lower() not in HTML_SUFFIXES]
    if pages:
        sources.append(SavedPageSource(pages))
    return sources


def load_listings(sources: Iterable[ListingSource]) -> tuple[list[Listing], list[str]]:
    """Читает и валидирует объявления. Записи без ссылки, цены или заголовка не включаются."""
    listings: list[Listing] = []
    errors: list[str] = []
    for source in sources:
        for n, raw in enumerate(source.fetch(), start=1):
            if "_error" in raw:
                errors.append(f"{source.name}: {raw['_error']}")
                continue
            try:
                listings.append(Listing.from_dict(raw, source=source.name))
            except (ValueError, TypeError) as exc:
                errors.append(f"{source.name} #{n}: {exc}")
    return listings, errors
