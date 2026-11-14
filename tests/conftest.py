"""Offline test harness: Valio APIs are stubbed so gates and plans are deterministic."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OFFER_SIGNING_SECRET", "test-secret")
os.environ["AI_CROSSCHECK"] = "0"          # tests are offline and deterministic: no LLM calls
os.environ["ORDERS_DB"] = os.path.join(tempfile.mkdtemp(), "orders.db")

import pytest  # noqa: E402

from order_service import checks, config, db, offers, orders  # noqa: E402

# code -> (unit_price, comparison_price, model, amount, closing, reliability-irrelevant)
STUB: dict[str, dict] = {}


def stub(code, price, comp=None, model="Stock", amount=10**10, closing="2099-01-01T17:00:00+02:00",
         date="2026-11-16", block=False, lines=True):
    STUB[code] = dict(price=price, comp=comp if comp is not None else price, model=model, amount=amount,
                      closing=closing, date=date, block=block, lines=lines)


async def fake_pa(codes, customer, deliver_by, fresh=False):
    out = {}
    for c in codes:
        s = STUB.get(c)
        if not s:
            out[c] = {"price": {"found": False, "reason": "no price"},
                      "availability": {"found": False, "deliverable": False, "stock_known": False, "reason": "not stubbed"}}
            continue
        terminal = s["model"] == "Terminal"
        amt = s["amount"]
        out[c] = {
            "price": {"found": True, "unit_price_eur": s["price"], "comparison_price_eur": s["comp"], "comparison_unit": "L",
                      "vat_rate_pct": 13.5, "as_of": "now", "scenario_override": None},
            "availability": {"found": True, "logistic_model": s["model"], "stock_known": not terminal,
                             "available_units": None if terminal or amt >= 10**8 else amt, "ample_stock": amt >= 10**8,
                             "available_date": s["date"] if s["lines"] else None,
                             "later_than_requested": s["date"] > deliver_by.isoformat(),
                             "closing_time": s["closing"], "cutoff_passed": s["closing"] < "2026-01-01",
                             "sales_block": s["block"], "deliverable": s["lines"] and amt > 0 and not s["block"],
                             "as_of": "now", "scenario_override": None},
        }
    return out


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    STUB.clear()
    monkeypatch.setattr(offers, "price_and_availability", fake_pa)
    monkeypatch.setattr(checks, "price_and_availability", fake_pa)
    monkeypatch.setattr(orders, "price_and_availability", fake_pa)
    monkeypatch.setattr(config, "AI_CROSSCHECK", False)
    db.reset()
    yield


@pytest.fixture
def hotel():
    return config.load_buyers()["hotel-aurora"]


@pytest.fixture
def school():
    return config.load_buyers()["school-koivu"]
