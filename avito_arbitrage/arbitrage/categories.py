"""Справочник категорий (раздел 4): приоритет, ликвидность, габариты, ценность на запчасти."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    code: str
    name: str
    priority: str          # A / B / C
    repair_group: str      # группа ремонтного справочника (repair_kb.yaml)
    liquidity: int         # базовая ликвидность 1–10
    days_to_sell: int      # базовый срок продажи, дней
    size: str              # small / medium / large / bulky — влияет на логистику
    parts_ratio: float     # доля цены исправного товара при продаже на запчасти
    low_season: tuple[int, ...] = ()  # месяцы пониженного спроса


CATEGORIES: dict[str, Category] = {c.code: c for c in [
    Category("smartphone", "Смартфоны", "A", "phone", 9, 4, "small", 0.25),
    Category("tablet", "Планшеты", "A", "phone", 7, 6, "small", 0.25),
    Category("laptop", "Ноутбуки", "A", "laptop", 8, 6, "small", 0.30),
    Category("desktop_pc", "Настольные ПК", "A", "desktop", 7, 8, "medium", 0.45),
    Category("gpu", "Видеокарты", "A", "gpu", 8, 5, "small", 0.20),
    Category("pc_component", "Комплектующие ПК", "A", "desktop", 7, 6, "small", 0.15),
    Category("monitor", "Мониторы", "A", "monitor", 6, 8, "medium", 0.15),
    Category("peripheral", "Компьютерная периферия", "A", "audio", 6, 7, "small", 0.10),
    Category("console", "Игровые приставки", "A", "console", 9, 4, "small", 0.30),
    Category("gamepad", "Контроллеры и VR", "A", "console", 7, 6, "small", 0.20),
    Category("robot_vacuum", "Роботы-пылесосы", "A", "small_appliance", 7, 7, "medium", 0.20),
    Category("vacuum", "Пылесосы", "A", "small_appliance", 6, 8, "medium", 0.15),
    Category("fridge", "Холодильники и морозильники", "A", "large_appliance", 5, 12, "bulky", 0.08),
    Category("washing_machine", "Стиральные машины", "A", "large_appliance", 6, 10, "bulky", 0.10),
    Category("dishwasher", "Посудомоечные машины", "A", "large_appliance", 5, 12, "bulky", 0.10),
    Category("microwave", "Микроволновые печи", "A", "microwave", 6, 7, "medium", 0.05),
    Category("coffee_machine", "Кофемашины", "A", "small_appliance", 6, 9, "medium", 0.15),
    Category("tv", "Телевизоры", "A", "tv", 6, 9, "large", 0.10),
    Category("home_appliance", "Мелкая бытовая техника", "A", "small_appliance", 5, 10, "medium", 0.10),
    Category("camera", "Фото- и видеокамеры", "A", "photo", 6, 10, "small", 0.25),
    Category("lens", "Объективы", "A", "photo", 7, 8, "small", 0.20),
    Category("audio", "Аудиотехника", "A", "audio", 6, 8, "small", 0.15),
    Category("music_gear", "Музыкальное оборудование", "A", "audio", 5, 12, "medium", 0.20),
    Category("power_tool", "Электроинструмент", "B", "tool", 7, 7, "small", 0.20),
    Category("generator", "Генераторы и сварочные аппараты", "B", "tool", 5, 12, "large", 0.20),
    Category("chair", "Игровые и офисные кресла", "B", "furniture", 5, 10, "large", 0.05),
    Category("furniture", "Мебель", "B", "furniture", 4, 14, "large", 0.0),
    Category("sport", "Спортивное оборудование", "B", "other", 5, 12, "medium", 0.05),
    Category("bicycle", "Велосипеды", "B", "other", 6, 9, "large", 0.20, (11, 12, 1, 2)),
    Category("other", "Прочее", "C", "other", 4, 14, "medium", 0.05),
]}

# Порядок важен при равной позиции совпадения: более специфичные правила — раньше.
_INFERENCE_RULES: list[tuple[str, str]] = [
    ("robot_vacuum", r"робот[\s-]*пылесос|пылесос[\s-]*робот|roborock|dreame|ecovacs|irobot|roomba|mi robot|robot vacuum"),
    ("gamepad", r"джойстик|геймпад|gamepad|dualsense|dualshock|joy[\s-]*con|oculus|meta quest|quest [23]|vr[\s-]*шлем|vr очки|ps vr"),
    ("gpu", r"видеокарт|видюх|\b(?:rtx|gtx)\s*\d{3,4}|\brx\s*\d{3,4}\b"),
    ("console", r"playstation|(?<![a-zа-я])(?:ps|пс)\s*[345]\b|плейстейш|xbox|иксбокс|nintendo|switch oled|switch lite|приставк"),
    ("tablet", r"\bipad\b|планшет|galaxy tab|\bmi pad\b|redmi pad|matepad|электронн\w* книг"),
    ("smartphone", r"iphone|айфон|смартфон|телефон|galaxy\s*(?:s|a|m|note|z)\s*\d|galaxy z|redmi(?!\s*pad)|\bpoco\b|honor|realme|oneplus|pixel \d|huawei p\d|nokia|tecno|infinix"),
    ("laptop", r"ноутбук|\bноут\b|macbook|макбук|thinkpad|vivobook|zenbook|ideapad|legion|pavilion|aspire|nitro 5|\btuf\b|rog strix|matebook|ультрабук"),
    ("monitor", r"монитор"),
    ("desktop_pc", r"системн\w* блок|игров\w* пк|игров\w* компьютер|компьютер|\bпк\b|сборк\w* пк"),
    ("pc_component", r"процессор|\bcpu\b|ryzen|core i[3579]|оперативн\w* памят|\bddr[345]\b|\bssd\b|\bhdd\b|жестк\w* диск|материнск\w* плат|блок питания|кулер для"),
    ("peripheral", r"клавиатур|мышь|мышка|веб[\s-]*камер|принтер|роутер"),
    ("fridge", r"холодильник|морозильн"),
    ("washing_machine", r"стиральн\w* машин|стиралк"),
    ("dishwasher", r"посудомо"),
    ("microwave", r"микроволнов|\bсвч\b"),
    ("coffee_machine", r"кофемашин|кофеварк|кофе[\s-]*машин"),
    ("vacuum", r"пылесос|dyson v\d"),
    ("tv", r"телевизор|\bsmart tv\b|\bтв\b"),
    ("home_appliance", r"очистител\w* воздуха|увлажнител|блендер|мультиварк|чайник|тостер|духов\w* шкаф|утюг|фен\b|кухонн\w* комбайн|мясорубк"),
    ("lens", r"объектив"),
    ("camera", r"фотоаппарат|зеркалк|беззеркалк|gopro|экшн[\s-]*камер|видеокамер|\bdji\b"),
    ("music_gear", r"синтезатор|гитар|звуков\w* карт|микшер|комбоусилител|midi"),
    ("audio", r"наушник|airpods|колонк|акустик|микрофон|саундбар|усилител|ресивер|проигрывател"),
    ("power_tool", r"шуруповерт|перфоратор|дрел|болгарк|\bушм\b|лобзик|гайковерт|циркулярн|торцовк|сабельн\w* пил|электропил|бензопил|триммер|makita|dewalt|metabo|интерскол"),
    ("generator", r"генератор|сварочн|инвертор сварочн"),
    ("chair", r"кресл"),
    ("furniture", r"\bстол\b|шкаф|комод|тумб|стеллаж|диван"),
    ("bicycle", r"велосипед|\bвелик\b"),
    ("sport", r"тренажер|гантел|беговая дорожк|штанг|самокат|сноуборд|лыжи"),
]
_COMPILED_RULES = [(code, re.compile(rx)) for code, rx in _INFERENCE_RULES]

# Названия категорий Avito → код справочника (если источник отдаёт категорию).
AVITO_CATEGORY_MAP = {
    "телефоны": "smartphone",
    "мобильные телефоны": "smartphone",
    "планшеты и электронные книги": "tablet",
    "ноутбуки": "laptop",
    "настольные компьютеры": "desktop_pc",
    "мониторы": "monitor",
    "игры, приставки и программы": "console",
    "фототехника": "camera",
    "велосипеды": "bicycle",
    "музыкальные инструменты": "music_gear",
    "товары для компьютера": "pc_component",
    "бытовая техника": "home_appliance",
    "аудио и видео": "audio",
    "ремонт и строительство": "power_tool",
    "мебель и интерьер": "furniture",
    "спорт и отдых": "sport",
}


def normalize_text(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def infer_category(title: str, description: str = "", source_category: str = "") -> str:
    """Категория по ключевым словам: выигрывает совпадение, стоящее раньше в заголовке.

    Явно заданный код справочника имеет приоритет над эвристикой.
    """
    if source_category in CATEGORIES:
        return source_category
    for text in (normalize_text(title), normalize_text(description)):
        best: tuple[int, int, str] | None = None
        for order, (code, rx) in enumerate(_COMPILED_RULES):
            m = rx.search(text)
            if m and (best is None or (m.start(), order) < best[:2]):
                best = (m.start(), order, code)
        if best:
            return best[2]
    return AVITO_CATEGORY_MAP.get(normalize_text(source_category), "other")


def get_category(code: str) -> Category:
    return CATEGORIES.get(code, CATEGORIES["other"])
