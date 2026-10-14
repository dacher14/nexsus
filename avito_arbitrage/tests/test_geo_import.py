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
    assert "rossiya" in search_url("Нижний Тагил", "ps4") and "%D0%A2%D0%B0%D0%B3%D0%B8%D0%BB" in search_url(
        "Нижний Тагил", "ps4")


def test_search_plan_cities():
    plan = build_plan(["smartphone"], ["Тюмень", "Курган", ""], home="Тюмень")
    cities = {q.city for q in plan}
    assert cities == {"Тюмень", "Курган", ""}
    deep = {q.city for q in plan if q.pass_no == 4}
    assert deep == {"Тюмень", ""}
