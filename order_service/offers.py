"""Step 1: find_offers(need) -> signed offers with trust flags + a suggested plan.

Rules, no AI:
 1. Filter the local catalog (labels, allergens, unit, category).
 2. Call live price + availability only for the top candidates and their
    official substitutes (capped).
 3. Build offers with flags and provenance; sign each orderable offer (15 min).
 4. Suggested plan: hard filters -> rank by the buyer's priority (known stock
    above Terminal, verified allergen data above unknown) -> fill the quantity
    from the top product, then its official substitutes (good/better/best),
    rounding to the order step.
"""
from __future__ import annotations

import json
import math
import time
from datetime import date

from data import catalog
from order_service import config, db
from order_service.models import Need
from order_service.signing import short_ref, sign_offer
from order_service.valio_client import ValioAPIError, active_scenario, price_and_availability

UNIT_LABEL = {"L": "L", "KG": "kg", "SALES_UNIT": "units"}


def fmt_qty(x: float) -> str:
    return f"{x:,.0f}" if abs(x - round(x)) < 1e-9 else f"{x:,.2f}"


def customer_allergens(buyer: dict) -> list[str]:
    return list((buyer["mandate"].get("allergen_policy") or {}).get("exclude") or [])


def effective_need(buyer: dict, need: Need) -> tuple[Need, list[str]]:
    """The need Valio actually enforces: the agent's declared need, plus label words found in
    the query, plus the customer's standing allergen list (applied whatever the agent sends).
    Returns (effective need, notes). Merging is idempotent, so the signed fingerprint of the
    effective need matches whether the agent resends its own need or the enforced one."""
    _, label_words = catalog.split_query(need.query)
    policy = customer_allergens(buyer)
    labels = sorted(set(need.required_labels) | set(label_words))
    excl = sorted(set(need.exclude_allergens) | set(policy))
    notes = []
    if set(label_words) - set(need.required_labels):
        notes.append(f"Query words read as required labels: {sorted(set(label_words) - set(need.required_labels))}")
    added = sorted(set(policy) - set(need.exclude_allergens))
    if added:
        notes.append(f"Customer allergen policy applied: also excluding {added}")
    return need.model_copy(update={"required_labels": labels, "exclude_allergens": excl}), notes


def resolve_measure(p: dict, price: dict, unit: str) -> dict:
    """Amount of the need's unit per sales unit. Local data, confirmed or corrected by
    the price API (catalogPrice / comparisonPrice when comparisonUnit matches)."""
    if unit == "SALES_UNIT":
        return {"per_sales_unit": 1.0, "source": "sales unit"}
    local = catalog.measure_per_unit(p, unit)
    derived = None
    if price.get("found") and price.get("comparison_price_eur") and (price.get("comparison_unit") or "").upper() == unit:
        derived = round(price["unit_price_eur"] / price["comparison_price_eur"], 3)
    if local and derived:
        if abs(local - derived) / local <= 0.1:
            return {"per_sales_unit": local, "source": f"{p.get('measure_source')}, confirmed by price API"}
        return {"per_sales_unit": derived, "source": "price API (catalog price / comparison price); product data disagreed",
                "product_data_value": local}
    if local:
        return {"per_sales_unit": local, "source": p.get("measure_source")}
    if derived:
        return {"per_sales_unit": derived, "source": "price API (catalog price / comparison price)"}
    return {"per_sales_unit": None, "source": "unknown"}


def round_up(x: float, step: float) -> float:
    return round(math.ceil(x / step - 1e-9) * step, 4)


def round_down(x: float, step: float) -> float:
    return round(math.floor(x / step + 1e-9) * step, 4)


def offer_flags(p: dict, live: dict, need: Need, allergen_status: dict, measure: dict, mandate: dict) -> list[str]:
    a, pr = live["availability"], live["price"]
    flags = []
    if not pr.get("found"):
        flags.append("no_price")
    if not a.get("found") or not a.get("deliverable"):
        flags.append("not_deliverable")
    if a.get("sales_block"):
        flags.append("sales_block")
    if a.get("logistic_model") == "Terminal":
        flags.append("supplier_stock_unknown")
    if a.get("later_than_requested"):
        flags.append("later_than_requested")
    if a.get("cutoff_passed"):
        flags.append("order_cutoff_passed")
    rel = p.get("reliability")
    if rel is None:
        flags.append("reliability_unknown")
    elif rel < max(config.LOW_RELIABILITY, mandate.get("min_delivery_reliability", 0)):
        flags.append("low_reliability")
    for g, s in allergen_status.items():
        if s == "unknown":
            flags.append(f"allergen_unknown:{g}")
    if measure.get("per_sales_unit") is None:
        flags.append("unit_conversion_unknown")
    if a.get("scenario_override") or pr.get("scenario_override"):
        flags.append("demo_scenario_override")
    return flags


PLAN_BLOCKERS = {"no_price", "not_deliverable", "sales_block", "order_cutoff_passed", "later_than_requested", "unit_conversion_unknown"}


def provenance(p: dict, live: dict) -> dict:
    a, pr = live["availability"], live["price"]
    av_src = "Valio availability API"
    if a.get("scenario_override"):
        av_src += f" + demo scenario override '{a['scenario_override']}'"
    pr_src = "Valio price API (catalog price, excl. VAT)"
    if pr.get("scenario_override"):
        pr_src += f" + demo scenario override '{pr['scenario_override']}'"
    return {
        "price": {"source": pr_src, "as_of": pr.get("as_of")},
        "availability": {"source": av_src, "as_of": a.get("as_of"), "stock_known": a.get("stock_known")},
        "allergens": {"source": "manufacturer data (GS1 Synkka)", "last_updated": p.get("synkka_updated"),
                      "statement_present": p.get("allergen_data")},
        "reliability": {"source": "Valio delivery history (share of ordered quantity delivered)", "available": p.get("reliability") is not None},
        "carbon": {"source": "product-group average kg CO2e/kg", "per_product": False, "available": p.get("carbon_kg_per_kg") is not None},
    }


def _rank_key(o: dict, priority: str, country: str | None):
    allergen_unknown = any(f.startswith("allergen_unknown") for f in o["flags"])
    low_rel = "low_reliability" in o["flags"]
    stock_unknown = not o["availability"].get("stock_known")
    if priority == "reliability":
        metric = -(o["product"]["delivery_reliability"] if o["product"]["delivery_reliability"] is not None else -1)
    elif priority == "carbon":
        c = o["product"]["carbon_kg_co2e_per_kg"]
        metric = c if c is not None else 1e9
    else:
        metric = o["price"]["price_per_need_unit_eur"] if o["price"].get("price_per_need_unit_eur") is not None else 1e9
    other_country = bool(country) and o["product"]["country_of_origin"] != country
    return (allergen_unknown, stock_unknown, low_rel, metric, other_country, -o["_relevance"])


def build_plan(offers: list[dict], need: Need) -> dict:
    prefs = need.preferences
    eligible = {o["product_code"]: o for o in offers if o["eligible_for_plan"]}
    primaries = sorted([o for o in eligible.values() if o["role"] == "primary"],
                       key=lambda o: _rank_key(o, prefs.priority, prefs.country))
    remaining = need.quantity
    lines, used, notes = [], set(), []

    def take(o: dict, is_sub: bool, sub_for: str | None):
        nonlocal remaining
        step = o["product"]["order_step"]
        per = o["measure"]["per_sales_unit"]
        units = round_up(remaining / per, step)
        avail = o["availability"].get("available_units")
        if avail is not None:
            units = min(units, round_down(avail, step))
        if units <= 0:
            return
        covered = units * per
        remaining -= covered
        used.add(o["product_code"])
        lines.append({
            "offer_id": o["offer_id"], "ref": o["ref"], "product_code": o["product_code"], "name": o["product"]["name"],
            "quantity": units, "sales_unit": o["product"]["sales_unit"],
            "covers": f"{fmt_qty(covered)} {UNIT_LABEL[need.unit]}",
            "line_total_eur": round(units * o["price"]["unit_price_eur"], 2),
            "is_substitution": is_sub, "substitute_for": sub_for,
            "substitute_rank": o.get("substitute_rank") if is_sub else None,
        })
        if avail is not None and units * per < need.quantity and remaining > 1e-9:
            notes.append(f"{o['product']['name']} has only {fmt_qty(avail)} {o['product']['sales_unit']} "
                         f"(= {fmt_qty(avail * per)} {UNIT_LABEL[need.unit]}) by {need.deliver_by.isoformat()}")

    for prim in primaries:
        if remaining <= 1e-9:
            break
        if prim["product_code"] in used:
            continue
        take(prim, False, None)
        if remaining > 1e-9 and prefs.allow_substitutes:
            subs = [eligible[c] for c in prim["product"]["official_substitutes"] if c in eligible and c not in used]
            subs.sort(key=lambda o: ("best", "better", "good").index(o.get("substitute_rank") or "good"))
            for s in subs:
                if remaining <= 1e-9:
                    break
                take(s, True, prim["product_code"])
                notes.append(f"{s['product']['name']} is Valio's official substitute for {prim['product']['name']} "
                             f"(rank: {s.get('substitute_rank')})")

    if not lines:
        reasons = sorted({r for o in offers for r in o["ineligible_reasons"]})
        return {"lines": [], "fully_covered": False, "why": "No offer passes the hard filters" +
                (f": {', '.join(reasons)}" if reasons else ""), "covers": None, "total_eur": 0.0}
    covered = need.quantity - remaining
    total = round(sum(l["line_total_eur"] for l in lines), 2)
    top = lines[0]
    why = [f"Ranked by {prefs.priority}; {top['name']} ranks first among offers that meet every hard filter"]
    why += notes
    if remaining > 1e-9:
        why.append(f"Only {fmt_qty(covered)} of {fmt_qty(need.quantity)} {UNIT_LABEL[need.unit]} can be covered by "
                   f"{need.deliver_by.isoformat()}" + ("" if prefs.accept_partial else "; set preferences.accept_partial to order it anyway"))
    return {
        "lines": [{k: v for k, v in l.items()} for l in lines],
        "covers": f"{fmt_qty(min(covered, need.quantity))} {UNIT_LABEL[need.unit]} by {need.deliver_by.isoformat()}",
        "covered_quantity": round(covered, 3),
        "fully_covered": remaining <= 1e-9,
        "total_eur": total,
        "why": ". ".join(why),
    }


def _rank_substitutes(offers_by_code: dict[str, dict]) -> None:
    """good / better / best among each primary's eligible official substitutes."""
    for o in offers_by_code.values():
        if o["role"] != "primary":
            continue
        base = o["price"].get("price_per_need_unit_eur") or 0
        subs = [offers_by_code[c] for c in o["product"]["official_substitutes"]
                if c in offers_by_code and offers_by_code[c]["eligible_for_plan"]]
        subs.sort(key=lambda s: (
            any(f.startswith("allergen_unknown") for f in s["flags"]),
            not s["availability"].get("stock_known"),
            (s["price"].get("price_per_need_unit_eur") or 1e9) - base,
            -(s["product"]["delivery_reliability"] or 0),
            s["product"]["carbon_kg_co2e_per_kg"] or 1e9,
        ))
        for i, s in enumerate(subs):
            s.setdefault("substitute_rank", ("best", "better")[i] if i < 2 else "good")


async def find_offers(buyer: dict, need: Need) -> dict:
    today = date.today()
    if need.deliver_by < today:
        return {"error": f"deliver_by {need.deliver_by} is in the past", "hint": "Use a date today or later"}
    mandate = buyer["mandate"]
    try:
        catalog.validate_labels(need.required_labels)
        catalog.validate_allergens(need.exclude_allergens)
    except catalog.UnknownTerm as e:
        return {"error": str(e), "hint": "Fix the declared need and call find_offers again"}
    need, enforce_notes = effective_need(buyer, need)
    digest = need.digest()      # fingerprint of the ENFORCED need: same whether the agent resends its own or this one
    try:
        found = catalog.search_local_products(
            need.query, need.required_labels, need.exclude_allergens, need.unit,
            need.category, need.product_codes, limit=config.LIVE_CANDIDATES)
    except catalog.UnknownTerm as e:
        return {"error": str(e), "hint": "Fix the declared need and call find_offers again"}

    found["notes"] = enforce_notes + [n for n in found["notes"] if not n.startswith("Query words read as")]
    if not found["candidates"]:
        db.audit("find_offers", f"No catalog match for '{need.query}'", buyer_id=buyer["buyer_id"], actor=f"agent:{buyer['buyer_id']}",
                 data={"need": need.model_dump(mode="json"), "dropped": found["dropped"]})
        return {"need": need.model_dump(mode="json"), "offers": [], "suggested_plan": None,
                "search": {k: found[k] for k in ("query_words", "category", "dropped", "notes")},
                "hint": "No product matches. Try other product words (fi/sv/en), fewer labels, or another unit."}

    primaries = {c["product"]["code"]: c for c in found["candidates"]}
    sub_of: dict[str, list[str]] = {}
    if need.preferences.allow_substitutes:
        for code in list(primaries):
            for s in catalog.official_substitutes(code):
                if s not in primaries:
                    sub_of.setdefault(s, []).append(code)
    sub_products = catalog.get_products(list(sub_of))
    for code, p in list(sub_products.items()):     # substitutes must meet the same hard requirements
        if any(not catalog.label_ok(p, l) for l in need.required_labels) or \
           any(catalog.allergen_status(p, g) in ("contains", "may_contain") for g in need.exclude_allergens) or \
           (need.unit != "SALES_UNIT" and catalog.measure_per_unit(p, need.unit) is None):
            sub_products.pop(code)
    codes = (list(primaries) + list(sub_products))[: config.API_MAX_PRODUCTS]

    try:
        live = await price_and_availability(codes, buyer.get("customer_number", buyer["buyer_id"]), need.deliver_by)
    except ValioAPIError as e:
        db.audit("valio_api_error", str(e), buyer_id=buyer["buyer_id"], actor="system")
        return {"error": f"Valio live data unavailable: {e}", "hint": "Retry in a moment. No offer is issued without live price and stock."}

    issued_at = int(time.time())
    expires_at = issued_at + config.OFFER_TTL_SECONDS
    offers: dict[str, dict] = {}
    for code in codes:
        is_primary = code in primaries
        p = primaries[code]["product"] if is_primary else sub_products[code]
        status = primaries[code]["allergen_status"] if is_primary else {g: catalog.allergen_status(p, g) for g in need.exclude_allergens}
        lv = live[code]
        measure = resolve_measure(p, lv["price"], need.unit)
        flags = offer_flags(p, lv, need, status, measure, mandate)
        card = catalog.product_card(p, need.exclude_allergens)
        price = dict(lv["price"])
        if price.get("found") and measure.get("per_sales_unit"):
            price["price_per_need_unit_eur"] = round(price["unit_price_eur"] / measure["per_sales_unit"], 4)
        reasons = sorted(PLAN_BLOCKERS & set(flags))
        if price.get("found") and need.preferences.max_unit_price_eur is not None and price["unit_price_eur"] > need.preferences.max_unit_price_eur:
            reasons.append("over max_unit_price_eur")
        orderable = price.get("found") and lv["availability"].get("deliverable") and not lv["availability"].get("sales_block")
        offer = {
            "product_code": code,
            "role": "primary" if is_primary else "official_substitute",
            "substitute_for": None if is_primary else sub_of[code],
            "product": card,
            "price": price,
            "availability": lv["availability"],
            "measure": {"unit": need.unit, **measure},
            "flags": flags,
            "eligible_for_plan": not reasons,
            "ineligible_reasons": reasons,
            "provenance": provenance(p, lv),
            "offer_id": None,
            "_relevance": primaries[code]["relevance"] if is_primary else -1,
        }
        if orderable:
            facts = {
                "v": 1, "buyer_id": buyer["buyer_id"], "product_code": code,
                "unit_price_eur": price["unit_price_eur"], "sales_unit": card["sales_unit"],
                "available_units": lv["availability"].get("available_units"),
                "stock_known": lv["availability"].get("stock_known"),
                "deliver_by": need.deliver_by.isoformat(), "available_date": lv["availability"].get("available_date"),
                "role": offer["role"], "substitute_for": offer["substitute_for"],
                "need_digest": digest, "issued_at": issued_at, "expires_at": expires_at,
            }
            offer["offer_id"] = sign_offer(facts)
            offer["ref"] = short_ref(offer["offer_id"])
            offer["expires_at"] = expires_at
            db.execute("INSERT OR REPLACE INTO offer_log VALUES (?,?,?,?,?,?)",
                       (offer["ref"], buyer["buyer_id"], db.now_iso(), code, digest, json.dumps(facts)))
        else:
            offer["ref"] = None
            offer["not_orderable_reason"] = lv["availability"].get("reason") or lv["price"].get("reason") or "sales block"
        offers[code] = offer

    _rank_substitutes(offers)
    plan = build_plan(list(offers.values()), need)
    ordered = sorted(offers.values(), key=lambda o: (not o["eligible_for_plan"], o["role"] != "primary",
                                                    _rank_key(o, need.preferences.priority, need.preferences.country)))
    for o in ordered:
        o.pop("_relevance", None)

    db.audit("find_offers",
             f"Searched '{need.query}' ({', '.join(need.required_labels) or 'no labels'}): {len(found['candidates'])} catalog candidates, "
             f"live data for {len(codes)}; plan: {plan.get('covers') or 'none'}" + (f", EUR {plan['total_eur']:.2f}" if plan.get("lines") else ""),
             buyer_id=buyer["buyer_id"], actor=f"agent:{buyer['buyer_id']}",
             data={"need": need.model_dump(mode="json"), "need_digest": digest, "plan": plan,
                   "candidates": codes, "dropped": found["dropped"], "scenario": active_scenario()})
    return {
        "need": need.model_dump(mode="json"),
        "enforced": {"required_labels": need.required_labels, "exclude_allergens": need.exclude_allergens,
                     "customer_allergen_policy": customer_allergens(buyer)},
        "need_digest": digest,
        "search": {k: found[k] for k in ("query_words", "required_labels", "category", "total_matches", "dropped", "notes")},
        "offers": ordered,
        "suggested_plan": plan,
        "valid_until": expires_at,
        "how_to_order": "Call place_order with lines=[{offer_id, quantity}] (quantities in sales units) and the same need "
                        "(your own or the enforced one: Valio re-applies its rules either way). "
                        "A basic agent can pass suggested_plan.lines as-is.",
        "scenario": active_scenario(),
    }
