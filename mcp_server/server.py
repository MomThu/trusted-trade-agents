"""MCP server: the only door into Valio for external buyer agents.

Agent tools (JSON in, JSON out; rules decide, no AI decides):
  get_need_schema   what a declared need may contain (labels, allergens, units)
  find_offers       look: catalog filter -> live price + stock -> signed offers + suggested plan
  place_order       buy: request check -> policy -> placed | pending_approval | rejected
  get_order_status  poll an order
  get_audit_log     this buyer's audit trail

Human-only actions (approve, reject, cancel) are NOT tools; they live in the UI backend.

Auth: one API key per customer. HTTP: `Authorization: Bearer <key>` (or `X-API-Key`).
stdio: env BUYER_API_KEY. buyer_id is derived from the key, never taken from input.

Run:  python -m mcp_server.server            # stdio (Claude Desktop, MCP Inspector, ...)
      uvicorn ui.app:app                      # HTTP at http://localhost:8000/mcp (with the UI)
"""
from __future__ import annotations

import os
from typing import Annotated

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from data.vocab import ALLERGENS, CERTIFICATE_LABELS, LABELS
from order_service import config, orders
from order_service.models import Need, OrderLine
from order_service.offers import find_offers as _find_offers

INSTRUCTIONS = """You are connected to Valio Aimo's trade interface for professional kitchens.
Workflow: (1) translate the buyer's words into a declared `need` (see get_need_schema);
(2) call find_offers(need); (3) call place_order(lines, need) with the SAME need, usually
passing suggested_plan.lines as-is; (4) report the outcome honestly. If place_order returns
errors, fix exactly what they say and retry. pending_approval means a human must sign off:
say so, do not claim the order is placed. If you could not express or verify part of the buyer's
request, say so in need.unresolved: the order then goes to a human instead of being guessed. Never claim a product is allergen-safe unless its
allergen status is 'free_from'. Prices are fixed (no negotiation) and exclude VAT."""

mcp = FastMCP(
    "valio-aimo-trade",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        # DNS-rebinding protection guards servers meant to be reachable only from this machine. On a public cloud
        # host whose hostname is not configured, customer API keys are the protection, so it is switched off.
        enable_dns_rebinding_protection=not (config.PUBLIC_DEPLOY and not config.PUBLIC_HOSTS),
        allowed_hosts=["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
                      + [h for host in config.PUBLIC_HOSTS for h in (host, f"{host}:*")],
        allowed_origins=["http://localhost:*", "http://127.0.0.1:*"] + [f"https://{h}" for h in config.PUBLIC_HOSTS],
    ),
)


class AuthError(Exception):
    pass


def buyer_from_ctx(ctx: Context | None) -> dict:
    key = None
    req = getattr(getattr(ctx, "request_context", None), "request", None) if ctx else None
    if req is not None:
        auth = req.headers.get("authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else req.headers.get("x-api-key")
    else:
        key = os.getenv("BUYER_API_KEY")
    buyer = config.buyer_for_key(key)
    if not buyer:
        raise AuthError("Unknown or missing buyer API key")
    return buyer


def _auth_error(e: Exception) -> dict:
    return {"error": str(e), "hint": "Send your customer API key as 'Authorization: Bearer <key>'"}



def _inline_refs(schema: dict) -> dict:
    defs = schema.get("$defs", {})

    def walk(n):
        if isinstance(n, dict):
            if "$ref" in n:
                return walk(defs[n["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in n.items() if k != "$defs"}
        return [walk(x) for x in n] if isinstance(n, list) else n
    return walk(schema)


@mcp.tool()
def get_need_schema() -> dict:
    """Describe the declared-need format: required fields, supported labels, allergen groups and units.
    Call once before find_offers if unsure how to express the buyer's requirements."""
    return {
        "need_json_schema": _inline_refs(Need.model_json_schema()),
        "required": ["text", "query", "quantity", "unit", "deliver_by"],
        "units": {"L": "litres", "KG": "kilograms", "SALES_UNIT": "count of sales units (cartons, pieces...)"},
        "labels": sorted(LABELS), "certificate_labels": sorted(CERTIFICATE_LABELS),
        "allergen_groups": sorted(ALLERGENS),
        "priorities": ["price", "reliability", "carbon"],
        "example": {"text": "Need 1,000 L lactose-free milk by Monday", "query": "milk", "quantity": 1000, "unit": "L",
                    "deliver_by": "2026-11-16", "required_labels": ["lactose-free"], "exclude_allergens": [],
                    "preferences": {"priority": "price", "allow_substitutes": True}},
        "notes": ["query = product words only (fi/sv/en), e.g. 'milk', 'maito', 'whipping cream'",
                  "Requirements outside this schema ('the one the chef likes', 'fresh') cannot be checked by Valio: keep them "
                  "in `text` and describe your doubt in `unresolved`; such orders always go to a human approver",
                  "Allergen status 'unknown' means no manufacturer statement: never treat it as safe"],
    }


@mcp.tool()
async def find_offers(need: Need, ctx: Context) -> dict:
    """Look before you buy. Filters Valio's catalog by the declared need, fetches LIVE price and
    availability for the top candidates only, and returns signed offers (valid 15 min) with trust
    flags, provenance, and a rule-based suggested_plan that covers the quantity (splitting across
    Valio's official substitutes if stock is short). Nothing is ordered."""
    try:
        buyer = buyer_from_ctx(ctx)
    except AuthError as e:
        return _auth_error(e)
    return await _find_offers(buyer, need)


@mcp.tool()
async def place_order(
    lines: Annotated[list[OrderLine], Field(description="One or more {offer_id, quantity} lines; quantity in sales units")],
    need: Annotated[Need, Field(description="The SAME declared need that was passed to find_offers")],
    ctx: Context,
) -> dict:
    """Order at the offered fixed prices. Runs the request check (offer signature/expiry, labels and
    allergens vs need, lot size, lines add up to the need, live stock/price re-check, history) and then
    the buyer's policy. Returns status placed (routine, cancellable until cut-off), pending_approval
    (a human must sign off) or rejected, or errors that say exactly what to fix."""
    try:
        buyer = buyer_from_ctx(ctx)
    except AuthError as e:
        return _auth_error(e)
    return await orders.place_order(buyer, lines, need)


@mcp.tool()
def get_order_status(order_id: str, ctx: Context) -> dict:
    """Current status of one of this buyer's orders (placed, pending_approval, approved, rejected, cancelled)."""
    try:
        buyer = buyer_from_ctx(ctx)
    except AuthError as e:
        return _auth_error(e)
    o = orders.get_order(order_id, buyer["buyer_id"])
    if not o:
        return {"error": f"No order {order_id} for this buyer"}
    return {k: o[k] for k in ("order_id", "status", "total_eur", "summary", "flags", "policy_reasons", "cancel_until",
                              "cancellable", "decided_by", "decision_note", "lines", "created_at", "updated_at")}


@mcp.tool()
def get_audit_log(ctx: Context, order_id: str | None = None, limit: int = 50) -> dict:
    """Human-readable record of what this buyer's agent did and why (optionally for one order)."""
    try:
        buyer = buyer_from_ctx(ctx)
    except AuthError as e:
        return _auth_error(e)
    rows = orders.audit_log(buyer["buyer_id"], order_id, min(limit, 200))
    return {"buyer_id": buyer["buyer_id"], "entries": [{k: r[k] for k in ("ts", "order_id", "actor", "event", "message")} for r in rows]}


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
