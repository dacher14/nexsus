import pytest

from arbitrage.categories import infer_category
from arbitrage.defects import analyze_defects
from arbitrage.identify import identify_model


@pytest.mark.parametrize("title,category,key", [
    ("iPhone 12 128GB", "smartphone", "smartphone:apple:iphone 12:128gb"),
    ("Айфон 11 Pro Max 256 гб", "smartphone", "smartphone:apple:iphone 11 promax:256gb"),
    ("Samsung Galaxy S21 Ultra 256GB", "smartphone", "smartphone:samsung:galaxy s21 ultra:256gb"),
    ("Redmi Note 11 Pro+ 128GB", "smartphone", "smartphone:xiaomi:redmi note 11 pro+:128gb"),
    ("Джойстик для PS5", "gamepad", None),
    ("PS4 Pro 1TB + 2 джойстика", "console", "console:sony:playstation 4:pro"),
    ("Видеокарта RTX 3060 Ti 8GB", "gpu", "gpu:nvidia:rtx 3060 ti:"),
    ("Робот-пылесос Roborock S5 Max", "robot_vacuum", "robot_vacuum:roborock:s5 max:"),
    ("Пылесос робот Xiaomi", "robot_vacuum", None),
    ("Ноутбук Asus без блока питания", "laptop", None),
    ("Шуруповерт Makita DF333DWAE", "power_tool", "power_tool:makita:df333dwae:"),
])
def test_category_and_model(title, category, key):
    cat = infer_category(title)
    assert cat == category
    if key:
        assert identify_model(title, "", cat).key(cat) == key


def test_explicit_category_code_wins():
    assert infer_category("Что-то непонятное", "", "laptop") == "laptop"


def _codes(title, desc, category="smartphone", **kw):
    d = analyze_defects(title, desc, category, **kw)
    return d, {i.symptom for i in d.items}, {f.code for f in d.red_flags}


def test_negation_is_respected():
    _, symptoms, flags = _codes("iPhone 12", "Экран не разбит, не падал, не ремонтировался. iCloud чистый.")
    assert symptoms == set()
    assert "activation_lock" not in flags


def test_purchase_word_does_not_trigger_drop():
    _, symptoms, _ = _codes("iPhone 12", "Покупал в магазине, всё работает")
    assert "drop" not in symptoms


def test_activation_lock_is_critical():
    d, _, flags = _codes("iPhone 12", "Заблокирован iCloud, пароль не знаю")
    assert "activation_lock" in flags and d.critical_flags


def test_replaced_screen_is_not_replica():
    d, _, flags = _codes("iPhone 11", "Менялся экран, стоит копия дисплея, всё работает")
    assert "replaced_parts" in flags and "replica" not in flags
    assert d.resale_factor < 1


def test_replica_is_critical():
    d, _, flags = _codes("AirPods Pro", "Копия 1:1, отличное качество", "audio")
    assert "replica" in flags and d.critical_flags


def test_battery_health_percent():
    _, symptoms, _ = _codes("iPhone 11", "Аккумулятор 76%, всё остальное отлично")
    assert symptoms == {"battery"}
    _, symptoms, _ = _codes("iPhone 11", "Аккумулятор 91%, всё отлично")
    assert symptoms == set()


def test_specific_symptom_replaces_generic_fault():
    d, symptoms, _ = _codes("Ноутбук HP", "Неисправен: не видит диск", "laptop")
    assert symptoms == {"storage"}
    assert d.defect_class == "A"


def test_cause_absorbs_effects():
    d, symptoms, _ = _codes("Poco X3", "Попала вода, не включается")
    assert symptoms == {"liquid"}
    assert "следствия" in d.items[0].evidence
    assert d.defect_class == "C"


def test_parts_listing_without_symptoms_is_unknown_fault():
    d, symptoms, _ = _codes("PS4 на запчасти", "Продаю на запчасти", "console")
    assert d.is_parts_listing and symptoms == {"unknown_fault"}


def test_untested_flag():
    d, symptoms, _ = _codes("Xbox Series S", "Не проверял, достал с антресоли", "console")
    assert d.is_untested and symptoms == set()


def test_microwave_never_diy():
    d, _, _ = _codes("Микроволновка Samsung", "Не греет", "microwave")
    assert d.items and not any(i.diy_allowed for i in d.items)
    assert "напряжение" in d.items[0].diy_note


def test_skill_gate_for_soldering():
    d, _, _ = _codes("PS4 Slim", "Не работает HDMI", "console", diy_skills=["basic"])
    assert not d.items[0].diy_allowed
    d, _, _ = _codes("PS4 Slim", "Не работает HDMI", "console", diy_skills=["basic", "soldering"])
    assert d.items[0].diy_allowed


def test_costs_scale_with_market_price():
    cheap = analyze_defects("Redmi 9", "Разбит экран", "smartphone", market_price=6000).items[0]
    pricey = analyze_defects("iPhone 13", "Разбит экран", "smartphone", market_price=40000).items[0]
    assert pricey.service_parts[1] > cheap.service_parts[1]
    assert cheap.service_parts[0] >= 1500  # parts_min


def test_extra_symptoms_from_llm():
    d = analyze_defects("Ноутбук", "Какая-то проблема", "laptop",
                        extra_symptoms=[("overheat", "LLM: выключается при нагрузке")])
    assert {i.symptom for i in d.items} == {"overheat"}
