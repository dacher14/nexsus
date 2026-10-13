"""План многопроходного поиска (раздел 5).

Генерирует поисковые запросы для разрешённого источника объявлений или для
ручного поиска. Запрос сохраняется в поле search_query объявления — по нему
считается эффективность запросов на листе MARKET INTELLIGENCE.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .categories import CATEGORIES

# Модели с высокой ликвидностью — отправная точка; список расширяется по статистике.
SEED_MODELS: dict[str, list[str]] = {
    "smartphone": ["iPhone 11", "iPhone 12", "iPhone 13", "iPhone XR", "Samsung Galaxy S21", "Samsung Galaxy A52",
                   "Redmi Note 10", "Redmi Note 11", "Poco X3", "Poco F3"],
    "laptop": ["MacBook Air M1", "Lenovo Legion", "Asus TUF Gaming", "Acer Nitro 5", "HP Pavilion", "Lenovo ThinkPad"],
    "gpu": ["RTX 3060", "RTX 2060", "GTX 1660 Super", "RX 6600", "RX 580"],
    "console": ["PlayStation 4", "PlayStation 5", "Xbox Series S", "Nintendo Switch"],
    "gamepad": ["DualSense", "DualShock 4", "Joy-Con"],
    "robot_vacuum": ["Roborock S5", "Xiaomi Mi Robot Vacuum", "Dreame"],
    "power_tool": ["Makita шуруповерт", "Bosch перфоратор", "DeWalt шуруповерт"],
    "tv": ["Samsung телевизор 43", "LG телевизор 50"],
    "monitor": ["монитор 27 IPS", "монитор 144 Гц"],
    "camera": ["Canon EOS", "Nikon D", "Sony Alpha"],
    "coffee_machine": ["DeLonghi кофемашина", "Philips кофемашина"],
}

SYMPTOM_QUERIES = ["не включается", "не заряжается", "не загружается", "разбит экран", "нет изображения",
                   "не видит диск", "не держит заряд", "перегревается", "перезагружается", "нужна прошивка",
                   "на запчасти", "под восстановление", "требует ремонта", "продаю срочно", "без блока питания",
                   "не проверялся", "после падения", "неисправен"]

TYPO_QUERIES = ["айфон", "айфн", "iphon", "самсунк", "ксиоми", "сяоми", "плейстейшн", "плестейшн", "иксбокс",
                "ноут игровой", "видюха", "шуруповерт", "робот пылесос", "кофемашинка"]


@dataclass
class Query:
    pass_no: int
    pass_name: str
    category: str
    query: str


def build_plan(categories: list[str] | None = None, symptoms_per_model: int = 6) -> list[Query]:
    cats = categories or [c for c in CATEGORIES if CATEGORIES[c].priority in ("A", "B")]
    plan: list[Query] = []
    for cat in cats:
        plan.append(Query(1, "Широкий сбор", cat, CATEGORIES[cat].name.lower()))
        for model in SEED_MODELS.get(cat, []):
            plan.append(Query(1, "Широкий сбор", cat, model))
            plan.append(Query(2, "Недооценённые (сортировка по цене)", cat, model))
            for symptom in SYMPTOM_QUERIES[:symptoms_per_model]:
                plan.append(Query(4, "Поиск по симптомам", cat, f"{model} {symptom}"))
        plan.append(Query(3, "Неисправные", cat, f"{CATEGORIES[cat].name.lower()} на запчасти"))
    for typo in TYPO_QUERIES:
        plan.append(Query(6, "Неточные объявления", "", typo))
    return plan


def write_plan(plan: list[Query], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Проход", "Название прохода", "Категория", "Запрос"])
        for q in plan:
            w.writerow([q.pass_no, q.pass_name, q.category, q.query])
    return path
