"""География сделки: домашний рынок, самовывоз из соседних городов, доставка издалека.

Перепродажа идёт в домашнем городе (settings.region). Товар из другого города
покупается либо с поездкой (города из pickup_distances_km в пределах
max_pickup_km), либо с доставкой. При доставке осмотр возможен только при
получении, поэтому риск выше; крупногабарит издалека не возим.
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus

from .config import Settings
from .models import Logistics

# slug в адресе Avito → город (для разбора сохранённых страниц и ссылок поиска)
CITY_SLUGS = {
    "tyumen": "Тюмень", "yalutorovsk": "Ялуторовск", "zavodoukovsk": "Заводоуковск",
    "tobolsk": "Тобольск", "ishim": "Ишим", "kurgan": "Курган", "shadrinsk": "Шадринск",
    "ekaterinburg": "Екатеринбург", "kamensk-uralskiy": "Каменск-Уральский", "chelyabinsk": "Челябинск",
    "omsk": "Омск", "surgut": "Сургут", "hanty-mansiysk": "Ханты-Мансийск", "nizhnevartovsk": "Нижневартовск",
    "novosibirsk": "Новосибирск", "perm": "Пермь", "moskva": "Москва", "sankt-peterburg": "Санкт-Петербург",
    "kazan": "Казань", "ufa": "Уфа", "samara": "Самара", "rossiya": "",
}
SLUG_BY_CITY = {v: k for k, v in CITY_SLUGS.items() if v}


def normalize_city(name: str | None) -> str:
    name = (name or "").strip().lower().replace("ё", "е")
    name = re.sub(r"^(?:г\.|город)\s*", "", name)
    return name.split(",")[0].strip()


def city_from_slug(slug: str) -> str:
    return CITY_SLUGS.get(slug, slug.replace("_", " ").replace("-", " ").title())


def logistics_for(city: str, size: str, settings: Settings) -> Logistics:
    if not settings.region:
        return Logistics("local", note="домашний город не задан — логистика не учитывается")
    home = normalize_city(settings.region)
    where = normalize_city(city)
    if not where:
        return Logistics("unknown", note="город в объявлении не указан — уточнить у продавца")
    city = re.sub(r"^(?:г\.|город)\s*", "", city.strip())
    if where == home:
        return Logistics("local", city=city)
    distances = {normalize_city(k): v for k, v in settings.pickup_distances_km.items()}
    km = distances.get(where)
    delivery = settings.delivery_cost.get(size)
    if km is not None and km <= settings.max_pickup_km:
        cost = round(2 * km * settings.travel_cost_per_km)
        # поездка выгоднее, если дешевле доставки с учётом ценности осмотра до оплаты
        if delivery is None or cost <= delivery + settings.inspection_value:
            return Logistics("pickup", city=city, extra_cost=cost, extra_days=1 if km > 150 else 0,
                             note=f"самовывоз: {city}, ~{km} км в одну сторону, дорога ≈{cost} ₽")
    if delivery is None:
        return Logistics("unavailable", city=city,
                         note=f"крупногабарит в другом городе ({city}) вне зоны самовывоза — доставка нецелесообразна")
    return Logistics("delivery", city=city, extra_cost=int(delivery), extra_days=settings.delivery_days,
                     inspect_before_payment=False,
                     note=f"доставка, город продавца — {city}: ≈{int(delivery)} ₽, +{settings.delivery_days} дн.; "
                          f"осмотр только при получении")


def search_url(city: str, query: str) -> str:
    """Ссылка для ручного поиска в браузере (агент сам Avito не открывает)."""
    slug = SLUG_BY_CITY.get(city)
    if slug is None:  # город без известного адреса — ищем по всей России с названием города в запросе
        slug, query = "rossiya", f"{query} {city}".strip()
    return f"https://www.avito.ru/{slug}?q={quote_plus(query)}"
