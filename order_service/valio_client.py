"""Client for Valio's mock Pricing and Availability APIs.

- Only called after the local catalog has narrowed the candidates.
- At most API_MAX_PRODUCTS codes per call, responses cached for API_CACHE_SECONDS.
- Raw quirks are normalised here, once:
    Terminal + 999999      -> stock unknown (supplier stock), never "unlimited"
    empty scheduleLines    -> not deliverable
    availableAmount 0      -> out of stock
    salesBlock             -> cannot be ordered
    closingTime            -> order cut-off for delivery on availableDate
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime, timezone

import httpx

from order_service import config

TERMINAL_UNKNOWN = 999999
AMPLE = 10**8                     # Stock values at/above this are placeholders for "plenty"

_cache: dict[tuple, tuple[float, dict]] = {}
_active_scenario: str | None = os.getenv("VALIO_SCENARIO") or None


class ValioAPIError(RuntimeError):
    pass


def set_scenario(name: str | None) -> None:
    global _active_scenario
    if name and name not in config.load_scenarios():
        raise KeyError(name)
    _active_scenario = name or None
    _cache.clear()


def active_scenario() -> str | None:
    return _active_scenario


def now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if len(s) >= 5 and (s[-5] in "+-") and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]           # +0200 -> +02:00
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


async def _post(path: str, customer: str, requested: str, codes: list[str]) -> dict:
    if not config.VALIO_API_KEY:
        raise ValioAPIError("VALIO_API_KEY is not set in .env")
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(3):
            try:
                r = await client.post(f"{config.VALIO_API_BASE}{path}",
                                      json={"customer": customer, "requestedDate": requested, "products": codes},
                                      headers={"X-API-Key": config.VALIO_API_KEY})
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise ValioAPIError(f"{path} unreachable: {e}") from e
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 2:
                    raise ValioAPIError(f"{path} returned {r.status_code}")
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise ValioAPIError(f"{path} returned {r.status_code}: {r.text[:200]}")
            return r.json()
    raise ValioAPIError(f"{path} failed")


async def _fetch(path: str, customer: str, requested: date, codes: list[str], fresh: bool) -> dict[str, dict]:
    req = requested.isoformat()
    out, missing = {}, []
    t = time.time()
    for c in dict.fromkeys(codes):
        hit = _cache.get((path, customer, req, c))
        if hit and not fresh and t - hit[0] < config.API_CACHE_SECONDS:
            out[c] = hit[1]
        else:
            missing.append(c)
    for i in range(0, len(missing), config.API_MAX_PRODUCTS):
        batch = missing[i:i + config.API_MAX_PRODUCTS]
        data = await _post(path, customer, req, batch)
        stamp = data.get("timeNow") or data.get("pricingDate")
        for item in data.get("products") or []:
            item = dict(item, _as_of=now().isoformat(timespec="seconds"), _api_time=stamp)
            _cache[(path, customer, req, item["productId"])] = (t, item)
            out[item["productId"]] = item
    return out


def _normalise_availability(code: str, raw: dict | None, deliver_by: date) -> dict:
    scenario = None
    if raw and _active_scenario:
        override = config.load_scenarios()[_active_scenario].get("availability", {}).get(code)
        if override:
            raw = dict(raw, scheduleLines=[dict(sl, **override) for sl in raw.get("scheduleLines") or []])
            scenario = _active_scenario
    if not raw:
        return {"found": False, "deliverable": False, "stock_known": False, "reason": "product not returned by availability API"}
    lines = raw.get("scheduleLines") or []
    model = raw.get("logisticModel")
    closing = parse_ts(raw.get("closingTime"))
    block = bool(raw.get("salesBlock")) or any(sl.get("salesBlock") for sl in lines)
    dated = [sl for sl in lines if sl.get("availableDate")]
    first = min(dated, key=lambda sl: sl["availableDate"]) if dated else None
    on_time = [sl for sl in dated if sl["availableDate"] <= deliver_by.isoformat()]
    use = on_time or ([first] if first else [])     # nothing on time -> earliest later date
    amount = sum(float(sl.get("availableAmount") or 0) for sl in use)
    terminal = model == "Terminal"
    stock_known = not terminal and model is not None
    available_date = date.fromisoformat(max(sl["availableDate"] for sl in use)) if use else None
    out = {
        "found": True,
        "logistic_model": model,
        "stock_known": stock_known,
        "available_units": None if terminal else (None if amount >= AMPLE else amount),
        "ample_stock": stock_known and amount >= AMPLE,
        "available_date": available_date.isoformat() if available_date else None,
        "later_than_requested": bool(available_date and available_date > deliver_by),
        "closing_time": closing.isoformat() if closing else None,
        "cutoff_passed": bool(closing and closing < now()),
        "sales_block": block,
        "deliverable": bool(first) and amount > 0 and not block,
        "sales_unit": (lines[0].get("salesUnit") if lines else None),
        "schedule_lines": [{"date": sl.get("availableDate"), "amount": sl.get("availableAmount")} for sl in lines],
        "as_of": raw.get("_as_of"),
        "api_time": raw.get("_api_time"),
        "scenario_override": scenario,
    }
    if not lines:
        out["reason"] = "no schedule lines: not deliverable"
    elif not first or amount <= 0:
        out["reason"] = "out of stock for the requested date"
    return out


def _normalise_price(code: str, raw: dict | None) -> dict:
    if not raw or raw.get("error") or not raw.get("catalogPrice"):
        return {"found": False, "reason": (raw or {}).get("errorMessage") or "no price returned"}
    cat = raw["catalogPrice"]
    comp = raw.get("comparisonPrice") or {}
    mult, scenario = 1.0, None
    if _active_scenario:
        m = config.load_scenarios()[_active_scenario].get("price_multiplier", {}).get(code)
        if m:
            mult, scenario = float(m), _active_scenario
    return {
        "found": True,
        "unit_price_eur": round(cat["value"] * mult, 4),
        "unit_price_with_vat_eur": round((cat.get("valueWithTax") or cat["value"]) * mult, 4),
        "vat_rate_pct": (raw.get("tax") or {}).get("taxRate"),
        "comparison_price_eur": round(comp["value"] * mult, 4) if comp.get("value") else None,
        "comparison_unit": raw.get("comparisonUnit"),
        "currency": cat.get("currency", "EUR"),
        "valid_to": cat.get("validTo"),
        "as_of": raw.get("_as_of"),
        "scenario_override": scenario,
    }


async def price_and_availability(codes: list[str], customer: str, deliver_by: date, fresh: bool = False) -> dict[str, dict]:
    """{code: {"price": {...}, "availability": {...}}} for up to a few dozen codes."""
    prices, avail = await asyncio.gather(
        _fetch("/price", customer, deliver_by, codes, fresh),
        _fetch("/availability", customer, deliver_by, codes, fresh),
    )
    return {c: {"price": _normalise_price(c, prices.get(c)),
                "availability": _normalise_availability(c, avail.get(c), deliver_by)} for c in codes}
