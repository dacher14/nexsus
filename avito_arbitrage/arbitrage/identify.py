"""Идентификация бренда и модели — дешёвая эвристика без LLM (раздел 23).

Результат — ключ модели вида «категория:бренд:модель:вариант» для сравнения
с аналогами. Неоднозначные объявления помечаются низкой уверенностью и
могут быть переданы в LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .categories import normalize_text

BRAND_ALIASES = {
    "apple": ["apple", "iphone", "айфон", "ipad", "macbook", "макбук", "airpods", "imac"],
    "samsung": ["samsung", "самсунг", "galaxy"],
    "xiaomi": ["xiaomi", "сяоми", "ксиоми", "redmi", "poco", "mi robot", "mijia"],
    "huawei": ["huawei", "хуавей", "matebook", "matepad"],
    "honor": ["honor", "хонор"],
    "realme": ["realme"], "oneplus": ["oneplus"], "google": ["google pixel", "pixel"],
    "nokia": ["nokia"], "tecno": ["tecno"], "infinix": ["infinix"], "motorola": ["motorola"],
    "asus": ["asus", "асус", "rog", "vivobook", "zenbook"], "acer": ["acer", "асер", "aspire", "nitro"],
    "lenovo": ["lenovo", "леново", "thinkpad", "ideapad", "legion"], "hp": ["hp", "pavilion", "omen"],
    "dell": ["dell", "дэлл", "inspiron", "latitude", "alienware"], "msi": ["msi"],
    "gigabyte": ["gigabyte"], "palit": ["palit"], "zotac": ["zotac"], "sapphire": ["sapphire"],
    "sony": ["sony", "сони", "playstation", "плейстейшн", "плейстейшен", "dualsense", "dualshock"],
    "microsoft": ["microsoft", "xbox", "иксбокс"], "nintendo": ["nintendo", "нинтендо", "switch"],
    "roborock": ["roborock"], "dreame": ["dreame"], "ecovacs": ["ecovacs"], "irobot": ["irobot", "roomba"],
    "dyson": ["dyson", "дайсон"], "philips": ["philips", "филипс"], "delonghi": ["delonghi", "де лонги"],
    "bosch": ["bosch", "бош"], "makita": ["makita", "макита"], "dewalt": ["dewalt"], "metabo": ["metabo"],
    "interskol": ["интерскол"], "lg": ["lg"], "indesit": ["indesit", "индезит"],
    "atlant": ["атлант", "atlant"], "beko": ["beko"], "haier": ["haier"], "candy": ["candy"],
    "electrolux": ["electrolux"], "canon": ["canon", "кенон"], "nikon": ["nikon", "никон"],
    "fujifilm": ["fujifilm", "fuji"], "gopro": ["gopro"], "dji": ["dji"], "jbl": ["jbl"],
    "marshall": ["marshall"], "sennheiser": ["sennheiser"], "logitech": ["logitech"],
    "razer": ["razer"], "hyperx": ["hyperx"], "aoc": ["aoc"], "benq": ["benq"],
    "nvidia": ["nvidia"], "amd": ["amd", "ryzen", "radeon"], "intel": ["intel", "core i"],
    "kingston": ["kingston"], "crucial": ["crucial"], "seagate": ["seagate"], "wd": ["western digital", "wd"],
}
_BRAND_RES = [(brand, re.compile(r"(?<![a-zа-я0-9])(?:" + "|".join(map(re.escape, aliases)) + r")(?![a-zа-я])"))
              for brand, aliases in BRAND_ALIASES.items()]

_STORAGE_RE = re.compile(r"(?<!\d)(16|32|64|128|256|512)\s*(?:gb|гб|g)(?![a-zа-я])|(?<!\d)([12])\s*(?:tb|тб)(?![a-zа-я])")


@dataclass
class ModelId:
    brand: str
    model: str
    variant: str
    confidence: str  # высокая / средняя / низкая

    def key(self, category: str) -> str:
        return ":".join([category, self.brand or "?", self.model or "?", self.variant or ""])

    def family_key(self, category: str) -> str:
        return ":".join([category, self.brand or "?", self.model or "?", ""])


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def detect_brand(text: str) -> str:
    best: tuple[int, str] | None = None
    for brand, rx in _BRAND_RES:
        m = rx.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), brand)
    return best[1] if best else ""


def detect_storage(text: str) -> str:
    m = _STORAGE_RE.search(text)
    if not m:
        return ""
    return f"{m.group(1)}gb" if m.group(1) else f"{m.group(2)}tb"


def _iphone(m: re.Match) -> tuple[str, str]:
    gen = m.group(1).replace(" ", "")
    suffix = _clean(m.group(2)).replace(" ", "")
    return "iphone " + gen + (" " + suffix if suffix else ""), ""


_MODEL_RULES: list[tuple[str, str, re.Pattern, object]] = [
    ("apple", "smartphone", re.compile(r"(?:iphone|айфон)\s*(1[0-7]|[6-9]|se|xs|xr|x)(?:\s*(pro\s*max|pro|plus|mini|max|e)\b)?"), _iphone),
    ("apple", "tablet", re.compile(r"ipad\s*(pro|air|mini)?\s*(\d{1,2})?"),
     lambda m: ("ipad " + _clean(f"{m.group(1) or ''} {m.group(2) or ''}"), "")),
    ("apple", "laptop", re.compile(r"(?:macbook|макбук)\s*(air|pro)?.*?\b(m[1-5](?:\s*(?:pro|max))?|20[12]\d)\b"),
     lambda m: ("macbook " + _clean(f"{m.group(1) or ''} {m.group(2)}"), "")),
    ("samsung", "tablet", re.compile(r"galaxy\s*tab\s*(s|a)?\s*(\d{1,2})?\s*(fe|lite|plus|\+|ultra)?"),
     lambda m: ("galaxy tab " + _clean(f"{m.group(1) or ''}{m.group(2) or ''} {m.group(3) or ''}"), "")),
    ("samsung", "smartphone", re.compile(r"galaxy\s*(s|a|m|note|z\s*fold|z\s*flip)\s*(\d{1,2})\s*(ultra|plus|\+|fe|lite|edge)?"),
     lambda m: ("galaxy " + _clean(f"{m.group(1).replace(' ', '')}{m.group(2)} {m.group(3) or ''}"), "")),
    ("xiaomi", "smartphone", re.compile(r"(redmi\s*note|redmi|poco)\s*([a-z]?\d{1,2}[a-z]?)\s*(pro\s*\+|pro\+|pro|ultra|lite|s|t|c)?(?![a-zа-я0-9])"),
     lambda m: (_clean(f"{m.group(1)} {m.group(2)} {m.group(3) or ''}").replace(" +", "+"), "")),
    ("sony", "console", re.compile(r"(?:playstation|(?<![a-zа-я])ps|(?<![a-zа-я])пс|плейстейшн|плейстейшен)\s*([345])\b(?:\s*(slim|pro|fat|digital))?"),
     lambda m: (f"playstation {m.group(1)}", m.group(2) or "")),
    ("microsoft", "console", re.compile(r"xbox\s*(series\s*[sx]|one\s*[sx]|one|360)"),
     lambda m: ("xbox " + _clean(m.group(1)), "")),
    ("nintendo", "console", re.compile(r"(?:nintendo\s*)?switch(?:\s*(oled|lite|2))?"),
     lambda m: ("switch", m.group(1) or "")),
    ("", "gpu", re.compile(r"\b(rtx|gtx|rx)\s*(\d{3,4})\s*(ti\s*super|ti|super|xtx|xt|gre)?\b"),
     lambda m: (_clean(f"{m.group(1)} {m.group(2)} {m.group(3) or ''}"), "")),
    ("intel", "pc_component", re.compile(r"\b(?:core\s*)?(i[3579])[\s-]*(\d{4,5}[a-z]{0,2})\b"),
     lambda m: (f"{m.group(1)}-{m.group(2)}", "")),
    ("amd", "pc_component", re.compile(r"ryzen\s*([3579])\s*(\d{4}[a-z0-9]{0,3})"),
     lambda m: (f"ryzen {m.group(1)} {m.group(2)}", "")),
]

_GENERIC_TOKEN = re.compile(r"[a-zа-я0-9][a-zа-я0-9+\-]*")
_STOPWORDS = {"для", "и", "в", "на", "с", "б/у", "бу", "новый", "новая", "продам", "продаю",
              "срочно", "отличное", "состояние", "рабочий", "рабочая", "неисправный", "неисправен",
              "игровой", "игровая", "игровое", "комплект", "оригинал", "оригинальный", "торг"}
_PRODUCT_LINES = {"iphone", "ipad", "macbook", "airpods", "imac", "galaxy", "redmi", "poco", "playstation",
                  "dualsense", "dualshock", "xbox", "switch", "rog", "vivobook", "zenbook", "thinkpad", "ideapad",
                  "legion", "pavilion", "omen", "inspiron", "latitude", "alienware", "aspire", "nitro",
                  "matebook", "matepad", "roomba"}
_VARIANT_WORDS = {"pro", "max", "plus", "ultra", "lite", "mini", "se", "slim", "air", "fe", "neo"}


def identify_model(title: str, description: str = "", category: str = "",
                   declared_brand: str = "", declared_model: str = "") -> ModelId:
    text = normalize_text(title)
    full = normalize_text(f"{title} {description}")
    storage = detect_storage(text) or detect_storage(full)

    for brand, rule_cat, rx, build in _MODEL_RULES:
        if category and rule_cat != category and not (rule_cat == "pc_component" and category == "desktop_pc"):
            continue
        m = rx.search(text) or rx.search(full)
        if m:
            model, variant = build(m)  # type: ignore[operator]
            if rule_cat in ("smartphone", "tablet"):
                variant = storage or variant
            detected = brand or detect_brand(full)
            if rule_cat == "gpu" and detected in ("", "amd", "nvidia"):
                detected = "amd" if model.startswith("rx") else "nvidia"
            return ModelId(detected, model, variant,
                           "высокая" if (rule_cat not in ("smartphone", "tablet") or storage) else "средняя")

    brand = normalize_text(declared_brand) or detect_brand(text) or detect_brand(full)
    if declared_model:
        return ModelId(brand, normalize_text(declared_model), storage, "средняя")

    tokens = [t for t in _GENERIC_TOKEN.findall(text) if t not in _STOPWORDS]
    if brand:
        aliases = BRAND_ALIASES.get(brand, [brand])
        idx = next((i for i, t in enumerate(tokens) if any(t == a or t.startswith(a) for a in aliases)), None)
        tail = tokens[idx + 1: idx + 7] if idx is not None else tokens[:6]
        # алиас-линейка (airpods, thinkpad, dualsense) — часть названия модели
        model_tokens = [tokens[idx]] if idx is not None and tokens[idx] in _PRODUCT_LINES else []
        for n, t in enumerate(tail):
            model_tokens.append(t)
            if re.search(r"\d", t):
                if n + 1 < len(tail) and tail[n + 1] in _VARIANT_WORDS:
                    model_tokens.append(tail[n + 1])
                return ModelId(brand, " ".join(model_tokens), storage, "средняя")
        if model_tokens and model_tokens[0] in _PRODUCT_LINES:
            return ModelId(brand, " ".join(model_tokens[:3]), storage, "средняя")
        return ModelId(brand, " ".join(tail[:2]), storage, "низкая")
    return ModelId("", " ".join(tokens[:3]), storage, "низкая")
