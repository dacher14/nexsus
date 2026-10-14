import dataclasses
import json
import quopri
from pathlib import Path

import pytest

from arbitrage.analyzer import Analyzer
from arbitrage.config import load_settings
from arbitrage.geo import logistics_for, search_url
from arbitrage.html_import import parse_saved_page
from arbitrage.negotiation import first_message
from arbitrage.search_plan import build_plan
from arbitrage.sources import load_listings, sources_for
from conftest import make_listing

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def tyumen():
    return load_settings(ROOT / "config.example.yaml")


# --- логистика ---

def test_logistics_modes(tyumen):
    assert logistics_for("Тюмень", "small", tyumen).mode == "local"
    near = logistics_for("г. Ялуторовск", "small", tyumen)
    assert near.mode == "pickup" and near.extra_cost == round(2 * 75 * 7) and near.inspect_before_payment
    far = logistics_for("Москва", "small", tyumen)
    assert far.mode == "delivery" and not far.inspect_before_payment and far.extra_days == tyumen.delivery_days
    assert logistics_for("Москва", "bulky", tyumen).mode == "unavailable"
    assert logistics_for("", "small", tyumen).mode == "unknown"


def test_pickup_vs_delivery_choice(tyumen):
    # смартфон из Екатеринбурга: поездка 4620 ₽ дороже доставки 450 + 1500 за осмотр
    assert logistics_for("Екатеринбург", "small", tyumen).mode == "delivery"
    # стиральную машину оттуда не доставляют — едем сами
    assert logistics_for("Екатеринбург", "bulky", tyumen).mode == "pickup"


def test_no_region_means_no_geography(settings):
    assert logistics_for("Москва", "small", settings).mode == "local"


def _analyze(settings, target, comps):
    res = Analyzer(settings).analyze_all(comps + [target])
    return next(a for a in res if a.listing_id == target.listing_id)


def _comps(prices, city="Тюмень", title="iPhone 12 128GB"):
    return [make_listing(title, p, "Всё работает", city=city) for p in prices]


def test_remote_purchase_costs_more_and_is_riskier(tyumen):
    comps = _comps([16000, 17000, 15500, 16500, 18000, 16000, 15000, 17500])
    local = _analyze(tyumen, make_listing("iPhone 12 128GB", 9500, "Всё работает", city="Тюмень"), comps)
    remote = _analyze(tyumen, make_listing("iPhone 12 128GB", 9500, "Всё работает", city="Москва"), comps)
    lc, rc = local.best.scenarios["base"], remote.best.scenarios["base"]
    assert rc.costs.transport > lc.costs.transport
    assert rc.days_held > lc.days_held
    assert remote.risk > local.risk
    assert any("доставк" in c.lower() for c in remote.checks)
    assert "Авито Доставку" in first_message(remote, tyumen, "iPhone 12")
    assert "подъехать" in first_message(local, tyumen, "iPhone 12")


def test_remote_bulky_is_skipped(tyumen):
    comps = _comps([9000, 9500, 8800, 9200, 9100], title="Стиральная машина Indesit IWSB 5085")
    a = _analyze(tyumen, make_listing("Стиральная машина Indesit IWSB 5085", 2500, "Всё работает", city="Москва"),
                 comps)
    assert a.recommendation.startswith("ПРОПУСТИТЬ") and "крупногабарит" in a.recommendation
    assert a.best is None


def test_home_city_comps_preferred(tyumen):
    home = _comps([16000, 16500, 15500, 16200, 15800])
    moscow = _comps([25000, 26000, 24500, 25500, 26500, 25200], city="Москва")
    a = _analyze(tyumen, make_listing("iPhone 12 128GB", 9500, "Всё работает", city="Тюмень"), home + moscow)
    assert a.market.working_median < 17000 and "Тюмень" in a.market.basis

    few_home = _comps([16000, 16500])
    b = _analyze(tyumen, make_listing("iPhone 12 128GB", 9500, "Всё работает", city="Тюмень"), few_home + moscow)
    assert "других городов" in b.market.basis
    assert b.market.confidence in ("средняя", "низкая")


# --- сохранённые страницы ---

def test_parse_search_page():
    records = parse_saved_page(FIXTURES / "search_page.html")
    by_id = {r["listing_id"]: r for r in records}
    assert set(by_id) == {"4501234567", "4507654321", "4509999999"}
    first = by_id["4501234567"]
    assert first["url"] == "https://www.avito.ru/tyumen/telefony/iphone_12_128gb_4501234567"
    assert first["title"] == "iPhone 12, 128 ГБ" and first["price"] == 23500
    assert first["city"] == "Тюмень" and first["category"] == "Телефоны"
    assert "АКБ 86%" in first["description"]
    assert first["search_query"] == "Купить iPhone 12 в Тюмени | Авито"
    second = by_id["4507654321"]
    assert second["price"] == 8900 and second["city"] == "Екатеринбург"
    assert "падения" in second["description"]
    assert by_id["4509999999"].get("_skip")


def test_saved_pages_validation(tmp_path):
    listings, errors = load_listings(sources_for([FIXTURES / "search_page.html"]))
    assert len(listings) == 2 and len(errors) == 1


def test_parse_listing_page_ignores_similar_items():
    (record,) = parse_saved_page(FIXTURES / "listing_page.html")
    assert record["listing_id"] == "4512345678"
    assert record["title"] == "Ноутбук Lenovo IdeaPad 3 15" and record["price"] == 9000
    assert record["city"] == "Ялуторовск" and record["condition"] == "Б/у"
    assert "перегрева" in record["description"]
    assert record["seller_id"] == "i123456789"


def test_mhtml_and_directory_import(tmp_path):
    html = (FIXTURES / "search_page.html").read_bytes()
    mhtml = (b"From: <Saved by Blink>\r\nMIME-Version: 1.0\r\n"
             b'Content-Type: multipart/related; type="text/html"; boundary="B"\r\n\r\n'
             b"--B\r\nContent-Type: text/html\r\nContent-Transfer-Encoding: quoted-printable\r\n"
             b"Content-Location: https://www.avito.ru/tyumen?q=iphone\r\n\r\n"
             + quopri.encodestring(html) + b"\r\n--B--\r\n")
    folder = tmp_path / "pages"
    folder.mkdir()
    (folder / "search.mhtml").write_bytes(mhtml)
    (folder / "extra.json").write_text(json.dumps([{"url": "https://example.invalid/x_1234567", "title": "t",
                                                    "price": 100}]), encoding="utf-8")
    (folder / "notes.txt").write_text("не объявления", encoding="utf-8")
    listings, errors = load_listings(sources_for([folder]))
    assert {l.listing_id for l in listings} == {"4501234567", "4507654321", "1234567"}
    assert len(errors) == 1


def test_campaign_from_saved_pages(storage, tyumen, tmp_path):
    from arbitrage.pipeline import run_campaign
    tyumen = dataclasses.replace(tyumen, database_url=str(storage.engine.url))
    result = run_campaign(storage, tyumen, [FIXTURES], out_path=tmp_path / "out.xlsx")
    # 2 объявления из поиска (третье без цены отклонено) + страница объявления; «похожие» не берутся
    assert result.unique == 3 and result.invalid == 1
    remote = next(a for a in result.analyses if a.listing_id == "4507654321")
    assert remote.logistics.mode == "delivery"
    laptop = next(a for a in result.analyses if a.listing_id == "4512345678")
    assert laptop.logistics.mode == "pickup"
    assert {i.symptom for i in laptop.defects.items} >= {"overheat", "missing_accessory"}


# --- план поиска ---

def test_search_urls():
    assert search_url("Тюмень", "iPhone 12") == "https://www.avito.ru/tyumen?q=iPhone+12"
    assert search_url("", "ps4").startswith("https://www.avito.ru/rossiya?q=ps4")
    assert search_url("г. тюмень", "ps4") == "https://www.avito.ru/tyumen?q=ps4"
    assert search_url("Нижний Тагил", "ps4") == "https://www.avito.ru/nizhniy_tagil?q=ps4"
    assert search_url("Россия", "ps4") == "https://www.avito.ru/rossiya?q=ps4"


def test_search_plan_cities():
    plan = build_plan(["smartphone"], ["Тюмень", "Курган", ""], home="Тюмень")
    cities = {q.city for q in plan}
    assert cities == {"Тюмень", "Курган", ""}
    deep = {q.city for q in plan if q.pass_no == 4}
    assert deep == {"Тюмень", ""}


# --- регрессии по итогам ревью ---

def test_home_variant_comps_do_not_displace_exact_model(tyumen):
    home_256 = [make_listing("iPhone 12 256GB", p, "Всё работает", city="Тюмень") for p in (21800, 22000, 22300, 22500)]
    far_64 = [make_listing("iPhone 12 64GB", p, "Всё работает", city=c)
              for p, c in zip((11700, 11900, 12000, 12100, 12200, 12300, 12400, 12000, 11800, 12100),
                              ["Москва", "Екатеринбург", "Курган", "Омск"] * 3)]
    a = _analyze(tyumen, make_listing("iPhone 12 64GB", 13000, "Всё работает", city="Тюмень"), home_256 + far_64)
    assert a.market.match_level == "model" and a.market.working_median < 13000
    assert "других городов" in a.market.basis and a.recommendation.startswith("ПРОПУСТИТЬ")


def test_variantless_title_uses_family_comps(tyumen):
    comps = _comps([16000, 17000, 15500, 16500, 18000])
    a = _analyze(tyumen, make_listing("iPhone 12 не включается", 3000, "Не включается", city="Тюмень"), comps)
    assert a.market.comps_count == 5 and a.market.match_level == "family"


def test_pickup_choice_counts_loading_for_large_items(tyumen):
    lg = logistics_for("Тобольск", "large", tyumen)  # 2×250×7=3500 + погрузка 1500 > 2500 + 1500
    assert lg.mode == "delivery"
    near = logistics_for("Ялуторовск", "large", tyumen)
    assert near.mode == "pickup" and near.extra_cost == 1050 + tyumen.transport_cost["large"]


def test_city_matching_by_transliteration(tyumen):
    tyumen.pickup_distances_km = {**tyumen.pickup_distances_km, "Тавда": 230}
    assert logistics_for("tavda", "large", tyumen).mode == logistics_for("Тавда", "large", tyumen).mode
    assert logistics_for("tyumen", "small", tyumen).mode == "local"
    assert logistics_for("г. тюмень", "small", tyumen).mode == "local"


def test_empty_yaml_sections_fall_back_to_defaults(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("region: Тюмень\npickup_distances_km:\ndelivery_cost:\n", encoding="utf-8")
    s = load_settings(cfg)
    assert s.pickup_distances_km == {} and s.delivery_cost["small"] == 450
    assert logistics_for("Курган", "small", s).mode == "delivery"


def _page(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(f"<html><head><title>t</title></head><body>{body}</body></html>", encoding="utf-8")
    return parse_saved_page(path)


def test_price_never_glues_title_digits(tmp_path):
    (r,) = _page(tmp_path, "a.html", '<div><a href="/tyumen/telefony/x_4700000001">iPhone 12</a>'
                                      '<span>8 900 ₽</span><p>Описание товара достаточно длинное</p></div>')
    assert r["price"] == 8900
    (r,) = _page(tmp_path, "b.html", '<div data-marker="item"><a href="/tyumen/telefony/x_4700000002">Galaxy S21 8/128'
                                      '</a><s>12 000 ₽</s><span>9 500 ₽</span><span>от 900 ₽ в месяц</span></div>')
    assert r["price"] == 9500
    (r,) = _page(tmp_path, "c.html", '<div data-marker="item"><a href="/tyumen/telefony/x_4700000003">PS4</a>'
                                      '<span>Цена не указана</span><span>Доставка 300 ₽</span></div>')
    assert r["price"] is None


def test_single_result_card_does_not_swallow_page(tmp_path):
    body = ('<nav data-marker="breadcrumbs"><span itemprop="name">Тюмень</span></nav>'
            '<p>Объявления о продаже мобильных телефонов в Тюмени: большой выбор, низкие цены.</p>'
            '<section><div><a href="/tyumen/telefony/iphone_12_mini_4700000010">iPhone 12 mini</a>'
            '<span>14 000 ₽</span><p>Экран разбит, Face ID не работает.</p></div></section>')
    (r,) = _page(tmp_path, "s.html", body)
    assert r["title"] == "iPhone 12 mini" and "Экран разбит" in r["description"]


def test_item_seen_first_in_compact_block_uses_full_card(tmp_path):
    body = ('<div data-marker="viewed-items"><a href="/tyumen/telefony/x_4801000001">iPhone 12</a>'
            '<a href="/tyumen/telefony/y_4801000009">Другой</a></div>'
            '<div data-marker="item"><a href="/tyumen/telefony/x_4801000001">iPhone 12</a>'
            '<meta itemprop="price" content="9500"><div itemprop="description">Разбит экран, остальное работает</div></div>')
    rec = {r["listing_id"]: r for r in _page(tmp_path, "d.html", body)}["4801000001"]
    assert rec["price"] == 9500 and "Разбит" in rec["description"]


def test_item_page_prefers_own_description_and_price(tmp_path):
    body = ('<h1>Ноутбук HP</h1><span data-marker="item-view/item-price" itemprop="price" content="23500">23 500</span>'
            '<span itemprop="priceCurrency">₽</span>'
            '<div data-marker="item-view/item-description"><p>Сильно греется и выключается</p></div>'
            '<div data-marker="item"><a href="/tyumen/noutbuki/z_4598765432">Другой</a>'
            '<div itemprop="description">Идеальное состояние</div><meta itemprop="price" content="999"></div>'
            '<div data-marker="seller-info/name"><a href="/user/0d5cabc/profile?src=item">Пётр</a></div>')
    path = tmp_path / "item.html"
    path.write_text('<html><head><link rel="canonical" href="https://www.avito.ru/tyumen/noutbuki/hp_4512340000">'
                    f'</head><body>{body}</body></html>', encoding="utf-8")
    (r,) = parse_saved_page(path)
    assert r["price"] == 23500 and "греется" in r["description"] and r["seller_id"] == "0d5cabc"


def test_relative_dates():
    from datetime import date
    from arbitrage.html_import import parse_relative_date
    ref = date(2026, 10, 14)
    assert parse_relative_date("2 часа назад", ref) == "2026-10-14"
    assert parse_relative_date("вчера в 18:20", ref) == "2026-10-13"
    assert parse_relative_date("3 дня назад", ref) == "2026-10-11"
    assert parse_relative_date("12 октября", ref) == "2026-10-12"
    assert parse_relative_date("20 декабря", ref) == "2025-12-20"
    assert parse_relative_date("неизвестно", ref) == ""


def test_broken_and_hidden_files_do_not_stop_import(tmp_path):
    folder = tmp_path / "p"
    folder.mkdir()
    (folder / "search.HTML").write_bytes((FIXTURES / "search_page.html").read_bytes())
    (folder / "._search.mhtml").write_bytes(b"\x00\x05\x16\x07junk")
    (folder / "broken.mht").write_bytes(b"not a mime file")
    listings, errors = load_listings(sources_for([folder]))
    assert {l.listing_id for l in listings} == {"4501234567", "4507654321"}
    assert any("broken.mht" in e for e in errors)
    with pytest.raises(FileNotFoundError):
        sources_for([tmp_path / "нет такой папки"])


def test_reimport_of_search_snippet_keeps_full_listing(storage, tyumen, tmp_path):
    from arbitrage.pipeline import run_campaign
    run_campaign(storage, tyumen, [FIXTURES / "listing_page.html"])
    path = tmp_path / "search.html"
    path.write_text('<html><head><title>поиск</title></head><body><div data-marker="item">'
                    '<a href="/yalutorovsk/noutbuki/lenovo_ideapad_3_15_4512345678">Ноутбук Lenovo IdeaPad 3 15</a>'
                    '<meta itemprop="price" content="8500"><div itemprop="description">Состояние хорошее</div>'
                    '</div></body></html>', encoding="utf-8")
    run_campaign(storage, tyumen, [path])
    listing = storage.get_listing("4512345678")
    assert listing.price == 8500 and "перегрева" in listing.description
    assert listing.seller_id == "i123456789" and listing.condition == "Б/у"
    a = storage.latest_analysis("4512345678")
    assert "overheat" in {i.symptom for i in a.defects.items}


def test_seller_ids_from_profile_links_are_distinct(tmp_path):
    from arbitrage.dedup import deduplicate
    from arbitrage.models import Listing
    recs = []
    for n, seller in enumerate(["zzz999", "abc123"]):
        path = tmp_path / f"{n}.html"
        path.write_text('<html><head><link rel="canonical" href="https://www.avito.ru/tyumen/telefony/'
                        f'iphone_12_450111111{n}"></head><body><h1>iPhone 12, 128 ГБ</h1>'
                        '<span data-marker="item-view/item-price">12 000 ₽</span>'
                        f'<div data-marker="seller-info/name"><a href="/user/{seller}/profile">x</a></div>'
                        '</body></html>', encoding="utf-8")
        recs += [Listing.from_dict(r) for r in parse_saved_page(path)]
    assert {r.seller_id for r in recs} == {"zzz999", "abc123"}
    assert len(deduplicate(recs).unique) == 2
