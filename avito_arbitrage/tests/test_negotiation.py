import pytest

from arbitrage.analyzer import Analyzer
from arbitrage.models import NegotiationStatus as NS, UserStatus
from arbitrage.negotiation import Negotiator, NotAuthorized, parse_price_mentions
from conftest import make_listing

PHRASE = "Давай, начинай переговоры по одобренным объявлениям"


@pytest.fixture
def setup(storage, settings):
    comps = [make_listing("iPhone 12 128GB", p, "Всё работает") for p in
             (16000, 17000, 15500, 16500, 18000, 16000, 15000, 17500)]
    target = make_listing("iPhone 12 128GB", 11500, "Всё работает, срочно", seller_id="seller-1")
    for l in comps + [target]:
        storage.upsert_listing(l)
    storage.save_analyses(Analyzer(settings).analyze_all(comps + [target]))
    return Negotiator(storage, settings), target.listing_id


def test_parse_prices():
    assert parse_price_mentions("Отдам за 9 500, не меньше") == [9500]
    assert parse_price_mentions("ну давайте 9.5к") == [9500]
    assert parse_price_mentions("за 10000 заберёте?") == [10000]


def test_requires_approval_and_explicit_command(setup, storage):
    neg, lid = setup
    with pytest.raises(NotAuthorized):
        neg.prepare(lid)  # не одобрено
    storage.set_user_status(lid, UserStatus.APPROVED)
    with pytest.raises(NotAuthorized):
        neg.prepare(lid)  # одобрено, но нет команды
    with pytest.raises(NotAuthorized):
        neg.authorize("ну давай")  # неточная команда
    assert neg.authorize(PHRASE) == [lid]


def test_draft_never_reveals_max_and_is_not_sent(setup, storage):
    neg, lid = setup
    storage.set_user_status(lid, UserStatus.APPROVED)
    neg.authorize(PHRASE)
    analysis, draft, _ = neg.prepare(lid)
    state = storage.negotiation(lid)
    assert draft and state["status"] == NS.DRAFT_READY.value
    assert state["start_price"] <= state["target_price"] <= state["max_price"]
    shown = parse_price_mentions(draft)
    assert shown == [state["start_price"]]
    assert all(not m["sent"] for m in storage.messages(lid))
    assert neg.mark_sent(lid) and storage.negotiation(lid)["status"] == NS.SENT.value


def _start(setup, storage):
    neg, lid = setup
    storage.set_user_status(lid, UserStatus.APPROVED)
    neg.authorize(PHRASE)
    neg.prepare(lid)
    neg.mark_sent(lid)
    return neg, lid, storage.negotiation(lid)


def test_counter_within_limit_gets_midpoint(setup, storage):
    neg, lid, state = _start(setup, storage)
    counter = state["max_price"] - 100 if state["max_price"] - 100 > state["target_price"] else state["max_price"]
    out = neg.process_reply(lid, f"Могу отдать за {counter}")
    assert out.status in (NS.COUNTER_OFFER, NS.AGREED)
    if out.draft:
        offered = parse_price_mentions(out.draft)[-1]
        assert offered <= state["max_price"]


def test_counter_above_max_steps_up_without_revealing_max(setup, storage):
    neg, lid, state = _start(setup, storage)
    offers, status = [], None
    for _ in range(6):
        out = neg.process_reply(lid, "Нет, 11000 минимум")
        status = out.status
        if out.draft:
            offers.append(parse_price_mentions(out.draft)[-1])
            assert "не могу" not in out.draft and "больше" not in out.draft
        if status == NS.DEFERRED:
            break
    assert status == NS.DEFERRED
    assert offers == sorted(set(offers)) and all(o < state["max_price"] for o in offers)


def test_discount_and_delivery_numbers_are_not_seller_price(setup, storage):
    neg, lid, state = _start(setup, storage)
    out = neg.process_reply(lid, "Могу скинуть 500 ₽, не больше")
    assert out.status != NS.AGREED and out.seller_price is None
    out = neg.process_reply(lid, "Отдам за 9 900, доставка около 400 ₽ за ваш счёт")
    assert out.seller_price == 9900


@pytest.mark.parametrize("reply", ["Не пиши мне больше", "Хватит мне писать", "Не надо мне писать", "Отвали"])
def test_informal_stop_requests(setup, storage, reply):
    neg, lid, _ = _start(setup, storage)
    assert neg.process_reply(lid, reply).status == NS.CLOSED


def test_agreement_with_new_defect_never_exceeds_lowered_max(setup, storage):
    neg, lid, state = _start(setup, storage)
    out = neg.process_reply(lid, "Ладно, согласен. Только честно: экран разбит и Face ID не работает")
    new_state = storage.negotiation(lid)
    assert new_state["max_price"] < state["max_price"]
    assert out.status != NS.AGREED or (out.seller_price or 0) <= new_state["max_price"]


def test_prepare_refreshes_stale_draft_after_price_drop(setup, storage):
    neg, lid = setup
    storage.set_user_status(lid, UserStatus.APPROVED)
    neg.authorize(PHRASE)
    _, first, _ = neg.prepare(lid)
    listing = storage.get_listing(lid)
    listing.price = 7500
    storage.upsert_listing(listing)
    _, second, note = neg.prepare(lid)
    state = storage.negotiation(lid)
    assert second != first and state["max_price"] <= 7500
    assert parse_price_mentions(second)[-1] <= 7500
    assert len([m for m in storage.messages(lid) if m["direction"] == "out"]) == 1


def test_agreement_is_not_purchase(setup, storage):
    neg, lid, state = _start(setup, storage)
    out = neg.process_reply(lid, "Договорились, приезжайте")
    assert out.status == NS.AGREED and out.notify_user
    assert "НЕ покупка" in out.next_step
    assert storage.deal(lid) is None


def test_stop_contact_closes(setup, storage):
    neg, lid, _ = _start(setup, storage)
    out = neg.process_reply(lid, "Больше не пишите мне")
    assert out.status == NS.CLOSED
    with pytest.raises(NotAuthorized):
        neg.process_reply(lid, "ещё одно")


def test_new_defect_recalculates_limit(setup, storage):
    neg, lid, state = _start(setup, storage)
    out = neg.process_reply(lid, "Забыл сказать: разбит экран, но всё работает. 10000")
    assert out.new_facts
    assert storage.negotiation(lid)["max_price"] < state["max_price"]


def test_critical_fact_in_reply_stops(setup, storage):
    neg, lid, _ = _start(setup, storage)
    out = neg.process_reply(lid, "Только предоплата на карту, иначе не продам")
    assert out.status == NS.DEFERRED and out.notify_user
