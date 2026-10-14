"""AI-переговорщик (разделы 15–19).

Порядок работы:
  1. Пользователь отмечает объявления статусом APPROVED.
  2. Пользователь даёт явную команду («Давай, начинай переговоры по одобренным
     объявлениям») — authorize() фиксирует, какие объявления в неё попали.
  3. prepare() перепроверяет цену, пересчитывает экономику и готовит черновик
     первого сообщения. Интеграции отправки сообщений Avito нет, поэтому
     сообщения отправляет пользователь вручную и подтверждает отправку
     (mark_sent). Система никогда не утверждает, что отправила сообщение сама.
  4. process_reply() разбирает ответ продавца и предлагает следующий шаг
     строго в пределах максимальной цены.

Запрещено без отдельного разрешения: оплата, предоплата, покупка, превышение
лимита, раскрытие максимальной цены, контакт после отказа продавца общаться.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .analyzer import Analyzer, fmt_rub
from .categories import normalize_text
from .config import Settings
from .economics import floor100
from .models import Analysis, NegotiationStatus as NS, UserStatus, now_iso
from .storage import Storage

AUTHORIZATION_PHRASES = (
    "давай, начинай переговоры по одобренным объявлениям",
    "начинай переговоры по одобренным объявлениям",
    "начать переговоры по одобренным объявлениям",
)

_STOP_CONTACT_RE = re.compile(
    r"не\s+(?:надо\s+|нужно\s+)?(?:мне\s+)?(?:больше\s+)?пи[шс]|хватит\s+(?:мне\s+)?пи[шс]|"
    r"не\s+беспоко|отстан|отвал|не\s+интересн|спам|черн\w*\s+спис")
_SOLD = ("продано", "уже продан", "продал", "продала", "уже забрали", "забронирован", "бронь")
_AGREE = ("договорились", "согласен", "согласна", "по рукам", "забирайте", "приезжайте", "можно забирать", "хорошо, давайте", "ок, давайте")
_REFUSE = ("нет", "не уступлю", "без торга", "торг неуместен", "торга нет", "дешевле не отдам", "цена окончательная", "не отдам")
_PRICE_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[ \u00a0.]\d{3})+|\d{3,6}|\d{1,3}(?:[.,]\d)?\s*(?:к|тыс|тысяч)\b)")


class NotAuthorized(PermissionError):
    pass


def is_authorization_phrase(text: str) -> bool:
    norm = normalize_text(text).rstrip(".! ")
    return norm in AUTHORIZATION_PHRASES


_UNIT_AFTER = re.compile(r"\s?(?:gb|гб|tb|тб|mah|мач|гц|hz|вт|w\b|мм|см|дюйм|\"|%|x\b|х\b)")
_MODEL_BEFORE = re.compile(r"(?:iphone|айфон|galaxy|note|rtx|gtx|rx|ps|xbox|series|ryzen|core|i[3579]|модель)\s*$")


def _price_mentions(text: str) -> list[tuple[int, int]]:
    """(цена, позиция) для «9 500», «10000», «9.5к». Числа с единицами (128GB) и номера моделей — не цены."""
    out = []
    norm = normalize_text(text).replace("\u00a0", " ")
    for m in _PRICE_RE.finditer(norm):
        if _UNIT_AFTER.match(norm, m.end()) or _MODEL_BEFORE.search(norm[max(0, m.start() - 12):m.start()]):
            continue
        token = m.group(1)
        if re.search(r"к|тыс", token):
            value = int(float(re.sub(r"[^\d.,]", "", token).replace(",", ".")) * 1000)
        else:
            value = int(re.sub(r"[ .]", "", token))
        if 100 <= value <= 5_000_000:
            out.append((value, m.start()))
    return out


def parse_price_mentions(text: str) -> list[int]:
    return [value for value, _ in _price_mentions(text)]


_NOT_ASKING_BEFORE = re.compile(r"(скин|уступ|сброш|сбав|доставк|пересыл|отправк|сдэк|почт|комисси|разниц|залог)"
                                r"[а-я]*\s*(?:на|в|за|около|примерно|где-то|до|ещ[её])?\s*$")
_ASKING_BEFORE = re.compile(r"(?:за|отдам|отдаю|цена|минимум|не меньше|не дешевле|давайте|могу)\s*$")


def seller_price_from(reply: str, reference: int | None = None) -> int | None:
    """Цена, которую называет продавец. Скидки («скину 500»), доставка и суммы намного
    ниже текущего предложения ценой продавца не считаются."""
    norm = normalize_text(reply).replace("\u00a0", " ")
    candidates = []
    for value, pos in _price_mentions(reply):
        before = norm[max(0, pos - 25):pos]
        if _NOT_ASKING_BEFORE.search(before):
            continue
        if reference and value < reference * 0.5:
            continue
        candidates.append((bool(_ASKING_BEFORE.search(before)), pos, value))
    if not candidates:
        return None
    preferred = [c for c in candidates if c[0]]
    return (preferred or candidates)[-1][2]


@dataclass
class ReplyOutcome:
    position: str            # agree / counter / refuse / sold / stop / info
    seller_price: int | None
    status: NS
    next_step: str
    draft: str | None = None
    notify_user: bool = False
    new_facts: list[str] = field(default_factory=list)


def _signature(settings: Settings) -> str:
    return f"\n{settings.negotiation_contact_name}" if settings.negotiation_contact_name else ""


def strategy_for(a: Analysis) -> str:
    parts = []
    if a.defects.has_defects or a.defects.is_untested:
        parts.append("C — аргументированный торг за неисправный товар")
    elif a.market.comps_count >= 3:
        parts.append("A — рациональный торг по ценам аналогов")
    if a.logistics.mode == "delivery":
        parts.append("B — удобство для продавца (быстрое оформление Авито Доставки)")
    else:
        parts.append("B — удобство для продавца (быстрый самовывоз)")
    if "no_bargain" in a.defects.signals:
        parts.append("E — продавец против торга: вероятно отложить сделку")
    return "; ".join(parts)


def first_message(a: Analysis, settings: Settings, title: str) -> str:
    """Первое сообщение: вежливо, по фактам, без выдуманных аргументов и без раскрытия лимита."""
    lines = ["Здравствуйте!", f"Интересует ваше объявление «{title}». Ещё актуально?"]
    if a.defects.has_defects:
        defects = ", ".join(i.label.lower() for i in a.defects.items if not i.accessory)
        lines.append(f"Понимаю, что есть неисправность ({defects}). Ремонт и диагностику беру на себя, "
                     f"а результат заранее неизвестен — поэтому могу предложить {fmt_rub(a.start_price)}.")
    elif a.defects.is_untested:
        lines.append("Раз работоспособность не проверялась, риск остаётся на мне — "
                     f"могу предложить {fmt_rub(a.start_price)}.")
    else:
        lines.append(f"Готов рассмотреть покупку за {fmt_rub(a.start_price)}.")
    lg = a.logistics
    if lg.mode == "delivery":
        lines.append("Готов оформить покупку через Авито Доставку с оплатой на платформе. "
                     "Пришлите, пожалуйста, видео, как товар включается и работает"
                     + (" и как проявляется неисправность." if a.defects.has_defects else "."))
    elif lg.mode == "pickup":
        lines.append(f"Могу подъехать в {lg.city} в ближайшие дни, посмотреть и проверить на месте; "
                     "оплата при встрече после проверки.")
    else:
        lines.append("Если вам удобно, могу подъехать в ближайшие дни, посмотреть и проверить на месте; "
                     "оплата при встрече после проверки.")
    if any(i.symptom == "unknown_fault" for i in a.defects.items):
        lines.append("Подскажите, пожалуйста, что именно не работает и проводилась ли диагностика?")
    return "\n".join(lines) + _signature(settings)


class Negotiator:
    def __init__(self, storage: Storage, settings: Settings, analyzer: Analyzer | None = None):
        self.storage = storage
        self.settings = settings
        self.analyzer = analyzer or Analyzer(settings, capital_in_use=storage.capital_in_use())

    # --- шаг 2: явное разрешение пользователя ---
    def authorize(self, phrase: str) -> list[str]:
        if not is_authorization_phrase(phrase):
            raise NotAuthorized("Переговоры начинаются только после явной команды: "
                                "«Давай, начинай переговоры по одобренным объявлениям».")
        approved = [lid for lid, st in self.storage.user_statuses().items() if st == UserStatus.APPROVED.value]
        authorized = []
        for lid in approved:
            state = self.storage.negotiation(lid)
            if state and state.get("authorized_at"):
                continue
            self.storage.save_negotiation(lid, status=(state or {}).get("status", NS.NOT_STARTED.value),
                                          authorized_at=now_iso())
            authorized.append(lid)
        self.storage.log("negotiations_authorized", None, {"phrase": phrase, "listing_ids": authorized})
        return authorized

    def _require(self, listing_id: str) -> dict:
        if self.storage.user_statuses().get(listing_id) != UserStatus.APPROVED.value:
            raise NotAuthorized(f"Объявление {listing_id} не одобрено пользователем (нужен статус APPROVED)")
        state = self.storage.negotiation(listing_id)
        if not state or not state.get("authorized_at"):
            raise NotAuthorized(f"Нет команды пользователя на переговоры по {listing_id}")
        if state["status"] in (NS.CLOSED.value,):
            raise NotAuthorized(f"Контакт с продавцом {listing_id} прекращён")
        return state

    def _reanalyze(self, listing_id: str) -> Analysis:
        """Перед переговорами: актуальная цена и пересчёт экономики с учётом аналогов в базе."""
        listings = self.storage.all_listings()
        prepared = [self.analyzer.prepare(l) for l in listings]
        index = self.analyzer.build_index(prepared)
        target = next((p for p in prepared if p.listing.listing_id == listing_id), None)
        if target is None:
            raise KeyError(f"Объявление {listing_id} не найдено или неактуально")
        analysis = self.analyzer.analyze(target, index)
        self.storage.save_analyses([analysis])
        return analysis

    # --- шаг 3: подготовка первого сообщения ---
    def prepare(self, listing_id: str) -> tuple[Analysis, str | None, str]:
        state = self._require(listing_id)
        a = self._reanalyze(listing_id)
        listing = self.storage.get_listing(listing_id)
        if a.critical or not a.max_deal_price or a.start_price is None:
            reason = a.recommendation if a.critical else "экономика не позволяет сделать предложение"
            self.storage.save_negotiation(listing_id, status=NS.DEFERRED.value, next_step=f"Не начинать: {reason}")
            return a, None, f"Переговоры не начаты: {reason}"
        if state["status"] == NS.DRAFT_READY.value:
            pending = [m for m in self.storage.messages(listing_id) if m["direction"] == "out" and not m["sent"]]
            if pending and pending[-1]["price"] == a.start_price and state["max_price"] == a.max_deal_price:
                return a, pending[-1]["text"], "Черновик уже подготовлен и ещё не отправлен"
            self.storage.discard_unsent_drafts(listing_id)  # цена или экономика изменились — новый черновик
        elif state["status"] != NS.NOT_STARTED.value:
            # переговоры идут: предел только снижается до свежего расчёта
            self.storage.save_negotiation(
                listing_id, max_price=min(state["max_price"] or a.max_deal_price, a.max_deal_price),
                target_price=min(state["target_price"] or a.target_price, a.target_price))
            return a, None, f"Переговоры уже идут (статус {state['status']}); предел цены пересчитан"
        text = first_message(a, self.settings, listing.title)
        self.storage.add_message(listing_id, "out", text, price=a.start_price, sent=False)
        self.storage.save_negotiation(
            listing_id, status=NS.DRAFT_READY.value, start_price=a.start_price, target_price=a.target_price,
            max_price=a.max_deal_price, last_offer=a.start_price, rounds=1, strategy=strategy_for(a),
            next_step="Отправить сообщение вручную в чате Avito и подтвердить отправку")
        return a, text, "Черновик готов — отправьте его вручную и подтвердите отправку"

    def mark_sent(self, listing_id: str) -> bool:
        self._require(listing_id)
        ok = self.storage.mark_message_sent(listing_id)
        if ok:
            state = self.storage.negotiation(listing_id)
            status = NS.SENT.value if state["status"] == NS.DRAFT_READY.value else state["status"]
            self.storage.save_negotiation(listing_id, status=status, next_step="Ждём ответа продавца")
        return ok

    # --- шаг 4: обработка ответа продавца ---
    def process_reply(self, listing_id: str, reply: str) -> ReplyOutcome:
        state = self._require(listing_id)
        self.storage.add_message(listing_id, "in", reply)
        text = normalize_text(reply)
        seller_price = seller_price_from(reply, state.get("last_offer") or state.get("start_price"))
        new_facts = self._new_facts(listing_id, reply)
        listing = self.storage.get_listing(listing_id)
        max_price = min(state["max_price"] or 0, listing.price if listing else state["max_price"] or 0)
        if new_facts:
            # новый дефект — пересчитать предел до продолжения переговоров
            a = self._reanalyze_with_reply(listing_id, reply)
            max_price = min(max_price, a.max_deal_price or 0)
        if max_price != state["max_price"]:
            self.storage.save_negotiation(
                listing_id, max_price=max_price, target_price=min(state["target_price"] or 0, max_price),
                last_offer=min(state["last_offer"] or 0, max_price))
            state = self.storage.negotiation(listing_id)

        latest = self.storage.latest_analysis(listing_id)
        remote = bool(latest and latest.logistics.mode == "delivery")
        outcome = self._decide(state, text, seller_price, max_price, new_facts, remote)
        if outcome.draft:
            self.storage.add_message(listing_id, "out", outcome.draft, price=outcome.seller_price, sent=False)
        values = dict(status=outcome.status.value, next_step=outcome.next_step)
        if seller_price:
            values["seller_price"] = seller_price
        if outcome.status == NS.AGREED:
            values["agreed_price"] = outcome.seller_price
        if outcome.draft and outcome.status == NS.COUNTER_OFFER:
            values["last_offer"] = parse_price_mentions(outcome.draft)[-1]
            values["rounds"] = (state.get("rounds") or 0) + 1
        self.storage.save_negotiation(listing_id, **values)
        return outcome

    def _new_facts(self, listing_id: str, reply: str) -> list[str]:
        listing = self.storage.get_listing(listing_id)
        before = {i.symptom for i in self.analyzer.prepare(listing).defects.items}
        listing.description = f"{listing.description}\n{reply}"
        after = self.analyzer.prepare(listing).defects
        return [f"{i.label}: «{i.evidence}»" for i in after.items if i.symptom not in before and not i.accessory] + \
               [f"Риск: {f.label}" for f in after.critical_flags]

    def _reanalyze_with_reply(self, listing_id: str, reply: str) -> Analysis:
        listings = self.storage.all_listings()
        for l in listings:
            if l.listing_id == listing_id:
                l.description = f"{l.description}\n[Ответ продавца] {reply}"
        prepared = [self.analyzer.prepare(l) for l in listings]
        index = self.analyzer.build_index(prepared)
        target = next(p for p in prepared if p.listing.listing_id == listing_id)
        a = self.analyzer.analyze(target, index)
        self.storage.save_analyses([a])
        return a

    def _decide(self, state: dict, text: str, seller_price: int | None, max_price: int | None,
                new_facts: list[str], remote: bool = False) -> ReplyOutcome:
        target = state.get("target_price") or 0
        last_offer = state.get("last_offer") or state.get("start_price") or 0
        rounds = state.get("rounds") or 0
        sig = _signature(self.settings)

        if _STOP_CONTACT_RE.search(text):
            return ReplyOutcome("stop", seller_price, NS.CLOSED,
                                "Продавец попросил не писать — контакт прекращён", notify_user=True)
        if any(w in text for w in _SOLD):
            return ReplyOutcome("sold", seller_price, NS.CLOSED,
                                "Товар продан/забронирован — запись закрыта (не считать нашей продажей)",
                                notify_user=True)
        if any(f.startswith("Риск:") for f in new_facts):
            return ReplyOutcome("info", seller_price, NS.DEFERRED,
                                "В ответе выявлен критический риск — переговоры остановлены, нужно решение пользователя",
                                notify_user=True, new_facts=new_facts)
        if not max_price:
            return ReplyOutcome("info", seller_price, NS.DEFERRED,
                                "После новых сведений сделка не проходит по экономике — отложить",
                                notify_user=True, new_facts=new_facts)

        check = ("оформление через Авито Доставку с проверкой при получении" if remote
                 else "проверка товара на месте")
        agree = any(w in text for w in _AGREE)
        if agree and (seller_price is None or seller_price <= max_price):
            price = seller_price or last_offer
            if not price or price > max_price:
                return ReplyOutcome("info", price, NS.DEFERRED,
                                    f"Продавец согласен на {fmt_rub(price)}, но после новых сведений предел "
                                    f"ниже — нужно решение пользователя", notify_user=True, new_facts=new_facts)
            return ReplyOutcome("agree", price, NS.AGREED,
                                f"Цена {fmt_rub(price)} согласована. Это НЕ покупка: нужна {check} "
                                f"и отдельное подтверждение покупки пользователем.",
                                notify_user=True, new_facts=new_facts)
        if seller_price is not None:
            if seller_price <= target:
                if remote:
                    draft = (f"Спасибо! {fmt_rub(seller_price)} мне подходит. Как удобнее оформить "
                             f"через Авито Доставку по этой цене?{sig}")
                else:
                    draft = (f"Спасибо! {fmt_rub(seller_price)} мне подходит. Когда удобно встретиться, "
                             f"чтобы я посмотрел и проверил? Оплата на месте после проверки.{sig}")
                return ReplyOutcome("counter", seller_price, NS.AGREED,
                                    f"Встречная цена в пределах целевой — дальше {check}; "
                                    "покупка только после проверки и подтверждения пользователя",
                                    draft=draft, notify_user=True, new_facts=new_facts)
            if seller_price <= max_price:
                if rounds >= self.settings.negotiation_max_rounds:
                    return ReplyOutcome("counter", seller_price, NS.COUNTER_OFFER,
                                        f"Цена {fmt_rub(seller_price)} в пределах лимита, раунды торга исчерпаны — "
                                        f"решение за пользователем", notify_user=True, new_facts=new_facts)
                offer = floor100((last_offer + seller_price) / 2)
                offer = min(max(offer, last_offer), max_price)
                handover = "сразу оформить заказ с доставкой" if remote else "забрать в удобное для вас время"
                draft = (f"Понимаю вас. Могу предложить {fmt_rub(offer)} и {handover}, "
                         f"без долгих согласований.{sig}")
                return ReplyOutcome("counter", seller_price, NS.COUNTER_OFFER,
                                    f"Предложить {fmt_rub(offer)} (в пределах лимита)", draft=draft,
                                    new_facts=new_facts)
            # встречная цена выше предела: шаг к пределу, но сам предел не называем и не подаём как потолок
            step = floor100(last_offer + (max_price - last_offer) / 2)
            offer = min(step, max_price - 100)
            if rounds < self.settings.negotiation_max_rounds and offer > last_offer:
                handover = "сразу оформить заказ с доставкой" if remote else "быстро встретиться и забрать"
                draft = (f"Спасибо за ответ. Могу предложить {fmt_rub(offer)} и {handover}. "
                         f"Подумайте, пожалуйста.{sig}")
                return ReplyOutcome("counter", seller_price, NS.COUNTER_OFFER,
                                    f"Предложить {fmt_rub(offer)}; при отказе — отложить и следить за ценой",
                                    draft=draft, new_facts=new_facts)
            return ReplyOutcome("refuse", seller_price, NS.DEFERRED,
                                f"Продавец хочет {fmt_rub(seller_price)} — выше предела. Отложено, следим за снижением цены",
                                notify_user=True, new_facts=new_facts)
        if any(re.search(rf"(?<![a-zа-я]){re.escape(w)}(?![a-zа-я])", text) for w in _REFUSE):
            return ReplyOutcome("refuse", None, NS.DEFERRED,
                                "Продавец не готов торговаться — отложено, следим за изменением цены",
                                notify_user=True, new_facts=new_facts)
        return ReplyOutcome("info", None, NS.COUNTER_OFFER if state["status"] != NS.DRAFT_READY.value else NS.SENT,
                            "Ответ без цены — уточнить позицию продавца", notify_user=bool(new_facts),
                            new_facts=new_facts)
