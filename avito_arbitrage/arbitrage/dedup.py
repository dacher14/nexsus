"""Удаление дубликатов (разделы 6 и 20).

* точный дубликат — тот же ID объявления (повторно найден другим запросом);
* повторная публикация — тот же продавец, заголовок и цена, либо совпадающие
  заголовок и подробное описание при другом ID.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .categories import normalize_text
from .models import Listing


@dataclass
class DedupResult:
    unique: list[Listing]
    exact_duplicates: int
    reposts: list[tuple[str, str, str]]  # (id дубликата, id основной записи, причина)
    total_found: int


def _fingerprint(listing: Listing) -> str | None:
    desc = normalize_text(listing.description)
    if len(desc) < 120:  # короткие шаблонные описания совпадают у разных продавцов
        return None
    return hashlib.sha1(f"{normalize_text(listing.title)}|{desc}".encode()).hexdigest()


def deduplicate(found: list[Listing]) -> DedupResult:
    by_id: dict[str, Listing] = {}
    for listing in found:
        prev = by_id.get(listing.listing_id)
        if prev is None:
            by_id[listing.listing_id] = listing
        else:
            # сохраняем самую свежую цену, но запрос, по которому нашли впервые, не теряем
            listing.search_query = prev.search_query or listing.search_query
            by_id[listing.listing_id] = listing
    exact = len(found) - len(by_id)

    unique: list[Listing] = []
    reposts: list[tuple[str, str, str]] = []
    by_seller_title: dict[tuple[str, str, int], str] = {}
    by_fp: dict[str, str] = {}
    for listing in sorted(by_id.values(), key=lambda l: (l.published_at or l.discovered_at, l.listing_id)):
        title = normalize_text(listing.title)
        # магазин может продавать несколько одинаковых товаров — учитываем и цену
        key = (listing.seller_id, title, round(listing.price, -2)) if listing.seller_id else None
        fp = _fingerprint(listing)
        if key and key in by_seller_title:
            reposts.append((listing.listing_id, by_seller_title[key], "тот же продавец, заголовок и цена"))
            continue
        if fp and fp in by_fp:
            reposts.append((listing.listing_id, by_fp[fp], "совпадают заголовок и описание"))
            continue
        if key:
            by_seller_title[key] = listing.listing_id
        if fp:
            by_fp[fp] = listing.listing_id
        unique.append(listing)
    return DedupResult(unique, exact, reposts, len(found))
