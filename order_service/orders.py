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
from datetime import datetime

from order_service import config, db
from order_service.checks import request_check, summarise
from order_service.models import Need, OrderLine
from order_service.policy import evaluate
from order_service.valio_client import now, parse_ts


def _order_public(o: dict) -> dict:
    if not o:
        return o
    o = dict(o)
    cu = parse_ts(o.get("cancel_until"))
    o["cancellable"] = o["status"] in ("placed", "approved", "pending_approval") and (o["status"] == "pending_approval" or bool(cu and cu > now()))
    return o


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

    outcome, reasons = evaluate(total, res.lines, res.flags, res.reject_reasons, buyer["mandate"], res.need.unresolved)
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


def approve(order_id: str, approver: str, remove_lines: list[int] | None = None, note: str | None = None) -> dict:
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
    total = round(sum(l["line_total_eur"] for l in lines), 2)
    closings = [parse_ts(l["closing_time"]) for l in lines if l.get("closing_time")]
    open_c = [c for c in closings if c and c > now()]
    cancel_until = min(open_c).isoformat() if open_c and len(open_c) == len(closings) else None
    db.execute("UPDATE orders SET status='approved', lines=?, total_eur=?, cancel_until=?, decided_by=?, decided_at=?, decision_note=?, updated_at=? WHERE order_id=?",
               (json.dumps(lines, ensure_ascii=False), total, cancel_until, approver, db.now_iso(), note, db.now_iso(), order_id))
    msg = f"Approved by {approver} and placed (EUR {total:,.2f})"
    if removed:
        msg += f"; removed line(s): {', '.join(l['name'] for l in removed)}"
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
