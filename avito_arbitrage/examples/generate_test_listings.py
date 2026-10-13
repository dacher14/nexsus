"""Генератор СИНТЕТИЧЕСКИХ объявлений для проверки работоспособности (раздел 23).

Это не данные рынка: цены и описания выдуманы, ссылки ведут на example.invalid,
у каждой записи is_synthetic=true. Набор намеренно содержит дубликаты,
повторные публикации, некорректные записи, блокировки, мошеннические признаки
и неисправности разных классов.

    python examples/generate_test_listings.py --out examples/test_listings.json
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta

# (категория, заголовок, базовая цена исправного, запрос, шаблоны дефектов)
MODELS = [
    ("smartphone", "iPhone 11 64GB", 13000, "iPhone 11", "phone"),
    ("smartphone", "iPhone 12 128GB", 18000, "iPhone 12", "phone"),
    ("smartphone", "iPhone XR 64GB", 9000, "iPhone XR", "phone"),
    ("smartphone", "Samsung Galaxy S21 128GB", 15000, "Samsung Galaxy S21", "phone"),
    ("smartphone", "Samsung Galaxy A52 128GB", 9000, "Samsung Galaxy A52", "phone"),
    ("smartphone", "Redmi Note 11 128GB", 8000, "Redmi Note 11", "phone"),
    ("smartphone", "Poco X3 Pro 128GB", 9500, "Poco X3", "phone"),
    ("laptop", "Ноутбук Lenovo IdeaPad 3 15", 15000, "ноутбук Lenovo", "laptop"),
    ("laptop", "Ноутбук HP Pavilion 15", 18000, "HP Pavilion", "laptop"),
    ("laptop", "Ноутбук Asus VivoBook 15 X515", 16000, "Asus VivoBook", "laptop"),
    ("gpu", "Видеокарта GTX 1660 Super 6GB", 11000, "GTX 1660 Super", "gpu"),
    ("gpu", "Видеокарта RX 580 8GB", 6000, "RX 580", "gpu"),
    ("gpu", "Видеокарта RTX 2060 6GB", 13000, "RTX 2060", "gpu"),
    ("console", "PlayStation 4 Slim 500GB", 14000, "PlayStation 4", "console"),
    ("console", "Xbox Series S", 20000, "Xbox Series S", "console"),
    ("console", "Nintendo Switch Lite", 11000, "Nintendo Switch", "console"),
    ("gamepad", "Геймпад DualSense", 4500, "DualSense", "gamepad"),
    ("robot_vacuum", "Робот-пылесос Roborock S5 Max", 11000, "Roborock S5", "robot"),
    ("robot_vacuum", "Робот-пылесос Xiaomi Mi Robot Vacuum Mop 2", 8000, "Xiaomi Mi Robot Vacuum", "robot"),
    ("power_tool", "Шуруповерт Makita DF333DWAE", 7000, "Makita шуруповерт", "tool"),
    ("power_tool", "Перфоратор Bosch GBH 2-26", 8500, "Bosch перфоратор", "tool"),
    ("tv", "Телевизор Samsung UE43 Smart TV", 14000, "Samsung телевизор 43", "tv"),
    ("monitor", "Монитор AOC 24G2 144Гц", 9000, "монитор 144 Гц", "monitor"),
    ("coffee_machine", "Кофемашина DeLonghi Magnifica S", 15000, "DeLonghi кофемашина", "coffee"),
    ("camera", "Фотоаппарат Canon EOS 2000D kit", 18000, "Canon EOS", "camera"),
    ("washing_machine", "Стиральная машина Indesit IWSB 5085", 9000, "стиральная машина", "washer"),
    ("chair", "Игровое кресло DXRacer", 8000, "игровое кресло", "chair"),
    ("microwave", "Микроволновая печь Samsung ME83", 3500, "микроволновка", "microwave"),
]

DEFECTS = {
    "phone": [("Разбит экран, сенсор работает, Face ID работает. iCloud чистый, выйду из аккаунта.", 0.45),
              ("Не заряжается, похоже гнездо зарядки. В остальном рабочий, iCloud отвязан.", 0.5),
              ("Аккумулятор 74%, быстро садится. Всё остальное работает.", 0.7),
              ("Не включается после падения, причину не знаю.", 0.3),
              ("Попала вода, не включается.", 0.25),
              ("Не работает Face ID, остальное отлично.", 0.55),
              ("Висит на логотипе, bootloop. Аккаунт отвязан.", 0.4)],
    "laptop": [("Не включается, без блока питания.", 0.3),
               ("Не видит диск, нужен SSD. Остальное рабочее.", 0.55),
               ("Сильно греется и шумит вентилятор, выключается от перегрева.", 0.6),
               ("Разбит экран (матрица), в остальном рабочий.", 0.45),
               ("Не работает клавиатура, несколько клавиш западает.", 0.6),
               ("Синий экран при загрузке, нужна переустановка.", 0.55)],
    "gpu": [("Артефакты в играх, в простое работает.", 0.35),
            ("Нет изображения, вентиляторы крутятся.", 0.3),
            ("Сильно греется до 90 градусов, надо менять термопрокладки.", 0.6)],
    "console": [("Не работает HDMI, нет изображения. Аккаунт удалён.", 0.4),
                ("Сильно шумит и греется, давно не чистили.", 0.65),
                ("Не включается, причина неизвестна.", 0.3),
                ("Джойстик дрифтит, сама приставка рабочая.", 0.75)],
    "gamepad": [("Дрифт левого стика, в остальном рабочий.", 0.4),
                ("Не заряжается, разъем зарядки.", 0.35)],
    "robot": [("Не держит заряд, аккумулятор под замену.", 0.45),
              ("Ошибка лидара, не строит карту.", 0.35),
              ("Не возвращается на базу, щетки целые.", 0.45)],
    "tool": [("Аккумулятор не держит заряд, сам шуруповерт рабочий.", 0.45),
             ("Искрит и не крутится, наверное щетки.", 0.35)],
    "tv": [("Нет подсветки, звук есть изображения нет.", 0.3),
           ("Разбита матрица, разбит экран. На запчасти.", 0.15)],
    "monitor": [("Не включается, индикатор не горит.", 0.3),
                ("Полосы на экране.", 0.3)],
    "coffee": [("Течет вода снизу, кофе варит.", 0.45),
               ("Выдает ошибку, мигает индикатор, не тянет воду.", 0.35)],
    "camera": [("Ошибка объектива, объектив не выдвигается.", 0.4),
               ("Не включается после воды.", 0.2)],
    "washer": [("Не сливает воду и не отжимает. 5 этаж без лифта.", 0.3)],
    "chair": [("Сломан газлифт, не держит высоту. Обивка без порывов.", 0.45)],
    "microwave": [("Не греет, свет и тарелка работают.", 0.3)],
}

WORKING = ["Отличное состояние, всё работает. Полный комплект.",
           "Рабочий, мелкие царапины на корпусе. Проверка при встрече.",
           "Состояние хорошее, пользовался аккуратно. Торг уместен.",
           "Всё работает, без нареканий. Продаю в связи с покупкой нового.",
           "Как новый, коробка и чек есть. Без торга."]
DISTRICTS = ["Центральный", "Северный", "Южный", "Западный", "Восточный"]
CITY = "Тестовый город"


def generate(seed: int = 42) -> list[dict]:
    rnd = random.Random(seed)
    today = date(2026, 10, 13)
    items: list[dict] = []
    next_id = 9_100_000_000

    def add(title: str, price: int, desc: str, cat: str, query: str, **extra) -> dict:
        nonlocal next_id
        next_id += rnd.randint(1, 97)
        slug = "_".join(title.lower().split()[:3])
        item = {
            "url": f"https://example.invalid/avito-test/{cat}/{slug}_{next_id}",
            "title": title, "price": price, "description": desc, "city": CITY,
            "district": rnd.choice(DISTRICTS), "seller_id": f"seller_{rnd.randint(10000, 99999)}",
            "published_at": (today - timedelta(days=rnd.randint(0, 20))).isoformat(),
            "photos_count": rnd.choice([0, 3, 5, 8]) if "photos" not in extra else extra.pop("photos"),
            "search_query": query, "is_synthetic": True, "source": "synthetic-test",
        }
        item.update(extra)
        items.append(item)
        return item

    for cat, title, base, query, group in MODELS:
        for _ in range(rnd.randint(6, 10)):
            price = int(rnd.gauss(base, base * 0.1) // 100 * 100)
            add(title, price, rnd.choice(WORKING), cat, query)
        # недооценённые исправные (реалистичный дисконт)
        for _ in range(rnd.randint(1, 2)):
            add(title, int(base * rnd.uniform(0.62, 0.78) // 100 * 100),
                "Рабочий, срочно продаю, переезд. Проверка на месте.", cat, query)
        for desc, frac in rnd.sample(DEFECTS[group], k=min(len(DEFECTS[group]), rnd.randint(2, 4))):
            price = int(base * frac * rnd.uniform(0.85, 1.15) // 100 * 100)
            add(f"{title} неисправен" if rnd.random() < 0.5 else title, price, desc, cat, f"{query} неисправен")
        if rnd.random() < 0.5:
            add(title, int(base * 0.4 // 100 * 100), "Не проверял, достал с антресоли, зарядки нет.", cat,
                f"{query} не проверялся")
        if rnd.random() < 0.35:
            add(f"{title} на запчасти", int(base * 0.2 // 100 * 100), "На запчасти, не включается.", cat,
                f"{query} на запчасти")

    # критические риски и подозрительные цены
    add("iPhone 12 128GB", 6000, "Заблокирован iCloud, пароль не знаю. На запчасти.", "smartphone", "iPhone 12 на запчасти")
    add("iPhone 11 64GB", 5200, "Как новый, всё работает. Только предоплата, отправлю СДЭК, переведите на карту.",
        "smartphone", "iPhone 11")
    add("AirPods Pro", 2500, "Копия 1:1, отличное качество звука.", "audio", "airpods")
    add("Samsung Galaxy S21 128GB", 4000, "Отличное состояние, всё работает, полный комплект.", "smartphone",
        "Samsung Galaxy S21")
    add("Xbox Series S", 9000, "Нашел на улице, работает, без документов.", "console", "Xbox Series S")
    add("Шуруповерт Makita DF333DWAE", 4000, "Пишите в WhatsApp, на звонки не отвечаю. Рабочий.", "power_tool",
        "Makita шуруповерт")

    # дубликаты: то же объявление найдено вторым запросом
    for original in rnd.sample(items, 15):
        dup = dict(original)
        dup["search_query"] = original["search_query"] + " (повтор)"
        items.append(dup)
    # повторные публикации: тот же продавец, тот же заголовок, новый ID
    for original in rnd.sample(items[:200], 6):
        next_id += 13
        repost = dict(original)
        repost["url"] = original["url"].rsplit("_", 1)[0] + f"_{next_id}"
        repost["published_at"] = today.isoformat()
        items.append(repost)
    # некорректные записи (должны быть отклонены при чтении)
    items.append({"title": "Без ссылки", "price": 1000, "is_synthetic": True})
    items.append({"url": "https://example.invalid/avito-test/x/no_price_9199999999", "title": "Без цены",
                  "is_synthetic": True})
    rnd.shuffle(items)
    return items


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="examples/test_listings.json")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    data = generate(args.seed)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"note": "СИНТЕТИЧЕСКИЕ ТЕСТОВЫЕ ДАННЫЕ — не объявления рынка", "listings": data},
                  fh, ensure_ascii=False, indent=1)
    print(f"{len(data)} записей → {args.out}")
