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
    "allergen_policy_unconfirmed": None,
    "agent_unresolved": None,          # reason added above with the agent's own words
}


def evaluate(total_eur: float, lines: list[dict], flags: list[str], reject_reasons: list[str], mandate: dict,
             unresolved: str | None = None, agent_spent_24h: float = 0.0) -> tuple[str, list[dict]]:
    """Return (outcome, reasons). outcome: rejected | pending_approval | placed."""
    if reject_reasons:
        return "rejected", [{"code": "not_orderable", "message": r} for r in reject_reasons]

    reasons: list[dict] = []
    if unresolved:   # fixed rule: the agent itself declared a doubt -> a human decides
        reasons.append({"code": "agent_unresolved", "message": f"Agent could not resolve: {unresolved}"})
    limit = mandate.get("spend_limit_eur")
    if limit is not None and total_eur > limit:
        reasons.append({"code": "over_spend_limit", "message": f"Order total EUR {total_eur:,.2f} exceeds the agent's limit of EUR {limit:,.2f}"})
    elif limit is not None and agent_spent_24h + total_eur > limit:
        # Splitting must not slip under the limit: the limit caps what the agent places ON ITS OWN in 24 h.
        # Orders a human approved do not use up the agent's budget.
        reasons.append({"code": "over_spend_limit_24h", "message":
                        f"Together with EUR {agent_spent_24h:,.2f} the agent already placed on its own in the last 24 h, "
                        f"the total EUR {agent_spent_24h + total_eur:,.2f} exceeds the agent's limit of EUR {limit:,.2f}"})
    subs = [l for l in lines if l.get("is_substitution")]
    if subs and mandate.get("substitutions_need_approval", True):
        reasons.append({"code": "substitution", "message": "Contains substitution(s): " + ", ".join(l["name"] for l in subs)})
    unknown = sorted({f.split(":", 1)[1] for f in flags if f.startswith("allergen_unknown:")})
    if unknown:   # fixed rule: no manufacturer statement is never treated as safe
        policy = set((mandate.get("allergen_policy") or {}).get("exclude") or [])
        src = [f"{g} ({'customer allergen policy' if g in policy else 'this order'})" for g in unknown]
        reasons.append({"code": "allergen_data_missing", "message": f"No manufacturer allergen statement for: {', '.join(src)}. "
                        "The agent may not claim these products are safe."})
    for f in flags:   # fixed rules, not configurable: e.g. unknown supplier stock always needs a human
        msg = RISK_FLAGS.get(f)
        if msg:
            reasons.append({"code": f, "message": msg})
    if reasons:
        return "pending_approval", reasons
    lim = f"limit EUR {limit:,.2f}" if limit is not None else "no spend limit"
    return "placed", [{"code": "within_mandate", "message": f"Within mandate ({lim}), no risk flags"}]
