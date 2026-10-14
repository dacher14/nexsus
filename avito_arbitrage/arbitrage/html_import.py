"""Импорт страниц Avito, сохранённых пользователем в браузере (Ctrl+S → .html или .mhtml).

Это ручной способ получения данных: пользователь сам открывает поиск или
объявление в своём браузере и сохраняет страницу. Система Avito не открывает,
ничего не скачивает и защиту не обходит.

Разбор опирается на устойчивые признаки, а не на классы вёрстки:
  * ссылка на объявление вида /<город>/<раздел>/<название>_<ID>;
  * карточка — явный контейнер объявления (data-marker="item", itemscope Product)
    или ограниченный предок ссылки, внутри которого нет других объявлений;
  * цена — микроразметка itemprop="price", элемент цены или отдельная строка «N ₽»
    (старая цена, рассрочка и доставка не учитываются);
  * для страницы объявления — заголовок h1/og:title, собственное описание объявления
    (блоки «похожие объявления» игнорируются).
Вёрстка Avito меняется: если страница разобралась плохо, пришлите её образец
для подстройки. Поля, которых нет, остаются пустыми — ничего не выдумывается.
"""
from __future__ import annotations

import email
import re
from datetime import date, datetime, timedelta
from email import policy
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup, NavigableString, Tag

from .geo import city_from_slug

ITEM_PATH_RE = re.compile(r"^/([a-z0-9_\-]+)/([a-z0-9_\-]+)/([^/?#]*_(\d{6,}))/?$")
PRICE_IN_STRING_RE = re.compile(r"(?<![\d\w])(\d{1,3}(?:[   ]\d{3})+|\d{1,7})\s*₽")
NUMBER_ONLY_RE = re.compile(r"^\d{1,3}(?:[   ]\d{3})*$|^\d{1,7}$")
NOT_PRICE_RE = re.compile(r"old|installment|credit|delivery|discount|monthly", re.I)
SELLER_RE = re.compile(r"/(?:user|brands)/([^/?#]+)")
HTML_SUFFIXES = (".html", ".htm", ".mhtml", ".mht")
MAX_CARD_DEPTH = 8
MAX_CARD_TEXT = 2500

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
MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
          "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}


def _decode(raw: bytes, charset: str | None = None) -> str:
    for enc in ([charset] if charset else []) + ["utf-8", "cp1251"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _read_html(path: Path) -> str:
    raw = path.read_bytes()
    if path.suffix.lower() in (".mhtml", ".mht"):
        msg = email.message_from_bytes(raw, policy=policy.default)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return _decode(part.get_payload(decode=True) or b"", part.get_content_charset())
        raise ValueError("в MHTML нет HTML-части")
    return _decode(raw)


def _item_path(href: str) -> re.Match | None:
    href = re.sub(r"^https?://(?:www\.|m\.)?avito\.ru", "", (href or "").strip())
    href = re.sub(r"[?#].*$", "", href)
    return ITEM_PATH_RE.match(href)


def _text(node: Tag | None) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def _digits(value: Any) -> int | None:
    """Цена из content="23500" / "23500.00" / текста «23 500»."""
    text = str(value or "").replace(" ", " ").replace(" ", " ").strip()
    m = re.fullmatch(r"(\d+)[.,]\d{1,2}", text)
    if m:
        return int(m.group(1))
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def _is_card(tag: Tag) -> bool:
    return tag.get("data-marker") == "item" or tag.has_attr("data-item-id") or (
        tag.has_attr("itemscope") and "Product" in str(tag.get("itemtype") or ""))


def _excluded(string: NavigableString, root: Tag) -> bool:
    """Строка внутри старой цены, рассрочки, доставки — не цена товара."""
    for parent in string.parents:
        if parent is root or parent is None:
            break
        if parent.name in ("s", "del", "strike"):
            return True
        marker = " ".join([str(parent.get("data-marker") or ""), " ".join(parent.get("class") or [])])
        if NOT_PRICE_RE.search(marker):
            return True
    return False


def _price(node: Tag, skip_nested_cards: bool = False) -> int | None:
    if node.get("itemprop") == "price":
        found = _digits(node.get("content")) or _digits(_text(node))
        if found:
            return found
    for meta in node.find_all(attrs={"itemprop": "price"}):
        if skip_nested_cards and meta.find_parent(_is_card) not in (None, node):
            continue
        found = _digits(meta.get("content")) or _digits(_text(meta))
        if found:
            return found
    marker = node.find(attrs={"data-marker": lambda v: bool(v) and "price" in v and not NOT_PRICE_RE.search(v)})
    scope = marker if isinstance(marker, Tag) else node
    strings = [s for s in scope.find_all(string=True) if s.strip() and not _excluded(s, scope)]
    for i, s in enumerate(strings):
        if skip_nested_cards and s.find_parent(_is_card) not in (None, node):
            continue
        text = s.strip()
        if re.search(r"доставк|/\s*мес|в месяц|^от\s", text, re.I):
            continue
        tail = strings[i + 1].strip() if i + 1 < len(strings) else ""
        if re.match(r"^(в месяц|/\s*мес)", tail, re.I):
            continue
        m = PRICE_IN_STRING_RE.search(text)
        if m:
            return _digits(m.group(1))
        if NUMBER_ONLY_RE.match(text) and tail.startswith("₽"):
            return _digits(text)
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
    """Карточка объявления: явный контейнер или ограниченный предок ссылки без других объявлений."""
    explicit = anchor.find_parent(_is_card)
    if explicit is not None and _ids_inside(explicit) == {item_id}:
        return explicit
    card = anchor
    for depth, parent in enumerate(anchor.parents):
        if (not isinstance(parent, Tag) or depth >= MAX_CARD_DEPTH
                or parent.name in ("body", "html", "[document]", "main", "header", "footer", "nav", "form")):
            break
        if _ids_inside(parent) != {item_id}:
            break
        if parent.find(attrs={"data-marker": re.compile("breadcrumb")}) or parent.find(
                attrs={"itemtype": re.compile("BreadcrumbList")}) or len(_text(parent)) > MAX_CARD_TEXT:
            break
        card = parent
    return card


def _seller_id(href: str | None) -> str:
    m = SELLER_RE.search(href or "")
    return m.group(1) if m else ""


def parse_relative_date(text: str, reference: date) -> str:
    """«2 часа назад», «вчера», «12 октября» → ISO-дата относительно даты сохранения страницы."""
    t = (text or "").lower().replace("ё", "е").strip()
    if not t:
        return ""
    if re.search(r"(минут|час|секунд)\w* назад|^сегодня", t):
        return reference.isoformat()
    if t.startswith("вчера"):
        return (reference - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\s+(дн|день|дня|недел)\w*\s+назад", t)
    if m:
        days = int(m.group(1)) * (7 if m.group(2).startswith("недел") else 1)
        return (reference - timedelta(days=days)).isoformat()
    m = re.search(r"(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?", t)
    if m:
        month = next((v for k, v in MONTHS.items() if m.group(2).startswith(k)), None)
        if month:
            year = int(m.group(3)) if m.group(3) else reference.year
            try:
                d = date(year, month, int(m.group(1)))
            except ValueError:
                return ""
            if d > reference and not m.group(3):
                d = d.replace(year=year - 1)
            return d.isoformat()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", t)
    return m.group(1) if m else ""


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


def _card_description(card: Tag, record: dict[str, Any]) -> str:
    desc = card.find(attrs={"itemprop": "description"}) or _marker(card, "item-description", "description")
    if isinstance(desc, Tag):
        return str(desc.get("content") or "") if desc.name == "meta" else _text(desc)
    if card.name == "a":
        return ""  # карточка не определена — описание не угадываем
    # нет разметки — самый длинный фрагмент текста карточки, кроме заголовка, цены, адреса и даты
    known = {record.get("title", ""), record.get("district", ""), record.get("_date_text", "")}
    texts = [re.sub(r"\s+", " ", t).strip() for t in card.stripped_strings]
    texts = [t for t in texts if len(t) >= 20 and t not in known and not PRICE_IN_STRING_RE.fullmatch(t)]
    return max(texts, key=len, default="")


def _search_results(soup: BeautifulSoup, source: str, page_title: str, ref: date) -> list[dict[str, Any]]:
    anchors: dict[str, list[Tag]] = {}
    for a in soup.find_all("a", href=True):
        m = _item_path(a["href"])
        if m:
            anchors.setdefault(m.group(4), []).append(a)
    items = []
    for item_id, links in anchors.items():
        # одно объявление может встречаться в нескольких блоках — берём самую полную карточку
        cards = {id(c): c for c in (_card(a, item_id) for a in links)}.values()
        card = max(cards, key=lambda c: (_price(c) is not None, min(len(_text(c)), MAX_CARD_TEXT)))
        record = _base(_item_path(links[0]["href"]), source, page_title)
        title_node = card.find(attrs={"itemprop": "name"}) or _marker(card, "item-title")
        titles = [a.get("title") or _text(a) for a in links]
        record["title"] = _text(title_node) or max(titles, key=len, default="")
        record["price"] = _price(card)
        address = _marker(card, "item-address", "item-location")
        if address:
            record["district"] = _text(address)
        date_node = _marker(card, "item-date")
        if date_node:
            record["_date_text"] = _text(date_node)
            record["published_at"] = parse_relative_date(record["_date_text"], ref)
        record["description"] = _card_description(card, record)
        record.pop("_date_text", None)
        if not record["title"] or not record["price"]:
            record["_skip"] = "не удалось определить заголовок или цену"
        items.append(record)
    return items


def _outside_cards(node: Tag | None) -> bool:
    return isinstance(node, Tag) and node.find_parent(_is_card) is None


def _single_listing(soup: BeautifulSoup, match: re.Match, source: str, ref: date) -> dict[str, Any]:
    record = _base(match, source, "")
    og = lambda prop: (soup.find("meta", attrs={"property": prop}) or {}).get("content", "")  # noqa: E731
    h1 = soup.find("h1")
    record["title"] = _text(h1) or og("og:title")

    price_box = _marker(soup, "item-view/item-price")
    price = _price(price_box) if price_box else None
    if price is None:
        own = [m for m in soup.find_all(attrs={"itemprop": "price"}) if _outside_cards(m)]
        price = next((p for p in (_price(m) for m in own) if p), None)
    record["price"] = price  # без явной цены объявления запись будет отклонена, а не угадана

    desc = _marker(soup, "item-view/item-description")
    if desc is None:
        desc = next((d for d in soup.find_all(attrs={"itemprop": "description"}) if _outside_cards(d)), None)
    if isinstance(desc, Tag) and desc.name == "meta":
        record["description"] = str(desc.get("content") or "")
    else:
        record["description"] = _text(desc) or og("og:description")

    seller = _marker(soup, "seller-info/name", "seller-link")
    if seller:
        record["seller_name"] = _text(seller)
        link = seller if seller.name == "a" else seller.find("a", href=True)
        record["seller_id"] = _seller_id(link.get("href") if isinstance(link, Tag) else "")
    for li in soup.find_all("li"):
        text = _text(li)
        if text.lower().startswith("состояние"):
            record["condition"] = text.split(":", 1)[-1].strip()
            break
    address = _marker(soup, "item-view/item-address")
    if address:
        record["district"] = _text(address)
    date_node = _marker(soup, "item-view/item-date")
    if date_node:
        record["published_at"] = parse_relative_date(_text(date_node), ref)
    return record


def parse_saved_page(path: str | Path) -> list[dict[str, Any]]:
    """Объявления из сохранённой страницы поиска или страницы одного объявления."""
    path = Path(path)
    soup = BeautifulSoup(_read_html(path), "html.parser")
    ref = datetime.fromtimestamp(path.stat().st_mtime).date()  # когда страница сохранена
    canonical = soup.find("link", rel="canonical")
    og_url = soup.find("meta", attrs={"property": "og:url"})
    for candidate in (canonical.get("href") if canonical else None, og_url.get("content") if og_url else None):
        m = _item_path(candidate or "")
        if m:
            return [_single_listing(soup, m, f"saved-item:{path.name}", ref)]
    return _search_results(soup, f"saved-search:{path.name}", _text(soup.find("title")), ref)


class SavedPageSource:
    def __init__(self, paths: Iterable[str | Path]):
        self.paths = [Path(p) for p in paths]
        self.name = "saved-pages"

    def fetch(self) -> Iterable[dict[str, Any]]:
        for path in self.paths:
            try:
                records = parse_saved_page(path)
            except Exception as exc:  # битый файл не должен останавливать импорт остальных
                yield {"_error": f"{path.name}: не удалось разобрать страницу ({exc})"}
                continue
            for record in records:
                skip = record.pop("_skip", None)
                if skip:
                    yield {"_error": f"{path.name}, объявление {record['listing_id']}: {skip}"}
                    continue
                yield record
