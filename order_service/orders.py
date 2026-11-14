"""Order service: place_order -> request check -> policy -> stored outcome.

States: placed | pending_approval | approved | rejected | cancelled.
approved means placed after human sign-off. Routine (placed) orders stay
cancellable until the earliest closingTime of their lines.

Agent-facing: place_order, get_order_status, get_audit_log (buyer-scoped).
Human-only: approve, reject, cancel (UI backend, never agent tools).
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta

from order_service import config, db
from order_service.checks import request_check, summarise
from order_service.models import Need, OrderLine
from order_service.policy import evaluate
from order_service.valio_client import ValioAPIError, now, parse_ts, price_and_availability


def _order_public(o: dict) -> dict:
    if not o:
        return o
    o = dict(o)
    cu = parse_ts(o.get("cancel_until"))
    o["cancellable"] = o["status"] in ("placed", "approved", "pending_approval") and (o["status"] == "pending_approval" or bool(cu and cu > now()))
    return o


def agent_spent_24h(buyer_id: str) -> float:
    """What the agent placed autonomously (no human decision) in the last 24 h, not cancelled."""
    since = (now() - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = db.query("SELECT total_eur, created_at FROM orders WHERE buyer_id=? AND status='placed' AND decided_by IS NULL", (buyer_id,))
    return round(sum(r["total_eur"] or 0 for r in rows if (parse_ts(r["created_at"]) or now()) >= parse_ts(since)), 2)


async def place_order(buyer: dict, lines: list[OrderLine], need: Need) -> dict:
    actor = f"agent:{buyer['buyer_id']}"
    res = await request_check(buyer, lines, need)
    if res.errors:
        db.audit("request_check_failed", "; ".join(e["message"] for e in res.errors), buyer_id=buyer["buyer_id"], actor=actor,
                 data={"errors": res.errors, "need": need.model_dump(mode="json"),
                       "lines": [l.model_dump() for l in lines]})
        return {"status": "error", "errors": res.errors,
                "hint": "Nothing was ordered. Fix the listed problems and call place_order again "
                        "(or find_offers again if an offer expired)."}

    total = round(sum(l["line_total_eur"] for l in res.lines), 2)
    summary = summarise(res.need, res.lines)

    if config.AI_CROSSCHECK:
        from order_service.crosscheck import crosscheck
        from order_service.checks import _history_avg
        hist = []
        for l in res.lines:
            _, n = _history_avg(buyer["buyer_id"], l["product_code"])
            hist.append(f"{l['product_code']} ordered by this buyer {n} time(s) before")
        context = (f"offers were ranked by {res.need.preferences.priority} (best first) among products meeting every "
                   f"declared requirement; " + "; ".join(hist))
        verdict = await crosscheck(res.need, summary, context)
        if verdict:
            res.flag("possible_misunderstanding")
            res.notes.append(f"AI cross-check: {verdict}")

    outcome, reasons = evaluate(total, res.lines, res.flags, res.reject_reasons, buyer["mandate"], res.need.unresolved,
                                agent_spent_24h(buyer["buyer_id"]))
    closings = [parse_ts(l["closing_time"]) for l in res.lines if l.get("closing_time")]
    open_closings = [c for c in closings if c and c > now()]
    cancel_until = min(open_closings).isoformat() if open_closings and len(open_closings) == len(closings) else None

    order_id = f"ord_{datetime.now():%m%d}_{secrets.token_hex(3)}"
    ts = db.now_iso()
    notes = res.notes + ([f"Price change: {json.dumps(res.price_changes)}"] if res.price_changes else [])
    db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (order_id, buyer["buyer_id"], outcome, ts, ts, json.dumps(res.need.model_dump(mode="json"), ensure_ascii=False),
                json.dumps(res.lines, ensure_ascii=False), total, json.dumps(res.flags), json.dumps(reasons),
                summary, cancel_until, None, None, None, json.dumps({"notes": notes, "price_changes": res.price_changes})))
    reason_text = "; ".join(r["message"] for r in reasons)
    db.audit("order_" + outcome, f"{summary}. Outcome: {outcome.replace('_', ' ')} ({reason_text})",
             buyer_id=buyer["buyer_id"], actor="policy", order_id=order_id,
             data={"buyer_said": need.text, "agent_understood": summary, "flags": res.flags, "reasons": reasons,
                   "notes": notes, "total_eur": total, "mandate": buyer["mandate"]})
    for n in res.notes:
        db.audit("check_note", n, buyer_id=buyer["buyer_id"], actor="request_check", order_id=order_id)

    out = {
        "status": outcome, "order_id": order_id, "total_eur": total, "currency": "EUR", "prices": "excl. VAT",
        "agent_understood": summary, "buyer_said": need.text, "flags": res.flags, "reasons": reasons, "notes": notes,
        "lines": res.lines,
    }
    if outcome == "placed":
        out["next"] = (f"Order placed. The buyer gets a confirmation and can cancel until {cancel_until}." if cancel_until
                       else "Order placed. Cut-off already passed, so it cannot be cancelled.")
    elif outcome == "pending_approval":
        out["next"] = "Stored, NOT placed. A human must approve it in the approval UI. Poll get_order_status."
    else:
        out["next"] = "Rejected. Choose another offer and call place_order again."
    return out


def get_order(order_id: str, buyer_id: str | None = None) -> dict | None:
    sql, args = "SELECT * FROM orders WHERE order_id=?", [order_id]
    if buyer_id:
        sql += " AND buyer_id=?"
        args.append(buyer_id)
    r = db.query(sql, tuple(args))
    return _order_public(r[0]) if r else None


def list_orders(buyer_id: str | None = None, status: str | None = None, limit: int = 200) -> list[dict]:
    sql, args = "SELECT * FROM orders WHERE 1=1", []
    if buyer_id:
        sql += " AND buyer_id=?"
        args.append(buyer_id)
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    return [_order_public(o) for o in db.query(sql, tuple(args))]


def audit_log(buyer_id: str | None = None, order_id: str | None = None, limit: int = 300) -> list[dict]:
    sql, args = "SELECT * FROM audit WHERE 1=1", []
    if buyer_id:
        sql += " AND buyer_id=?"
        args.append(buyer_id)
    if order_id:
        sql += " AND order_id=?"
        args.append(order_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return db.query(sql, tuple(args))


class TransitionError(ValueError):
    pass


async def approve(order_id: str, approver: str, remove_lines: list[int] | None = None, note: str | None = None) -> dict:
    """Human sign-off. Approved = placed, so first re-check what may have changed while the order waited:
    order cut-off, live stock and live price. If anything blocks it, the order stays pending and the
    approver is told why (a higher price is written onto the order so it can be approved knowingly)."""
    o = get_order(order_id)
    if not o:
        raise TransitionError("order not found")
    if o["status"] != "pending_approval":
        raise TransitionError(f"order is {o['status']}, only pending_approval can be approved")
    lines = o["lines"]
    removed = []
    if remove_lines:
        removed = [l for l in lines if l["line"] in remove_lines]
        lines = [l for l in lines if l["line"] not in remove_lines]
        if not lines:
            raise TransitionError("cannot remove every line; reject the order instead")

    # 1. order cut-off: after closingTime the delivery date can no longer be met
    late = [l for l in lines if l.get("closing_time") and parse_ts(l["closing_time"]) and parse_ts(l["closing_time"]) <= now()]
    if late:
        raise TransitionError("Too late to place: the order cut-off passed for " + ", ".join(
            f"{l['name']} ({parse_ts(l['closing_time']):%a %d.%m. %H:%M})" for l in late) +
            ". Reject it and let the agent order again for a later delivery date.")

    # 2. live stock and price, fresh
    buyer = config.load_buyers().get(o["buyer_id"], {})
    deliver_by = datetime.fromisoformat(o["need"]["deliver_by"]).date()
    try:
        live = await price_and_availability([l["product_code"] for l in lines], buyer.get("customer_number", o["buyer_id"]),
                                            deliver_by, fresh=True)
    except ValioAPIError as e:
        raise TransitionError(f"Cannot re-check live price and stock right now ({e}); try again in a moment")
    problems, rises, notes = [], [], []
    for l in lines:
        a, pr = live[l["product_code"]]["availability"], live[l["product_code"]]["price"]
        if not pr.get("found") or not a.get("deliverable") or a.get("sales_block"):
            problems.append(f"{l['name']} is no longer orderable ({a.get('reason') or 'no price or sales block'})")
            continue
        avail = a.get("available_units")
        if a.get("stock_known") and avail is not None and l["quantity"] > avail + 1e-9:
            problems.append(f"only {avail:g} {l['sales_unit']} of {l['name']} left (order has {l['quantity']:g})")
        new = pr["unit_price_eur"]
        if new > l["unit_price_eur"] + 1e-9:
            rises.append((l, new))
        elif new < l["unit_price_eur"] - 1e-9:
            notes.append(f"{l['name']} price fell {l['unit_price_eur']:.2f} -> {new:.2f} EUR; using the new price")
            l["unit_price_eur"], l["line_total_eur"] = new, round(l["quantity"] * new, 2)
    if problems:
        db.audit("approval_blocked", "Approval blocked: " + "; ".join(problems), buyer_id=o["buyer_id"],
                 actor=f"human:{approver}", order_id=order_id)
        raise TransitionError("Cannot place: " + "; ".join(problems) + ". Reject it, or let the agent re-plan.")
    if rises:
        for l, new in rises:          # write the new price onto the pending order so the approver sees it
            l.setdefault("offered_unit_price_eur", l["unit_price_eur"])
            l["unit_price_eur"], l["line_total_eur"] = new, round(l["quantity"] * new, 2)
            if "price_changed" not in l["flags"]:
                l["flags"].append("price_changed")
        all_lines = [x for x in o["lines"] if x["line"] not in {r["line"] for r in lines}] + lines
        all_lines.sort(key=lambda x: x["line"])
        new_total = round(sum(x["line_total_eur"] for x in all_lines), 2)
        flags = sorted(set(o["flags"] or []) | {"price_changed"})
        db.execute("UPDATE orders SET lines=?, total_eur=?, flags=?, updated_at=? WHERE order_id=?",
                   (json.dumps(all_lines, ensure_ascii=False), new_total, json.dumps(flags), db.now_iso(), order_id))
        msg = "; ".join(f"{l['name']} {l['offered_unit_price_eur']:.2f} -> {new:.2f} EUR" for l, new in rises)
        db.audit("approval_blocked", f"Price rose while the order waited: {msg}. Order updated, still pending.",
                 buyer_id=o["buyer_id"], actor=f"human:{approver}", order_id=order_id)
        raise TransitionError(f"Price rose while the order waited ({msg}). The order shows the new total "
                              f"EUR {new_total:,.2f}; review and approve again if it is still OK.")

    total = round(sum(l["line_total_eur"] for l in lines), 2)
    closings = [parse_ts(l["closing_time"]) for l in lines if l.get("closing_time")]
    open_c = [c for c in closings if c and c > now()]
    cancel_until = min(open_c).isoformat() if open_c and len(open_c) == len(closings) else None
    db.execute("UPDATE orders SET status='approved', lines=?, total_eur=?, cancel_until=?, decided_by=?, decided_at=?, decision_note=?, updated_at=? WHERE order_id=?",
               (json.dumps(lines, ensure_ascii=False), total, cancel_until, approver, db.now_iso(), note, db.now_iso(), order_id))
    msg = f"Approved by {approver} and placed (EUR {total:,.2f}); live price, stock and cut-off re-checked"
    if removed:
        msg += f"; removed line(s): {', '.join(l['name'] for l in removed)}"
    if notes:
        msg += "; " + "; ".join(notes)
    if note:
        msg += f". Note: {note}"
    db.audit("order_approved", msg, buyer_id=o["buyer_id"], actor=f"human:{approver}", order_id=order_id,
             data={"removed_lines": removed, "total_eur": total})
    return get_order(order_id)


def reject(order_id: str, approver: str, note: str | None = None) -> dict:
    o = get_order(order_id)
    if not o:
        raise TransitionError("order not found")
    if o["status"] != "pending_approval":
        raise TransitionError(f"order is {o['status']}, only pending_approval can be rejected")
    db.execute("UPDATE orders SET status='rejected', decided_by=?, decided_at=?, decision_note=?, updated_at=? WHERE order_id=?",
               (approver, db.now_iso(), note, db.now_iso(), order_id))
    db.audit("order_rejected", f"Rejected by {approver}" + (f": {note}" if note else ""), buyer_id=o["buyer_id"],
             actor=f"human:{approver}", order_id=order_id)
    return get_order(order_id)


def cancel(order_id: str, by: str, note: str | None = None) -> dict:
    o = get_order(order_id)
    if not o:
        raise TransitionError("order not found")
    if not o["cancellable"]:
        raise TransitionError(f"order is {o['status']} and past its cancel window ({o.get('cancel_until') or 'cut-off passed'})")
    db.execute("UPDATE orders SET status='cancelled', decided_by=?, decided_at=?, decision_note=?, updated_at=? WHERE order_id=?",
               (by, db.now_iso(), note, db.now_iso(), order_id))
    db.audit("order_cancelled", f"Cancelled by {by}" + (f": {note}" if note else ""), buyer_id=o["buyer_id"],
             actor=f"human:{by}", order_id=order_id)
    return get_order(order_id)
