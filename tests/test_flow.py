import asyncio
import time

import pytest

from data import catalog
from order_service import signing
from order_service.models import Need, OrderLine
from order_service.offers import find_offers
from order_service import db
from order_service.checks import _history_avg
from order_service.orders import TransitionError, approve, cancel, get_order, place_order
from order_service.policy import evaluate
from tests.conftest import stub

run = asyncio.run
NUT_PRODUCT = "460220"   # Fazer hazelnut milk chocolate: AN CONTAINS
MILK = dict(text="Need 1,000 L lactose-free milk by Monday", query="milk", quantity=1000, unit="L",
            deliver_by="2099-11-16", required_labels=["lactose-free"])


def lines_of(plan):
    return [OrderLine(offer_id=l["offer_id"], quantity=l["quantity"]) for l in plan["lines"]]


def test_search_finds_lactose_free_milk_category():
    r = catalog.search_local_products("maito", ["lactose-free"], unit="L")
    assert r["category"] == "10400"
    codes = [c["product"]["code"] for c in r["candidates"]]
    assert "464306" in codes and "460448" not in codes          # chocolate drink excluded


def test_label_words_in_query_become_labels():
    r = catalog.search_local_products("lactose-free milk", unit="L")
    assert r["required_labels"] == ["lactose-free"]


def test_unknown_allergen_is_never_safe():
    potato = catalog.get_product("470961")
    assert catalog.allergen_status(potato, "nuts") == "unknown"


def test_partial_stock_plan_uses_official_substitute(hotel):
    stub("464306", 1.01, amount=600)
    stub("464201", 1.56)                                        # official substitute of 464306
    r = run(find_offers(hotel, Need(**MILK)))
    plan = r["suggested_plan"]
    assert [(l["product_code"], l["quantity"], l["is_substitution"]) for l in plan["lines"]] == \
        [("464306", 600, False), ("464201", 400, True)]
    assert plan["fully_covered"]
    res = run(place_order(hotel, lines_of(plan), Need(**MILK)))
    assert res["status"] == "pending_approval"
    codes = {x["code"] for x in res["reasons"]}
    assert codes == {"over_spend_limit", "substitution"}
    o = run(approve(res["order_id"], "tester"))
    assert o["status"] == "approved"


def test_routine_order_placed_and_cancellable(hotel):
    stub("464306", 1.01)
    need = Need(**{**MILK, "quantity": 60, "text": "weekly 60 L"})
    r = run(find_offers(hotel, need))
    res = run(place_order(hotel, lines_of(r["suggested_plan"]), need))
    assert res["status"] == "placed" and res["total_eur"] == 60.6, res
    assert get_order(res["order_id"])["cancellable"]
    assert cancel(res["order_id"], "buyer")["status"] == "cancelled"


def test_request_check_short_quantity_is_precise_error(hotel):
    stub("464306", 1.01)
    r = run(find_offers(hotel, Need(**MILK)))
    res = run(place_order(hotel, [OrderLine(offer_id=r["suggested_plan"]["lines"][0]["offer_id"], quantity=3)], Need(**MILK)))
    assert res["status"] == "error" and res["errors"][0]["code"] == "quantity_short"


def test_lot_size_error_suggests_valid_quantities():
    bistro = __import__("order_service.config", fromlist=["x"]).load_buyers()["bistro-satama"]
    stub("462010", 17.41, comp=17.41, amount=300)
    need = Need(text="100 kg sea bass", query="sea bass", quantity=100, unit="KG", deliver_by="2099-11-16")
    r = run(find_offers(bistro, need))
    oid = next(o["offer_id"] for o in r["offers"] if o["product_code"] == "462010")
    res = run(place_order(bistro, [OrderLine(offer_id=oid, quantity=100)], need))
    err = res["errors"][0]
    assert err["code"] == "lot_size" and err["suggestions"] == [96, 102]
    assert "Try 96 or 102" in err["message"]
    assert run(place_order(bistro, [OrderLine(offer_id=oid, quantity=102)], need))["status"] == "placed"


def test_insufficient_stock_error(hotel):
    stub("464306", 1.01, amount=600)
    r = run(find_offers(hotel, Need(**MILK)))
    oid = next(o["offer_id"] for o in r["offers"] if o["product_code"] == "464306")
    res = run(place_order(hotel, [OrderLine(offer_id=oid, quantity=1000)], Need(**MILK)))
    assert any(e["code"] == "insufficient_stock" for e in res["errors"])


def test_price_rise_goes_to_approval(hotel):
    stub("464306", 1.01)
    need = Need(**{**MILK, "quantity": 60})
    r = run(find_offers(hotel, need))
    stub("464306", 1.10)
    res = run(place_order(hotel, lines_of(r["suggested_plan"]), need))
    assert res["status"] == "pending_approval" and "price_changed" in res["flags"]


def test_school_allergen_policy_applies_even_if_agent_omits_it(school):
    stub("470961", 9.78, comp=0.65)
    need = Need(text="40 kg potatoes", query="potatoes", quantity=40, unit="KG", deliver_by="2099-11-16",
                category="17421")                                  # agent declares NO allergens
    r = run(find_offers(school, need))
    assert set(r["enforced"]["exclude_allergens"]) == {"nuts", "peanuts", "sesame"}
    res = run(place_order(school, lines_of(r["suggested_plan"]), need))
    assert res["status"] == "pending_approval"
    msg = next(x["message"] for x in res["reasons"] if x["code"] == "allergen_data_missing")
    assert "nuts (customer allergen policy)" in msg
    # fix 3: the summary must never claim safety the data does not prove
    assert "free of nuts" not in res["agent_understood"]
    assert "must exclude nuts, peanuts, sesame but allergen data is missing" in res["agent_understood"]


def test_school_policy_blocks_product_containing_listed_allergen(school, hotel):
    need = Need(text="nut product", query="x", quantity=1, unit="SALES_UNIT", deliver_by="2099-11-16",
                product_codes=[NUT_PRODUCT])
    stub(NUT_PRODUCT, 5.0)
    assert run(find_offers(school, need))["offers"] == []            # filtered out for the school
    assert run(find_offers(hotel, need))["offers"]                   # same request is fine for the hotel


def test_unknown_allergen_for_order_specific_exclusion_escalates(hotel):
    stub("470961", 9.78, comp=0.65)
    need = Need(text="40 kg potatoes, guest with celery allergy", query="potatoes", quantity=40, unit="KG",
                deliver_by="2099-11-16", exclude_allergens=["celery"], category="17421")
    res = run(place_order(hotel, lines_of(run(find_offers(hotel, need))["suggested_plan"]), need))
    msg = next(x["message"] for x in res["reasons"] if x["code"] == "allergen_data_missing")
    assert "celery (this order)" in msg


def test_label_words_in_query_do_not_break_need_fingerprint(hotel):
    stub("464306", 1.01)
    need = Need(text="60 L lactose-free milk", query="lactose-free milk", quantity=60, unit="L", deliver_by="2099-11-16")
    r = run(find_offers(hotel, need))
    assert r["enforced"]["required_labels"] == ["lactose-free"]
    assert run(place_order(hotel, lines_of(r["suggested_plan"]), need))["status"] == "placed"          # own need
    enforced = Need(**r["need"])
    r2 = run(find_offers(hotel, need))
    assert run(place_order(hotel, lines_of(r2["suggested_plan"]), enforced))["status"] != "error"      # or enforced one


def test_terminal_and_cutoff_excluded_from_plan(hotel):
    stub("464306", 1.01, model="Terminal", amount=999999, closing="2020-01-01T15:00:00+02:00")
    stub("464401", 1.10)
    r = run(find_offers(hotel, Need(**MILK)))
    o = next(o for o in r["offers"] if o["product_code"] == "464306")
    assert {"supplier_stock_unknown", "order_cutoff_passed"} <= set(o["flags"]) and not o["eligible_for_plan"]
    assert r["suggested_plan"]["lines"][0]["product_code"] == "464401"


def test_offer_signature_buyer_and_expiry(hotel):
    stub("464306", 1.01)
    r = run(find_offers(hotel, Need(**MILK)))
    oid = r["suggested_plan"]["lines"][0]["offer_id"]
    with pytest.raises(signing.OfferError, match="altered"):
        signing.verify_offer(oid[:-2] + "00", "hotel-aurora")
    with pytest.raises(signing.OfferError, match="another buyer"):
        signing.verify_offer(oid, "school-koivu")
    expired = signing.sign_offer({"buyer_id": "hotel-aurora", "expires_at": time.time() - 1})
    with pytest.raises(signing.OfferError, match="expired"):
        signing.verify_offer(expired, "hotel-aurora")


def test_need_must_match_offer(hotel):
    stub("464306", 1.01)
    r = run(find_offers(hotel, Need(**MILK)))
    other = Need(**{**MILK, "required_labels": []})
    res = run(place_order(hotel, lines_of(r["suggested_plan"]), other))
    assert res["errors"][0]["code"] == "need_mismatch"


def test_policy_judges_whole_order():
    lines = [{"name": "a", "is_substitution": False}, {"name": "b", "is_substitution": False}]
    out, reasons = evaluate(1200, lines, [], [], {"spend_limit_eur": 1000})
    assert out == "pending_approval" and reasons[0]["code"] == "over_spend_limit"
    assert evaluate(10, lines, [], ["sales block"], {})[0] == "rejected"


def test_agent_declared_doubt_always_goes_to_a_human(hotel):
    stub("464306", 1.01)
    need = Need(**{**MILK, "quantity": 60, "text": "the milk the chef liked", "unresolved": "which milk the chef liked is unknown"})
    res = run(place_order(hotel, lines_of(run(find_offers(hotel, need))["suggested_plan"]), need))
    assert res["status"] == "pending_approval"
    assert res["reasons"][0] == {"code": "agent_unresolved", "message": "Agent could not resolve: which milk the chef liked is unknown"}



# ---------------------------------------------------------------- fix 1: approval re-checks before placing

def _pending_big_milk_order(hotel, qty=2000):
    stub("464306", 1.01)
    need = Need(**{**MILK, "quantity": qty})
    res = run(place_order(hotel, lines_of(run(find_offers(hotel, need))["suggested_plan"]), need))
    assert res["status"] == "pending_approval"            # over the EUR 1,000 limit
    return res["order_id"]


def test_approve_refused_after_order_cutoff(hotel):
    oid = _pending_big_milk_order(hotel)
    o = get_order(oid)
    for l in o["lines"]:
        l["closing_time"] = "2020-01-01T17:00:00+02:00"
    db.execute("UPDATE orders SET lines=? WHERE order_id=?", (__import__("json").dumps(o["lines"]), oid))
    with pytest.raises(TransitionError, match="Too late"):
        run(approve(oid, "tester"))
    assert get_order(oid)["status"] == "pending_approval"


def test_approve_shows_price_rise_then_allows_informed_approval(hotel):
    oid = _pending_big_milk_order(hotel)
    stub("464306", 1.20)                                 # price rose while the order waited
    with pytest.raises(TransitionError, match="Price rose"):
        run(approve(oid, "tester"))
    o = get_order(oid)
    assert o["status"] == "pending_approval" and o["total_eur"] == 2400.0 and "price_changed" in o["flags"]
    assert run(approve(oid, "tester"))["status"] == "approved"   # second, informed approval


def test_approve_refused_when_stock_dropped(hotel):
    oid = _pending_big_milk_order(hotel)
    stub("464306", 1.01, amount=500)
    with pytest.raises(TransitionError, match="only 500"):
        run(approve(oid, "tester"))


def test_approve_uses_lower_price(hotel):
    oid = _pending_big_milk_order(hotel)
    stub("464306", 0.95)
    o = run(approve(oid, "tester"))
    assert o["status"] == "approved" and o["total_eur"] == 1900.0


# ---------------------------------------------------------------- fix 2: splitting does not slip under the limit

def test_split_orders_cannot_slip_under_spend_limit(hotel):
    stub("464306", 1.01)
    need = Need(**{**MILK, "quantity": 890})              # EUR 898.90 each, limit EUR 1,000
    first = run(place_order(hotel, lines_of(run(find_offers(hotel, need))["suggested_plan"]), need))
    assert first["status"] == "placed"
    second = run(place_order(hotel, lines_of(run(find_offers(hotel, need))["suggested_plan"]), need))
    assert second["status"] == "pending_approval"
    assert [r["code"] for r in second["reasons"]] == ["over_spend_limit_24h"]


def test_human_approved_orders_do_not_use_up_agent_budget(hotel):
    oid = _pending_big_milk_order(hotel)                  # EUR 2,020, approved by a human
    run(approve(oid, "tester"))
    need = Need(**{**MILK, "quantity": 60, "text": "weekly 60 L"})
    assert run(place_order(hotel, lines_of(run(find_offers(hotel, need))["suggested_plan"]), need))["status"] == "placed"


# ---------------------------------------------------------------- fix 4: only placed orders are history

def test_pending_orders_do_not_count_as_history(hotel):
    _pending_big_milk_order(hotel)
    assert _history_avg("hotel-aurora", "464306") == (0.0, 0)
