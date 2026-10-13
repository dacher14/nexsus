"""Постоянное хранилище (раздел 20): SQLite по умолчанию, PostgreSQL через DATABASE_URL.

Google Sheets — рабочая панель; источник истины — эта база. История цен,
анализов, переговоров и сделок не удаляется и не перезаписывается.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import sqlalchemy as sa

from .models import Analysis, Listing, ListingStatus, NegotiationStatus, UserStatus, now_iso

metadata = sa.MetaData()

listings = sa.Table(
    "listings", metadata,
    sa.Column("listing_id", sa.String(64), primary_key=True),
    sa.Column("url", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("price", sa.Integer, nullable=False),
    sa.Column("status", sa.String(16), nullable=False, default=ListingStatus.ACTIVE.value),
    sa.Column("duplicate_of", sa.String(64)),
    sa.Column("first_seen", sa.String(32), nullable=False),
    sa.Column("last_checked", sa.String(32), nullable=False),
    sa.Column("is_synthetic", sa.Boolean, nullable=False, default=False),
    sa.Column("data", sa.JSON, nullable=False),
)
price_history = sa.Table(
    "price_history", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("listing_id", sa.String(64), sa.ForeignKey("listings.listing_id"), index=True),
    sa.Column("price", sa.Integer, nullable=False),
    sa.Column("observed_at", sa.String(32), nullable=False),
)
analyses = sa.Table(
    "analyses", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("listing_id", sa.String(64), sa.ForeignKey("listings.listing_id"), index=True),
    sa.Column("analyzed_at", sa.String(32), nullable=False),
    sa.Column("rating", sa.Integer),
    sa.Column("risk", sa.Integer),
    sa.Column("payload", sa.JSON, nullable=False),
)
user_status = sa.Table(
    "user_status", metadata,
    sa.Column("listing_id", sa.String(64), sa.ForeignKey("listings.listing_id"), primary_key=True),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("note", sa.Text, default=""),
    sa.Column("updated_at", sa.String(32), nullable=False),
)
negotiation_state = sa.Table(
    "negotiation_state", metadata,
    sa.Column("listing_id", sa.String(64), sa.ForeignKey("listings.listing_id"), primary_key=True),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("authorized_at", sa.String(32)),
    sa.Column("start_price", sa.Integer),
    sa.Column("target_price", sa.Integer),
    sa.Column("max_price", sa.Integer),
    sa.Column("last_offer", sa.Integer),
    sa.Column("seller_price", sa.Integer),
    sa.Column("agreed_price", sa.Integer),
    sa.Column("rounds", sa.Integer, default=0),
    sa.Column("strategy", sa.Text, default=""),
    sa.Column("next_step", sa.Text, default=""),
    sa.Column("updated_at", sa.String(32), nullable=False),
)
negotiation_messages = sa.Table(
    "negotiation_messages", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("listing_id", sa.String(64), sa.ForeignKey("listings.listing_id"), index=True),
    sa.Column("ts", sa.String(32), nullable=False),
    sa.Column("direction", sa.String(8), nullable=False),  # out / in
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("price", sa.Integer),
    sa.Column("sent", sa.Boolean, nullable=False, default=False),  # True — пользователь подтвердил отправку
)
deals = sa.Table(
    "deals", metadata,
    sa.Column("listing_id", sa.String(64), sa.ForeignKey("listings.listing_id"), primary_key=True),
    sa.Column("status", sa.String(16), nullable=False),  # bought / repairing / listed / sold / written_off
    sa.Column("data", sa.JSON, nullable=False),
    sa.Column("updated_at", sa.String(32), nullable=False),
)
events = sa.Table(
    "events", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.String(32), nullable=False),
    sa.Column("kind", sa.String(48), nullable=False, index=True),
    sa.Column("listing_id", sa.String(64)),
    sa.Column("payload", sa.JSON),
)
llm_cache = sa.Table(
    "llm_cache", metadata,
    sa.Column("key", sa.String(64), primary_key=True),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("created_at", sa.String(32), nullable=False),
)

OPEN_DEAL_STATUSES = ("bought", "repairing", "listed")


class Storage:
    def __init__(self, url: str):
        if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = sa.create_engine(url, future=True)
        metadata.create_all(self.engine)

    # --- журнал событий ---
    def log(self, kind: str, listing_id: str | None = None, payload: Any = None, conn=None) -> None:
        stmt = events.insert().values(ts=now_iso(), kind=kind, listing_id=listing_id, payload=payload)
        if conn is not None:
            conn.execute(stmt)
        else:
            with self.engine.begin() as c:
                c.execute(stmt)

    def events(self, kind: str | None = None, listing_id: str | None = None) -> list[dict]:
        q = sa.select(events).order_by(events.c.id)
        if kind:
            q = q.where(events.c.kind == kind)
        if listing_id:
            q = q.where(events.c.listing_id == listing_id)
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(q)]

    # --- объявления ---
    def upsert_listing(self, listing: Listing, conn=None) -> str:
        """Новая запись или обновление существующей. Возвращает created / price_changed / updated."""
        def run(c) -> str:
            ts = now_iso()
            row = c.execute(sa.select(listings).where(listings.c.listing_id == listing.listing_id)).first()
            data = listing.to_dict()
            if row is None:
                c.execute(listings.insert().values(
                    listing_id=listing.listing_id, url=listing.url, title=listing.title,
                    price=listing.price, status=ListingStatus.ACTIVE.value, first_seen=ts,
                    last_checked=ts, is_synthetic=listing.is_synthetic, data=data))
                c.execute(price_history.insert().values(listing_id=listing.listing_id,
                                                        price=listing.price, observed_at=ts))
                return "created"
            data["discovered_at"] = row.data.get("discovered_at", row.first_seen)
            c.execute(listings.update().where(listings.c.listing_id == listing.listing_id).values(
                url=listing.url, title=listing.title, price=listing.price, last_checked=ts, data=data,
                status=ListingStatus.ACTIVE.value if row.status != ListingStatus.DUPLICATE.value else row.status))
            if row.price != listing.price:
                c.execute(price_history.insert().values(listing_id=listing.listing_id,
                                                        price=listing.price, observed_at=ts))
                self.log("price_changed", listing.listing_id, {"old": row.price, "new": listing.price}, conn=c)
                return "price_changed"
            return "updated"
        if conn is not None:
            return run(conn)
        with self.engine.begin() as c:
            return run(c)

    def mark_status(self, listing_id: str, status: ListingStatus, duplicate_of: str | None = None) -> None:
        with self.engine.begin() as c:
            c.execute(listings.update().where(listings.c.listing_id == listing_id)
                      .values(status=status.value, duplicate_of=duplicate_of, last_checked=now_iso()))
            self.log("listing_status", listing_id, {"status": status.value, "duplicate_of": duplicate_of}, conn=c)

    def get_listing(self, listing_id: str) -> Listing | None:
        with self.engine.connect() as c:
            row = c.execute(sa.select(listings).where(listings.c.listing_id == listing_id)).first()
        return Listing(**row.data) if row else None

    def listing_rows(self, statuses: Iterable[str] = (ListingStatus.ACTIVE.value,)) -> list[dict]:
        with self.engine.connect() as c:
            q = sa.select(listings).where(listings.c.status.in_(list(statuses)))
            return [dict(r._mapping) for r in c.execute(q)]

    def all_listings(self, statuses: Iterable[str] = (ListingStatus.ACTIVE.value,)) -> list[Listing]:
        return [Listing(**r["data"]) for r in self.listing_rows(statuses)]

    def price_history(self, listing_id: str) -> list[tuple[str, int]]:
        with self.engine.connect() as c:
            q = (sa.select(price_history.c.observed_at, price_history.c.price)
                 .where(price_history.c.listing_id == listing_id).order_by(price_history.c.id))
            return [(r.observed_at, r.price) for r in c.execute(q)]

    # --- анализы (история сохраняется) ---
    def save_analyses(self, items: Iterable[Analysis]) -> None:
        with self.engine.begin() as c:
            for a in items:
                c.execute(analyses.insert().values(listing_id=a.listing_id, analyzed_at=a.analyzed_at,
                                                   rating=a.rating, risk=a.risk, payload=a.to_dict()))

    def latest_analysis(self, listing_id: str) -> Analysis | None:
        with self.engine.connect() as c:
            row = c.execute(sa.select(analyses.c.payload).where(analyses.c.listing_id == listing_id)
                            .order_by(analyses.c.id.desc()).limit(1)).first()
        return Analysis.from_dict(row.payload) if row else None

    def latest_analyses(self, listing_ids: Iterable[str] | None = None) -> dict[str, Analysis]:
        latest = sa.select(analyses.c.listing_id, sa.func.max(analyses.c.id).label("mid")) \
            .group_by(analyses.c.listing_id).subquery()
        q = sa.select(analyses.c.listing_id, analyses.c.payload).join(latest, analyses.c.id == latest.c.mid)
        if listing_ids is not None:
            q = q.where(analyses.c.listing_id.in_(list(listing_ids)))
        with self.engine.connect() as c:
            return {r.listing_id: Analysis.from_dict(r.payload) for r in c.execute(q)}

    # --- решения пользователя ---
    def set_user_status(self, listing_id: str, status: UserStatus, note: str = "") -> None:
        with self.engine.begin() as c:
            if c.execute(sa.select(listings.c.listing_id).where(listings.c.listing_id == listing_id)).first() is None:
                raise KeyError(f"Объявление {listing_id} не найдено в базе")
            prev = c.execute(sa.select(user_status).where(user_status.c.listing_id == listing_id)).first()
            values = dict(status=status.value, note=note, updated_at=now_iso())
            if prev:
                c.execute(user_status.update().where(user_status.c.listing_id == listing_id).values(**values))
            else:
                c.execute(user_status.insert().values(listing_id=listing_id, **values))
            self.log("user_status", listing_id, {"status": status.value, "previous": prev.status if prev else None,
                                                 "note": note}, conn=c)

    def user_statuses(self) -> dict[str, str]:
        with self.engine.connect() as c:
            return {r.listing_id: r.status for r in c.execute(sa.select(user_status))}

    # --- переговоры ---
    def negotiation(self, listing_id: str) -> dict | None:
        with self.engine.connect() as c:
            row = c.execute(sa.select(negotiation_state).where(negotiation_state.c.listing_id == listing_id)).first()
        return dict(row._mapping) if row else None

    def negotiations(self) -> dict[str, dict]:
        with self.engine.connect() as c:
            return {r.listing_id: dict(r._mapping) for r in c.execute(sa.select(negotiation_state))}

    def save_negotiation(self, listing_id: str, **values: Any) -> None:
        values["updated_at"] = now_iso()
        with self.engine.begin() as c:
            exists = c.execute(sa.select(negotiation_state.c.listing_id)
                               .where(negotiation_state.c.listing_id == listing_id)).first()
            if exists:
                c.execute(negotiation_state.update().where(negotiation_state.c.listing_id == listing_id)
                          .values(**values))
            else:
                values.setdefault("status", NegotiationStatus.NOT_STARTED.value)
                c.execute(negotiation_state.insert().values(listing_id=listing_id, **values))
            self.log("negotiation_state", listing_id,
                     {k: v for k, v in values.items() if k != "updated_at"}, conn=c)

    def add_message(self, listing_id: str, direction: str, text: str, price: int | None = None,
                    sent: bool = False) -> int:
        with self.engine.begin() as c:
            res = c.execute(negotiation_messages.insert().values(
                listing_id=listing_id, ts=now_iso(), direction=direction, text=text, price=price, sent=sent))
            return int(res.inserted_primary_key[0])

    def mark_message_sent(self, listing_id: str) -> bool:
        with self.engine.begin() as c:
            row = c.execute(sa.select(negotiation_messages.c.id)
                            .where(negotiation_messages.c.listing_id == listing_id,
                                   negotiation_messages.c.direction == "out",
                                   negotiation_messages.c.sent.is_(False))
                            .order_by(negotiation_messages.c.id.desc()).limit(1)).first()
            if not row:
                return False
            c.execute(negotiation_messages.update().where(negotiation_messages.c.id == row.id).values(sent=True))
            return True

    def messages(self, listing_id: str) -> list[dict]:
        with self.engine.connect() as c:
            q = sa.select(negotiation_messages).where(negotiation_messages.c.listing_id == listing_id) \
                .order_by(negotiation_messages.c.id)
            return [dict(r._mapping) for r in c.execute(q)]

    # --- сделки ---
    def deal(self, listing_id: str) -> dict | None:
        with self.engine.connect() as c:
            row = c.execute(sa.select(deals).where(deals.c.listing_id == listing_id)).first()
        return dict(row._mapping) if row else None

    def deals(self) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(sa.select(deals))]

    def save_deal(self, listing_id: str, status: str, data: dict) -> None:
        """Обновление сделки; предыдущее состояние сохраняется в журнале событий."""
        with self.engine.begin() as c:
            prev = c.execute(sa.select(deals).where(deals.c.listing_id == listing_id)).first()
            if prev:
                self.log("deal_history", listing_id, {"status": prev.status, "data": prev.data}, conn=c)
                c.execute(deals.update().where(deals.c.listing_id == listing_id)
                          .values(status=status, data=data, updated_at=now_iso()))
            else:
                c.execute(deals.insert().values(listing_id=listing_id, status=status, data=data,
                                                updated_at=now_iso()))
            self.log("deal", listing_id, {"status": status}, conn=c)

    def capital_in_use(self) -> int:
        return sum(int(d["data"].get("purchase_price") or 0) + int(d["data"].get("costs_so_far") or 0)
                   for d in self.deals() if d["status"] in OPEN_DEAL_STATUSES)

    # --- кэш LLM ---
    def llm_cache_get(self, key: str) -> dict | None:
        with self.engine.connect() as c:
            row = c.execute(sa.select(llm_cache.c.payload).where(llm_cache.c.key == key)).first()
        return row.payload if row else None

    def llm_cache_put(self, key: str, payload: dict) -> None:
        with self.engine.begin() as c:
            if c.execute(sa.select(llm_cache.c.key).where(llm_cache.c.key == key)).first() is None:
                c.execute(llm_cache.insert().values(key=key, payload=payload, created_at=now_iso()))
