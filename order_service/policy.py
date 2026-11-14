"""Gate 2, the policy: is the agent allowed to do this?

Judges the whole order (not single lines), so a large order cannot be split to
slip under the limit. Pure function of (order facts, buyer mandate) -> outcome.
"""
from __future__ import annotations

RISK_FLAGS = {
    "supplier_stock_unknown": "supplier stock unknown (Terminal product)",
    "later_than_requested": "delivery later than requested",
    "low_reliability": "delivery reliability below the buyer's minimum",
    "unusual_quantity": "quantity unusual vs. order history",
    "price_changed": "price rose after the offer was made",
    "order_cutoff_passed": "order cut-off (closingTime) already passed",
    "partial_fulfilment": "covers less than the full need",
    "possible_misunderstanding": "AI cross-check suspects the order differs from the buyer's words",
    "demo_scenario_override": None,   # informational only, never escalates
}


def evaluate(total_eur: float, lines: list[dict], flags: list[str], reject_reasons: list[str], mandate: dict) -> tuple[str, list[dict]]:
    """Return (outcome, reasons). outcome: rejected | pending_approval | placed."""
    if reject_reasons:
        return "rejected", [{"code": "not_orderable", "message": r} for r in reject_reasons]

    reasons: list[dict] = []
    limit = mandate.get("spend_limit_eur")
    if limit is not None and total_eur > limit:
        reasons.append({"code": "over_spend_limit", "message": f"Order total EUR {total_eur:,.2f} exceeds the agent's limit of EUR {limit:,.2f}"})
    subs = [l for l in lines if l.get("is_substitution")]
    if subs and mandate.get("substitutions_need_approval", True):
        reasons.append({"code": "substitution", "message": "Contains substitution(s): " + ", ".join(l["name"] for l in subs)})
    unknown = sorted({f.split(":", 1)[1] for f in flags if f.startswith("allergen_unknown:")})
    if unknown and mandate.get("allergen_strict"):
        reasons.append({"code": "allergen_data_missing", "message": f"Allergen-strict buyer, but allergen data is missing for: {', '.join(unknown)}. "
                        "The agent may not claim these products are safe."})
    for f in flags:
        if f == "supplier_stock_unknown" and not mandate.get("terminal_products_need_approval", True):
            continue
        msg = RISK_FLAGS.get(f)
        if msg:
            reasons.append({"code": f, "message": msg})
    if reasons:
        return "pending_approval", reasons
    lim = f"limit EUR {limit:,.2f}" if limit is not None else "no spend limit"
    return "placed", [{"code": "within_mandate", "message": f"Within mandate ({lim}), no risk flags"}]
