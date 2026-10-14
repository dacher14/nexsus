"""Импорт страниц Avito, сохранённых пользователем в браузере (Ctrl+S → .html или .mhtml).

Это ручной способ получения данных: пользователь сам открывает поиск или
объявление в своём браузере и сохраняет страницу. Система Avito не открывает,
ничего не скачивает и защиту не обходит.

Разбор опирается на устойчивые признаки, а не на классы вёрстки:
  * ссылка на объявление вида /<город>/<раздел>/<название>_<ID>;
  * карточка — наибольший предок ссылки, внутри которого нет других объявлений;
  * цена — микроразметка itemprop="price" или число со знаком ₽;
  * для страницы объявления — заголовок h1/og:title, описание itemprop/og:description.
Вёрстка Avito меняется: если страница разобралась плохо, пришлите её образец
для подстройки (поля, которых нет, остаются пустыми — ничего не выдумывается).
"""
from __future__ import annotations

import email
import re
from email import policy
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup, Tag

from .geo import city_from_slug

ITEM_PATH_RE = re.compile(r"^/([a-z0-9_\-]+)/([a-z0-9_\-]+)/([^/?#]*_(\d{6,}))/?$")
PRICE_TEXT_RE = re.compile(r"(\d[\d\s  ]{0,12})\s*₽")
HTML_SUFFIXES = (".html", ".htm", ".mhtml", ".mht")

# раздел в адресе → название категории Avito (уточняется ключевыми словами в categories.py)
SECTION_NAMES = {
    "telefony": "Телефоны", "noutbuki": "Ноутбуки", "nastolnye_kompyutery": "Настольные компьютеры",
    "planshety_i_elektronnye_knigi": "Планшеты и электронные книги",
    "igry_pristavki_i_programmy": "Игры, приставки и программы", "fototehnika": "Фототехника",
    "velosipedy": "Велосипеды", "muzykalnye_instrumenty": "Музыкальные инструменты",
    "tovary_dlya_kompyutera": "Товары для компьютера", "bytovaya_tehnika": "Бытовая техника",
    "audio_i_video": "Аудио и видео", "remont_i_stroitelstvo": "Ремонт и строительство",
    "mebel_i_interer": "Мебель и интерьер", "sport_i_otdyh": "Спорт и отдых",
}


def _read_html(path: Path) -> str:
    raw = path.read_bytes()
    if path.suffix.lower() in (".mhtml", ".mht"):
        msg = email.message_from_bytes(raw, policy=policy.default)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                raw = part.get_payload(decode=True) or b""
                charset = part.get_content_charset()
                if charset:
                    return raw.decode(charset, errors="replace")
                break
        else:
            raise ValueError(f"{path.name}: в MHTML нет HTML-части")
    for enc in ("utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _item_path(href: str) -> re.Match | None:
    href = re.sub(r"^https?://(?:www\.|m\.)?avito\.ru", "", (href or "").strip())
    href = re.sub(r"[?#].*$", "", href)
    return ITEM_PATH_RE.match(href)


def _text(node: Tag | None) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def _price(node: Tag) -> int | None:
    meta = node.find(attrs={"itemprop": "price"})
    if isinstance(meta, Tag):
        value = meta.get("content") or _text(meta)
        digits = re.sub(r"\D", "", str(value))
        if digits:
            return int(digits)
    m = PRICE_TEXT_RE.search(_text(node))
    if m:
        return int(re.sub(r"\D", "", m.group(1)))
    return None


def _marker(node: Tag, *names: str) -> Tag | None:
    for name in names:
        found = node.find(attrs={"data-marker": re.compile(name)})
        if isinstance(found, Tag):
            return found
    return None


def _ids_inside(node: Tag) -> set[str]:
    ids = set()
    for a in node.find_all("a", href=True):
        m = _item_path(a["href"])
        if m:
            ids.add(m.group(4))
    return ids


def _card(anchor: Tag, item_id: str) -> Tag:
    """Наибольший предок ссылки, содержащий только это объявление."""
    card = anchor
    for parent in anchor.parents:
        if not isinstance(parent, Tag) or parent.name in ("body", "html", "[document]"):
            break
        if _ids_inside(parent) != {item_id}:
            break
        card = parent
    return card


def _base(match: re.Match, source: str, page_title: str) -> dict[str, Any]:
    city_slug, section, path, item_id = match.groups()
    return {
        "listing_id": item_id,
        "url": f"https://www.avito.ru/{city_slug}/{section}/{path}",
        "city": city_from_slug(city_slug),
        "category": SECTION_NAMES.get(section, ""),
        "source": source,
        "search_query": page_title,
    }


def _search_results(soup: BeautifulSoup, source: str, page_title: str) -> list[dict[str, Any]]:
    anchors: dict[str, list[Tag]] = {}
    for a in soup.find_all("a", href=True):
        m = _item_path(a["href"])
        if m:
            anchors.setdefault(m.group(4), []).append(a)
    items = []
    for item_id, links in anchors.items():
        card = _card(links[0], item_id)
        record = _base(_item_path(links[0]["href"]), source, page_title)
        title_node = card.find(attrs={"itemprop": "name"}) or _marker(card, "item-title")
        titles = [a.get("title") or _text(a) for a in links]
        record["title"] = _text(title_node) or max(titles, key=len, default="")
        record["price"] = _price(card)
        address = _marker(card, "item-address", "item-location")
        if address:
            record["district"] = _text(address)
        date = _marker(card, "item-date")
        if date:
            record["published_at"] = _text(date)
        record["description"] = _card_description(card, record)
        if not record["title"] or not record["price"]:
            record["_skip"] = "не удалось определить заголовок или цену"
        items.append(record)
    return items


def _card_description(card: Tag, record: dict[str, Any]) -> str:
    desc = card.find(attrs={"itemprop": "description"}) or _marker(card, "item-description", "description")
    if isinstance(desc, Tag):
        return str(desc.get("content") or "") if desc.name == "meta" else _text(desc)
    # нет разметки — самый длинный фрагмент текста карточки, кроме заголовка, цены, адреса и даты
    known = {record.get("title", ""), record.get("district", ""), record.get("published_at", "")}
    texts = [re.sub(r"\s+", " ", t).strip() for t in card.stripped_strings]
    texts = [t for t in texts if len(t) >= 20 and t not in known and not PRICE_TEXT_RE.fullmatch(t)]
    return max(texts, key=len, default="")


def _single_listing(soup: BeautifulSoup, match: re.Match, source: str) -> dict[str, Any]:
    record = _base(match, source, "")
    og = lambda prop: (soup.find("meta", attrs={"property": prop}) or {}).get("content", "")  # noqa: E731
    h1 = soup.find("h1")
    record["title"] = _text(h1) or og("og:title")
    price_box = _marker(soup, "item-view/item-price", "item-price")
    record["price"] = _price(price_box) if price_box else _price(soup)
    desc = soup.find(attrs={"itemprop": "description"}) or _marker(soup, "item-view/item-description")
    record["description"] = _text(desc) or og("og:description")
    seller = _marker(soup, "seller-info/name", "seller-link")
    if seller:
        record["seller_name"] = _text(seller)
        href = seller.get("href") if seller.name == "a" else (seller.find("a", href=True) or {}).get("href")
        if href:
            record["seller_id"] = re.sub(r"[?#].*$", "", str(href)).rstrip("/").split("/")[-1]
    for li in soup.find_all("li"):
        text = _text(li)
        if text.lower().startswith("состояние"):
            record["condition"] = text.split(":", 1)[-1].strip()
            break
    address = _marker(soup, "item-view/item-address", "item-address")
    if address:
        record["district"] = _text(address)
    date = _marker(soup, "item-view/item-date", "item-date")
    if date:
        record["published_at"] = _text(date)
    return record


def parse_saved_page(path: str | Path) -> list[dict[str, Any]]:
    """Объявления из сохранённой страницы поиска или страницы одного объявления."""
    path = Path(path)
    soup = BeautifulSoup(_read_html(path), "html.parser")
    source = f"saved-page:{path.name}"
    page_title = _text(soup.find("title"))
    canonical = soup.find("link", rel="canonical")
    og_url = soup.find("meta", attrs={"property": "og:url"})
    for candidate in (canonical.get("href") if canonical else None, og_url.get("content") if og_url else None):
        m = _item_path(candidate or "")
        if m:
            return [_single_listing(soup, m, source)]
    return _search_results(soup, source, page_title)


class SavedPageSource:
    def __init__(self, paths: Iterable[str | Path]):
        self.paths = [Path(p) for p in paths]
        self.name = "saved-pages"

    def fetch(self) -> Iterable[dict[str, Any]]:
        for path in self.paths:
            for record in parse_saved_page(path):
                if record.pop("_skip", None):
                    record["price"] = None  # будет отклонено валидацией с понятной причиной
                yield record
