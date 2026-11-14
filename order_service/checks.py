"""Gate 1, the request check: did the agent understand the buyer?

Runs before policy. Hard problems return precise errors the agent can fix on its
own ("Quantity 100 is not a multiple of 6. Try 96 or 102."). Softer problems
become flags that the policy weighs. It also writes the plain-language "agent
understood" summary shown next to the buyer's own words.
"""
from __future__ import annotations

from datetime import date

from data import catalog
from order_service import config, db
from order_service.models import Need, OrderLine
from order_service.offers import UNIT_LABEL, effective_need, fmt_qty, resolve_measure, round_down, round_up
from order_service.signing import OfferError, short_ref, verify_offer
from order_service.valio_client import ValioAPIError, price_and_availability


class CheckResult:
    def __init__(self):
        self.errors: list[dict] = []
        self.flags: list[str] = []
        self.notes: list[str] = []
        self.lines: list[dict] = []
        self.reject_reasons: list[str] = []
        self.price_changes: list[dict] = []

    def error(self, code: str, message: str, line: int | None = None, **extra):
        self.errors.append({"code": code, "message": message, "line": line, **extra})

    def flag(self, f: str):
        if f not in self.flags:
            self.flags.append(f)


def _qty_suggestions(q: float, step: float) -> list[float]:
    lo, hi = round_down(q, step), round_up(q, step)
    out = [x for x in (lo, hi) if x > 0]
    return [int(x) if x == int(x) else x for x in dict.fromkeys(out)]


def _history_avg(buyer_id: str, code: str) -> tuple[float, int]:
    qs = []
    for o in db.query("SELECT lines FROM orders WHERE buyer_id=? AND status IN ('placed','approved')", (buyer_id,)):
        for l in o["lines"] or []:
            if l["product_code"] == code:
                qs.append(l["quantity"])
    return (sum(qs) / len(qs), len(qs)) if qs else (0.0, 0)


async def request_check(buyer: dict, lines: list[OrderLine], need: Need) -> CheckResult:
    res = CheckResult()
    mandate = buyer["mandate"]
    if not lines:
        res.error("no_lines", "Order has no lines")
        return res
    need, _ = effective_need(buyer, need)       # Valio enforces labels from the query + customer allergen list
    digest = need.digest()
    offers = []
    for i, line in enumerate(lines):
        try:
            facts = verify_offer(line.offer_id, buyer["buyer_id"])
        except OfferError as e:
            res.error("offer_invalid", str(e), i)
            continue
        if facts["need_digest"] != digest:
            res.error("need_mismatch", "This offer was issued for a different declared need. Pass the same need you "
                      "gave find_offers, or call find_offers again with the new need.", i)
            continue
        offers.append((i, line, facts))
    if res.errors:
        return res

    if need.unresolved:
        res.flag("agent_unresolved")
    pol = mandate.get("allergen_policy") or {}
    if not pol.get("confirmed_at"):
        res.flag("allergen_policy_unconfirmed")
        res.notes.append("This customer's allergen policy has not been confirmed yet")

    codes = [f["product_code"] for _, _, f in offers]
    if len(set(codes)) != len(codes):
        res.error("duplicate_product", "The same product appears on two lines; merge them into one line")
        return res

    products = catalog.get_products(codes)
    try:
        live = await price_and_availability(codes, buyer.get("customer_number", buyer["buyer_id"]), need.deliver_by, fresh=True)
    except ValioAPIError as e:
        res.error("live_data_unavailable", f"Could not re-check live price and stock: {e}. Retry shortly.")
        return res

    total_measure, largest_step_measure = 0.0, 0.0
    seen_products: list[str] = []
    for i, line, facts in offers:
        code = facts["product_code"]
        p = products[code]
        name = catalog.display_name(p)
        lv = live[code]
        a, pr = lv["availability"], lv["price"]
        line_flags: list[str] = []

        # quantity step (allowedLotSize)
        step = catalog.order_step(p)
        if abs(line.quantity / step - round(line.quantity / step)) > 1e-6:
            sug = _qty_suggestions(line.quantity, step)
            res.error("lot_size", f"Quantity {fmt_qty(line.quantity)} of {name} is not a multiple of {fmt_qty(step)}. "
                      f"Try {' or '.join(fmt_qty(s) for s in sug)}.", i, suggestions=sug, step=step)

        # labels and allergens vs the declared need
        policy_allergens = set((mandate.get("allergen_policy") or {}).get("exclude") or [])
        missing = [l for l in need.required_labels if not catalog.label_ok(p, l)]
        if missing:
            res.error("label_mismatch", f"{name} does not carry required label(s) {missing}", i)
        for g in need.exclude_allergens:
            s = catalog.allergen_status(p, g)
            if s in ("contains", "may_contain"):
                src = "the customer's allergen policy" if g in policy_allergens else "the need"
                res.error("allergen_conflict", f"{name} {s.replace('_', ' ')} {g}, which {src} excludes. Choose another offer.", i)
            elif s == "unknown":
                line_flags.append(f"allergen_unknown:{g}")

        # live availability re-check
        if not pr.get("found"):
            res.reject_reasons.append(f"no price for {name}")
        if a.get("sales_block"):
            res.reject_reasons.append(f"{name} has a sales block")
        if not a.get("deliverable"):
            res.reject_reasons.append(f"{name} is not deliverable ({a.get('reason', 'no schedule')})")
        avail = a.get("available_units")
        if a.get("stock_known") and avail is not None and line.quantity > avail + 1e-9:
            res.error("insufficient_stock", f"Only {fmt_qty(avail)} {p['sales_unit']} of {name} available by "
                      f"{need.deliver_by}. Reduce this line to {fmt_qty(round_down(avail, step))} and cover the rest with a substitute.",
                      i, available_units=avail)
        if a.get("logistic_model") == "Terminal":
            line_flags.append("supplier_stock_unknown")
        if a.get("later_than_requested"):
            line_flags.append("later_than_requested")
        if a.get("cutoff_passed"):
            line_flags.append("order_cutoff_passed")
        rel = p.get("reliability")
        if rel is not None and rel < mandate.get("min_delivery_reliability", config.LOW_RELIABILITY):
            line_flags.append("low_reliability")
        if a.get("scenario_override") or pr.get("scenario_override"):
            line_flags.append("demo_scenario_override")

        # price changed between offer and order?
        offered = facts["unit_price_eur"]
        unit_price = pr.get("unit_price_eur", offered) if pr.get("found") else offered
        tol = mandate.get("price_increase_tolerance_pct", 0) / 100
        if unit_price > offered * (1 + tol) + 1e-9:
            line_flags.append("price_changed")
            res.price_changes.append({"product_code": code, "offered_eur": offered, "now_eur": unit_price})
        elif abs(unit_price - offered) > 1e-9:
            res.notes.append(f"Price of {name} changed from {offered:.2f} to {unit_price:.2f} EUR; the order uses the new price")

        # substitution: signed role, an official substitute of an earlier line, or the agent says so
        sub_for = [c for c in seen_products if code in catalog.official_substitutes(c)]
        is_sub = facts["role"] == "official_substitute" or bool(sub_for) or bool(line.is_substitution)
        if line.is_substitution is False and (facts["role"] == "official_substitute" or sub_for):
            res.notes.append(f"Line {i + 1} ({name}) is treated as a substitution although the agent did not mark it")
        seen_products.append(code)

        # unusual quantity vs this buyer's order history
        avg, n = _history_avg(buyer["buyer_id"], code)
        factor = mandate.get("unusual_quantity_factor", 5)
        if n >= 2 and line.quantity > factor * avg:
            line_flags.append("unusual_quantity")
            res.notes.append(f"{fmt_qty(line.quantity)} {p['sales_unit']} of {name} is {line.quantity / avg:.1f}x this buyer's "
                             f"average of {fmt_qty(avg)} over {n} orders")

        measure = resolve_measure(p, pr, need.unit)
        per = measure.get("per_sales_unit")
        if per is None:
            res.error("unit_conversion_unknown", f"Cannot convert {name} ({p['sales_unit']}) to {need.unit}", i)
            per = 0.0
        total_measure += line.quantity * per
        largest_step_measure = max(largest_step_measure, step * per)
        closing = a.get("closing_time")
        for f in line_flags:
            res.flag(f)
        res.lines.append({
            "line": i + 1, "offer_ref": short_ref(line.offer_id), "product_code": code, "name": name,
            "quantity": line.quantity, "sales_unit": p["sales_unit"], "unit_price_eur": unit_price,
            "offered_unit_price_eur": offered, "vat_rate_pct": pr.get("vat_rate_pct"),
            "line_total_eur": round(line.quantity * unit_price, 2),
            "measure_per_unit": per, "covers": round(line.quantity * per, 3),
            "is_substitution": is_sub, "substitute_for": facts.get("substitute_for") or sub_for or None,
            "available_date": a.get("available_date"), "closing_time": closing,
            "logistic_model": a.get("logistic_model"), "delivery_reliability": rel,
            "carbon_kg_co2e_per_kg": p.get("carbon_kg_per_kg"), "flags": line_flags,
        })

    # do the lines add up to the need?
    if not res.errors:
        q = need.quantity
        unit = UNIT_LABEL[need.unit]
        if total_measure < q - 1e-6:
            if need.preferences.accept_partial:
                res.flag("partial_fulfilment")
                res.notes.append(f"Covers {fmt_qty(total_measure)} of {fmt_qty(q)} {unit} (buyer accepts partial)")
            else:
                res.error("quantity_short", f"Lines add up to {fmt_qty(total_measure)} {unit} but the need is {fmt_qty(q)} {unit} "
                          f"(short by {fmt_qty(q - total_measure)}). Add a line or a substitute, or set preferences.accept_partial.",
                          covered=round(total_measure, 3))
        elif total_measure > q + max(largest_step_measure, 0.02 * q) + 1e-6:
            res.error("quantity_over", f"Lines add up to {fmt_qty(total_measure)} {unit}, far over the need of {fmt_qty(q)} {unit}. "
                      "Reduce quantities.", covered=round(total_measure, 3))
    res.covered = total_measure
    res.need = need
    return res


def summarise(need: Need, lines: list[dict]) -> str:
    """Plain-language 'agent understood' text."""
    parts = []
    for l in lines:
        s = f"{fmt_qty(l['quantity'])} x {l['name']} ({l['product_code']})"
        if l["is_substitution"]:
            s += ", Valio's official substitute"
        parts.append(s)
    total = sum(l["line_total_eur"] for l in lines)
    covered = sum(l["covers"] for l in lines)
    d = date.fromisoformat(min(l["available_date"] for l in lines if l.get("available_date"))) if any(l.get("available_date") for l in lines) else need.deliver_by
    labels = f" {', '.join(need.required_labels)}" if need.required_labels else ""
    excl = ""
    if need.exclude_allergens:
        unknown = sorted({f.split(":", 1)[1] for l in lines for f in l.get("flags", []) if f.startswith("allergen_unknown:")})
        verified = [g for g in need.exclude_allergens if g not in unknown]
        if verified:
            excl += f", verified free of {', '.join(verified)}"
        if unknown:   # never claim safety the data does not prove
            excl += f", must exclude {', '.join(unknown)} but allergen data is missing"
    return (f"{' + '.join(parts)} = {fmt_qty(covered)} {UNIT_LABEL[need.unit]}{labels} {need.query}{excl}, "
            f"delivery {d.strftime('%a %d.%m.')}, EUR {total:,.2f} excl. VAT")
