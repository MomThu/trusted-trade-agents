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

PROMPT = """You audit purchase orders made by an AI agent for a professional kitchen.
Buyer's own words: {text}
Order the agent built: {summary}
Does the order clearly contradict the buyer's words (wrong product type, wrong quantity or unit,
wrong date, a dietary requirement ignored)? Minor differences in rounding, brand or pack size are fine.
Answer JSON only: {{"mismatch": true|false, "reason": "<one short sentence>"}}"""


async def crosscheck(need: Need, summary: str) -> str | None:
    if not config.GEMINI_API_KEY:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        resp = await asyncio.wait_for(client.aio.models.generate_content(
            model=config.GEMINI_MODEL, contents=PROMPT.format(text=need.text, summary=summary),
            config={"response_mime_type": "application/json", "temperature": 0}), timeout=12)
        data = json.loads(resp.text)
        return data.get("reason") or "possible mismatch" if data.get("mismatch") else None
    except Exception as e:  # never block an order on the cross-check
        print(f"[crosscheck] skipped: {e}")
        return None
