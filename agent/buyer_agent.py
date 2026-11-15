"""Reference buyer agent (external, "any vendor"). Talks to Valio ONLY through MCP.

Two modes:
  LLM (Gemini): turns the buyer's plain words into a declared need and tool calls.
  Scripted:     a deterministic agent for reproducible demos and tests; it plays a
                basic agent that accepts Valio's suggested plan and fixes errors
                from the precise messages the request check returns.

    python -m agent.buyer_agent --key demo-hotel-aurora-key "Need 1,000 L lactose-free milk by Monday"
    python -m agent.buyer_agent --preset hotel-milk-partial
    python -m agent.buyer_agent --list-presets
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parent.parent
PRESETS_FILE = ROOT / "demo" / "presets.json"
DEFAULT_URL = os.getenv("VALIO_MCP_URL", "http://localhost:8000/mcp")

Event = Callable[[dict], None]


def _print_event(e: dict) -> None:
    kind = e["kind"]
    if kind == "say":
        print(f"\n[{e.get('who', 'agent')}] {e['text']}")
    elif kind == "tool_call":
        print(f"\n-> {e['tool']}({json.dumps(e['args'], ensure_ascii=False)[:600]})")
    elif kind == "tool_result":
        print(f"<- {e['tool']}: {e['brief']}")
    else:
        print(f"[{kind}] {e}")


def brief(tool: str, r: dict) -> str:
    """One-line digest of a tool result for transcripts."""
    if not isinstance(r, dict):
        return str(r)[:200]
    if r.get("error"):
        return f"ERROR {r['error']}"
    if tool == "find_offers":
        plan = r.get("suggested_plan") or {}
        lines = ", ".join(f"{l['quantity']:g} x {l['product_code']}{' (substitute)' if l['is_substitution'] else ''}" for l in plan.get("lines", []))
        return f"{len(r.get('offers', []))} offers; plan: {plan.get('covers')} = {lines or 'none'}, EUR {plan.get('total_eur')}"
    if tool == "place_order":
        if r.get("status") == "error":
            return "REQUEST CHECK FAILED: " + " | ".join(e["message"] for e in r["errors"])
        return f"{r['status'].upper()} {r.get('order_id')} EUR {r.get('total_eur')}: " + "; ".join(x["message"] for x in r.get("reasons", []))
    if tool == "get_order_status":
        return f"{r.get('status')} (cancel until {r.get('cancel_until')})"
    return json.dumps(r, ensure_ascii=False)[:200]


async def _call(session: ClientSession, tool: str, args: dict, on_event: Event) -> dict:
    on_event({"kind": "tool_call", "tool": tool, "args": copy.deepcopy(args)})  # snapshot: args are mutated on retry
    res = await session.call_tool(tool, args)
    data: Any = res.structuredContent
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    if data is None:
        text = "".join(getattr(c, "text", "") for c in res.content)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {"error": text} if res.isError else {"text": text}
    on_event({"kind": "tool_result", "tool": tool, "brief": brief(tool, data), "result": data})
    return data


# ---------------------------------------------------------------- scripted agent

def load_presets() -> dict:
    data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
    return {p["id"]: p for p in data["presets"]}


def next_weekday(name: str, today: date | None = None) -> str:
    today = today or date.today()
    idx = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"].index(name.lower())
    return (today + timedelta(days=(idx - today.weekday()) % 7 or 7)).isoformat()


def preset_need(p: dict) -> dict:
    need = dict(p["need"])
    if str(need.get("deliver_by", "")).lower() in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        need["deliver_by"] = next_weekday(need["deliver_by"])
    return need


async def run_scripted(preset: dict, api_key: str, url: str = DEFAULT_URL, on_event: Event = _print_event,
                       before_order: Callable[[], None] | None = None) -> dict:
    """Basic agent: declare need -> find_offers -> place the suggested plan -> fix errors -> report."""
    need = preset_need(preset)
    on_event({"kind": "say", "who": "buyer", "text": preset["text"]})
    on_event({"kind": "say", "text": f"Declared need: {json.dumps(need, ensure_ascii=False)}"})
    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {api_key}"}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            offers = await _call(session, "find_offers", {"need": need}, on_event)
            if offers.get("error") or not (offers.get("suggested_plan") or {}).get("lines"):
                msg = offers.get("error") or (offers.get("suggested_plan") or {}).get("why") or offers.get("hint")
                on_event({"kind": "say", "text": f"I could not find an orderable option: {msg}"})
                return {"outcome": "no_offer", "detail": msg}
            plan = offers["suggested_plan"]
            lines = [{"offer_id": l["offer_id"], "quantity": l["quantity"], "is_substitution": l["is_substitution"]} for l in plan["lines"]]
            if preset.get("naive_first_quantity") is not None:   # simulate a sloppy first attempt
                lines = [dict(lines[0], quantity=preset["naive_first_quantity"])]
                on_event({"kind": "say", "text": f"Ordering {preset['naive_first_quantity']:g} {plan['lines'][0]['sales_unit']} of {plan['lines'][0]['name']}."})
            else:
                on_event({"kind": "say", "text": f"Accepting Valio's suggested plan: {plan['why']}"})
            if before_order:
                before_order()
            result = None
            for attempt in range(3):
                result = await _call(session, "place_order", {"lines": lines, "need": need}, on_event)
                if result.get("status") != "error":
                    break
                fixed = False
                for err in result["errors"]:
                    if err["code"] == "lot_size" and err.get("suggestions"):
                        i = err["line"]
                        q = max(err["suggestions"])        # never under-order
                        on_event({"kind": "say", "text": f"Valio says: {err['message']} Using {q:g}."})
                        lines[i]["quantity"] = q
                        fixed = True
                    elif err["code"] in ("quantity_short", "insufficient_stock", "offer_invalid"):
                        on_event({"kind": "say", "text": f"Valio says: {err['message']} Re-planning with fresh offers."})
                        offers = await _call(session, "find_offers", {"need": need}, on_event)
                        lines = [{"offer_id": l["offer_id"], "quantity": l["quantity"], "is_substitution": l["is_substitution"]}
                                 for l in (offers.get("suggested_plan") or {}).get("lines", [])]
                        fixed = bool(lines)
                        break
                if not fixed:
                    break
            report = _report(result)
            on_event({"kind": "say", "text": report})
            return {"outcome": (result or {}).get("status"), "order": result, "report": report}


def _report(r: dict | None) -> str:
    if not r:
        return "No order was made."
    s = r.get("status")
    if s == "placed":
        return f"Done: order {r['order_id']} is placed. {r['agent_understood']}. {r.get('next', '')}"
    if s == "pending_approval":
        why = "; ".join(x["message"] for x in r.get("reasons", []))
        return f"Order {r['order_id']} is waiting for your approval (not placed yet): {why.rstrip('.')}. Proposed: {r['agent_understood']}."
    if s == "rejected":
        return f"Valio rejected the order: {'; '.join(x['message'] for x in r.get('reasons', []))}"
    return "I could not place the order: " + " | ".join(e["message"] for e in r.get("errors", []))


# ---------------------------------------------------------------- LLM agent (Gemini)

SYSTEM = """You are the purchasing agent of {buyer_name}, a professional kitchen. You buy from Valio Aimo
through its MCP tools. Today is {today}.
Rules:
- Translate the buyer's words into a declared need. `text` must be the buyer's exact words. `query` is only the
  product words (e.g. 'milk'). Dietary requirements go to required_labels / exclude_allergens. Resolve relative
  dates ('by Monday') to an ISO date. Use unit L for liquids by volume, KG for weight, SALES_UNIT for counts.
- Call find_offers, then place_order with the SAME need. Prefer the suggested_plan unless the buyer asked for
  something it ignores. If place_order returns errors, fix exactly what they say and retry (max 3 attempts).
- Answer in the language the buyer used (e.g. Finnish if they spoke Finnish); keep `text` verbatim in their language.
- Report honestly in 2-4 sentences: what was ordered or proposed, the total, and whether it is placed or
  waiting for human approval (and why). Never say an order is placed if it is pending approval.
- If the buyer's words contain something the schema cannot express or you cannot verify ("the one the chef liked",
  "same as last time", "fresh"), declare the parts you can, order the best matching offer, and describe the doubt in
  `unresolved` (e.g. "which cheese the chef liked last time is unknown; proposed the cheapest hard cheese").
  Valio then sends the order to a human approver instead of placing it. Never hide a doubt.
- If the exact item is unavailable, try at most 2 alternative searches, then order the closest option with the
  difference described in `unresolved`."""


def _inline_refs(schema: dict) -> dict:
    """Gemini wants plain JSON schema without $ref/$defs."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node
    return walk(schema)


def _gemini_safe(node):
    """Gemini rejects '$'-prefixed keys ($ref, $defs) inside function responses."""
    if isinstance(node, dict):
        return {(k.lstrip("$") if isinstance(k, str) else k): _gemini_safe(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_gemini_safe(x) for x in node]
    return node


async def run_llm(text: str, api_key: str, buyer_name: str = "a professional kitchen", url: str = DEFAULT_URL,
                  on_event: Event = _print_event, max_turns: int = 10) -> dict:
    from google import genai
    from google.genai import types

    gkey = os.getenv("GEMINI_API_KEY")
    if not gkey:
        raise RuntimeError("GEMINI_API_KEY is not set; use a --preset (scripted agent) instead")
    model = os.getenv("GEMINI_MODEL", "gemini-4-hydrogen")
    client = genai.Client(api_key=gkey)
    on_event({"kind": "say", "who": "buyer", "text": text})
    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {api_key}"}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            decls = [types.FunctionDeclaration(name=t.name, description=t.description or "",
                                               parameters_json_schema=_inline_refs(t.inputSchema)) for t in tools]
            today = date.today()
            cfg = types.GenerateContentConfig(
                system_instruction=SYSTEM.format(buyer_name=buyer_name, today=f"{today:%A} {today.isoformat()}"),
                tools=[types.Tool(function_declarations=decls)],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                temperature=0)
            contents: list = [types.Content(role="user", parts=[types.Part.from_text(text=text)])]
            last_order = None
            for _ in range(max_turns):
                resp = await client.aio.models.generate_content(model=model, contents=contents, config=cfg)
                cand = resp.candidates[0].content
                contents.append(cand)
                calls = [p.function_call for p in cand.parts or [] if p.function_call]
                if not calls:
                    final = resp.text or ""
                    on_event({"kind": "say", "text": final})
                    return {"outcome": (last_order or {}).get("status"), "order": last_order, "report": final}
                parts = []
                for fc in calls:
                    data = await _call(session, fc.name, dict(fc.args or {}), on_event)
                    if fc.name == "place_order":
                        last_order = data
                    parts.append(types.Part.from_function_response(name=fc.name, response={"result": _gemini_safe(data)}))
                contents.append(types.Content(role="user", parts=parts))
            on_event({"kind": "say", "text": "Stopped: too many steps."})
            return {"outcome": (last_order or {}).get("status"), "order": last_order, "report": "too many steps"}


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="?", help="the buyer's request in plain words (LLM mode)")
    ap.add_argument("--key", default="demo-hotel-aurora-key", help="customer API key")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--preset", help="run a scripted demo preset")
    ap.add_argument("--list-presets", action="store_true")
    args = ap.parse_args()
    if args.list_presets:
        for p in load_presets().values():
            print(f"{p['id']:<22} {p['buyer_key']:<26} {p['text']}")
        return
    if args.preset:
        p = load_presets()[args.preset]
        base = args.url.rsplit("/mcp", 1)[0]

        def set_overlay(name):  # demo overlay is server state, switched via the human/demo endpoint
            import httpx
            httpx.post(f"{base}/api/ui/scenario", json={"name": name},
                       headers={"X-Staff-Key": os.getenv("STAFF_KEY", "demo-staff")}, timeout=10).raise_for_status()

        set_overlay(p.get("scenario"))
        after = p.get("scenario_after_find")
        try:
            asyncio.run(run_scripted(p, p["buyer_key"], args.url,
                                     before_order=(lambda: set_overlay(after)) if after else None))
        finally:
            set_overlay(None)
    elif args.text:
        asyncio.run(run_llm(args.text, args.key, url=args.url))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
