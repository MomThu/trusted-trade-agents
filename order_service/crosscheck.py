"""Optional Valio-side AI cross-check (AI_CROSSCHECK=1 + GEMINI_API_KEY).

An LLM compares the buyer's words (need.text) with the structured order summary.
It can only ADD a possible_misunderstanding flag (making the order stricter);
it can never approve, place or change an order. Failures are ignored.
"""
from __future__ import annotations

import asyncio
import json

from order_service import config
from order_service.models import Need

PROMPT = """You check orders that an AI purchasing agent placed for a professional kitchen.
The buyer may have asked for several products; this order may cover only ONE of them. Judge only this order's product.

Buyer's own words: {text}
This order: {summary}
Facts from Valio (trust these): {context}

Flag the order if, for the product in this order, the buyer's words contain a requirement the order does not clearly
satisfy or that cannot be verified from it, for example:
- a specific item identified by memory or history ("the one the chef liked", "same as last time", "our usual brand")
  when the order does not show it is that item
- a quality or state the product name does not confirm ("fresh", "organic", "boneless", "frozen", a brand, a size)
- a clearly different product type, quantity, unit or date
Do NOT flag: a ranking request ("cheapest", "most reliable") when the facts say Valio ranked by that priority;
"usual" / "same as last time" when the facts show this buyer has ordered this product before; other products the buyer mentioned that belong to separate orders, rounding up to pack sizes,
or generic requests ("milk", "butter") met by any matching product.
Answer JSON only: {{"mismatch": true|false, "reason": "<one short sentence for a human approver>"}}"""


async def crosscheck(need: Need, summary: str, context: str = "none") -> str | None:
    if not config.GEMINI_API_KEY:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        resp = await asyncio.wait_for(client.aio.models.generate_content(
            model=config.GEMINI_MODEL, contents=PROMPT.format(text=need.text, summary=summary, context=context),
            config={"response_mime_type": "application/json", "temperature": 0}), timeout=12)
        data = json.loads(resp.text)
        return data.get("reason") or "possible mismatch" if data.get("mismatch") else None
    except Exception as e:  # never block an order on the cross-check
        print(f"[crosscheck] skipped: {e}")
        return None
