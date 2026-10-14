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

# Названия для показа самых частых городов по slug из адреса Avito.
CITY_SLUGS = {
    "tyumen": "Тюмень", "yalutorovsk": "Ялуторовск", "zavodoukovsk": "Заводоуковск",
    "tobolsk": "Тобольск", "ishim": "Ишим", "kurgan": "Курган", "shadrinsk": "Шадринск",
    "ekaterinburg": "Екатеринбург", "kamensk-uralskiy": "Каменск-Уральский", "chelyabinsk": "Челябинск",
    "omsk": "Омск", "surgut": "Сургут", "hanty-mansiysk": "Ханты-Мансийск", "nizhnevartovsk": "Нижневартовск",
    "novosibirsk": "Новосибирск", "perm": "Пермь", "moskva": "Москва", "sankt-peterburg": "Санкт-Петербург",
    "kazan": "Казань", "ufa": "Уфа", "samara": "Самара", "rossiya": "",
}
_TRANSLIT = dict(zip("абвгдезийклмнопрстуфхыэ", "abvgdeziyklmnoprstufhye")) | {
    "ж": "zh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ю": "yu", "я": "ya", "ъ": "", "ь": "", "ё": "e"}


def normalize_city(name: str | None) -> str:
    name = (name or "").strip().lower().replace("ё", "е")
    name = re.sub(r"^(?:г\.|город)\s*", "", name)
    return name.split(",")[0].strip()


def city_key(name: str | None) -> str:
    """Ключ для сравнения городов: «Тюмень», «г. тюмень» и slug «tyumen» совпадают."""
    norm = normalize_city(name)
    return re.sub(r"\s+", "_", "".join(_TRANSLIT.get(ch, ch) for ch in norm))


def same_city(a: str | None, b: str | None) -> bool:
    return bool(city_key(a)) and city_key(a) == city_key(b)


def city_from_slug(slug: str) -> str:
    """Город из адреса объявления. Неизвестный slug остаётся как есть — он всё равно
    сравнивается с настройками через city_key (tavda == Тавда)."""
    return CITY_SLUGS.get(slug, slug)


def logistics_for(city: str, size: str, settings: Settings) -> Logistics:
    if not settings.region:
        return Logistics("local", note="домашний город не задан — логистика не учитывается")
    where = city_key(city)
    if not where:
        return Logistics("unknown", note="город в объявлении не указан — уточнить у продавца")
    distances = {city_key(k): (k, v) for k, v in (settings.pickup_distances_km or {}).items()}
    if where == city_key(settings.region):
        return Logistics("local", city=settings.region)
    display, km = distances.get(where, (re.sub(r"^(?:г\.|город)\s*", "", city.strip()), None))
    city = display
    delivery = (settings.delivery_cost or {}).get(size)
    if km is not None and km <= settings.max_pickup_km:
        travel = round(2 * km * settings.travel_cost_per_km)
        loading = settings.transport_cost.get(size, 0) if size in ("large", "bulky") else 0
        cost = travel + loading
        # поездка выгоднее, если дешевле доставки с учётом ценности осмотра до оплаты
        if delivery is None or cost <= delivery + settings.inspection_value:
            extra = f" + погрузка {loading} ₽" if loading else ""
            return Logistics("pickup", city=city, extra_cost=cost, extra_days=1 if km > 150 else 0,
                             note=f"самовывоз: {city}, ~{km} км в одну сторону, дорога ≈{travel} ₽{extra}")
    if delivery is None:
        return Logistics("unavailable", city=city,
                         note=f"крупногабарит в другом городе ({city}) вне зоны самовывоза — доставка нецелесообразна")
    return Logistics("delivery", city=city, extra_cost=int(delivery), extra_days=settings.delivery_days,
                     inspect_before_payment=False,
                     note=f"доставка, город продавца — {city}: ≈{int(delivery)} ₽, +{settings.delivery_days} дн.; "
                          f"осмотр только при получении")


def search_url(city: str, query: str) -> str:
    """Ссылка для ручного поиска в браузере (агент сам Avito не открывает)."""
    slug = city_key(city) or "rossiya"
    if city_key(city) in ("rossiya", "россия", "vsya_rossiya"):
        slug = "rossiya"
    return f"https://www.avito.ru/{slug}?q={quote_plus(query)}"
