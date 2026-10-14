import json
from types import SimpleNamespace

from arbitrage.analyzer import Analyzer
from arbitrage.learning import calibrations, category_performance, update_deal
from arbitrage.llm import LLMAnalyzer
from arbitrage.models import UserStatus
from arbitrage.telegram_bot import BotLogic
from conftest import make_listing


def _seed(storage, settings, price=9500):
    comps = [make_listing("iPhone 12 128GB", p, "Всё работает") for p in (16000, 17000, 15500, 16500, 18000)]
    target = make_listing("iPhone 12 128GB", price, "Всё работает")
    for l in comps + [target]:
        storage.upsert_listing(l)
    storage.save_analyses(Analyzer(settings).analyze_all(comps + [target]))
    return target.listing_id


def test_deal_lifecycle_and_calibration(storage, settings):
    lid = _seed(storage, settings)
    update_deal(storage, lid, "bought", purchase_price=9000, costs_so_far=500, bought_at="2026-10-01")
    assert storage.capital_in_use() == 9500
    data = update_deal(storage, lid, "sold", sold_price=12000, sold_at="2026-10-08")
    assert data["net_profit"] == 12000 - 9000 - 500
    assert data["days_held"] == 7
    assert data["forecast"]["resale_base"] and "resale_ratio" in data["errors"]
    assert storage.capital_in_use() == 0
    assert len(storage.events(kind="deal_history", listing_id=lid)) == 1  # прошлое состояние сохранено
    cal = calibrations(storage)["smartphone"]
    ratio = data["errors"]["resale_ratio"]
    assert min(1.0, ratio) <= cal.resale <= max(1.0, ratio)  # усадка к 1.0
    perf = category_performance(storage)
    assert perf[0]["category"] == "smartphone" and perf[0]["profit"] == 2500


class FakeClient:
    def __init__(self, payload, stop_reason="end_turn"):
        self.calls = 0
        self.payload = payload
        self.stop_reason = stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        assert kwargs["output_config"]["format"]["type"] == "json_schema"
        assert kwargs["fallbacks"] == "default"
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


PAYLOAD = {"category": "laptop", "brand": "lenovo", "model": "legion 5", "variant": "", "model_confident": True,
           "symptoms": [{"code": "overheat", "evidence": "выключается в играх"}], "red_flags": [], "summary": "ok"}


def test_llm_cache_and_budget():
    cache = {}
    client = FakeClient(PAYLOAD)
    llm = LLMAnalyzer(["overheat"], ["replica"], max_calls=1, client=client,
                      cache_get=cache.get, cache_put=cache.__setitem__)
    first = llm.analyze("Игровой ноут", "в играх выключается", 20000)
    again = llm.analyze("Игровой ноут", "в играх выключается", 20000)
    assert first.symptoms == again.symptoms == [("overheat", "LLM: выключается в играх")]
    assert client.calls == 1  # повтор — из кэша
    assert llm.analyze("Другой ноут", "текст", 1000) is None  # лимит вызовов исчерпан


def test_llm_refusal_returns_none():
    llm = LLMAnalyzer(["overheat"], [], client=FakeClient(PAYLOAD, stop_reason="refusal"))
    assert llm.analyze("x", "y", 1) is None


def test_analyzer_uses_llm_for_ambiguous(settings):
    settings.llm_enabled = True
    llm = LLMAnalyzer(["overheat"], [], client=FakeClient(PAYLOAD))
    listing = make_listing("Игровой ноут", 20000, "Иногда что-то не так")
    prepared = Analyzer(settings, llm=llm).prepare(listing)
    assert prepared.model.model == "legion 5" and prepared.defects.llm_used
    assert {i.symptom for i in prepared.defects.items} == {"overheat"}


def test_bot_logic(storage, settings, tmp_path):
    lid = _seed(storage, settings)
    bot = BotLogic(storage, settings, tmp_path / "out.xlsx")
    assert "Команды" in bot.handle("/help")
    assert lid in bot.handle("/top 5")
    assert "APPROVED" in bot.handle(f"/approve {lid}")
    assert storage.user_statuses()[lid] == UserStatus.APPROVED.value
    assert "Нет разрешённых" in bot.handle("/prepare")
    assert "Разрешение принято" in bot.handle("Давай, начинай переговоры по одобренным объявлениям")
    assert "Черновик" in bot.handle("/prepare") or "Переговоры не начаты" in bot.handle("/prepare")
    assert "Таблица обновлена" in bot.handle("/export")
    assert "Не понял" in bot.handle("купи всё")


def test_deal_for_unknown_listing_is_rejected(storage):
    import pytest
    with pytest.raises(KeyError):
        update_deal(storage, "999", "bought", purchase_price=100)
    assert storage.capital_in_use() == 0


def test_report_counts_and_rejected(storage, settings):
    from arbitrage.negotiation import Negotiator
    from arbitrage.report import campaign_report, negotiation_report
    lid = _seed(storage, settings)
    storage.set_user_status(lid, UserStatus.APPROVED)
    Negotiator(storage, settings).authorize("Давай, начинай переговоры по одобренным объявлениям")
    assert "с подготовленными сообщениями: 0" in negotiation_report(storage)
    analyses = list(storage.latest_analyses().values())
    assert lid in campaign_report(analyses, {}, None, settings, False)
    assert lid not in campaign_report(analyses, {}, None, settings, False, statuses={lid: "REJECTED"})
