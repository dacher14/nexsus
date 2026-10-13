import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from arbitrage.config import Settings  # noqa: E402
from arbitrage.models import Listing  # noqa: E402
from arbitrage.storage import Storage  # noqa: E402


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def storage(tmp_path):
    return Storage(f"sqlite:///{tmp_path / 'test.db'}")


_counter = iter(range(9_000_000_000, 9_999_999_999, 7))


def make_listing(title: str, price: int, description: str = "", **extra) -> Listing:
    lid = next(_counter)
    data = {"url": f"https://example.invalid/t/{title.split()[0].lower()}_{lid}", "title": title,
            "price": price, "description": description, "is_synthetic": True, **extra}
    return Listing.from_dict(data)


def market_of(title: str, prices: list[int], description: str = "Отличное состояние, всё работает.") -> list[Listing]:
    return [make_listing(title, p, description) for p in prices]


@pytest.fixture
def synthetic_file(tmp_path):
    from generate_test_listings import generate
    path = tmp_path / "listings.json"
    path.write_text(json.dumps({"listings": generate(7)}, ensure_ascii=False), encoding="utf-8")
    return path
