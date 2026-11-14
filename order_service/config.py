"""Environment, buyers and mandates. Onboarding a customer is a config change."""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

VALIO_API_BASE = os.getenv("VALIO_API_BASE", "https://junction26.valioaimo.fi/api").rstrip("/")
VALIO_API_KEY = os.getenv("VALIO_API_KEY", "")
ORDERS_DB = Path(os.getenv("ORDERS_DB", ROOT / "order_service" / "orders.db"))
BUYERS_FILE = Path(os.getenv("BUYERS_FILE", ROOT / "order_service" / "buyers.json"))
SCENARIOS_FILE = ROOT / "order_service" / "scenarios.json"

OFFER_TTL_SECONDS = 15 * 60
API_CACHE_SECONDS = 120
API_MAX_PRODUCTS = 50          # Valio allows 1,000; undisclosed rate limits, so we cap.
LIVE_CANDIDATES = 25           # top-N catalog candidates (of ~17,500) that get live price + stock
LOW_RELIABILITY = 0.9          # offer flag threshold; mandates may be stricter
AI_CROSSCHECK = os.getenv("AI_CROSSCHECK", "0") == "1"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-4-hydrogen")

# Public hostnames this server answers on (e.g. my-app.onrender.com). Empty = local only.
PUBLIC_HOSTS = [h.strip() for h in (os.getenv("PUBLIC_HOSTS") or os.getenv("RENDER_EXTERNAL_HOSTNAME") or "").split(",") if h.strip()]
PORT = int(os.getenv("PORT", "8000"))
# Running on a public cloud host (set by the deploy config). Requires a real STAFF_KEY.
PUBLIC_DEPLOY = os.getenv("PUBLIC_DEPLOY") == "1" or bool(PUBLIC_HOSTS)
# Hackathon demo: the human-side pages open without the staff key. Real authentication and roles are a next step.
OPEN_DEMO = os.getenv("OPEN_DEMO") == "1"

_secret = os.getenv("OFFER_SIGNING_SECRET", "")
if not _secret:
    # Dev fallback: a per-process secret, so offers die on restart. Set it in .env.
    _secret = secrets.token_hex(32)
    print("[config] OFFER_SIGNING_SECRET not set: using an ephemeral secret (offers expire on restart)")
OFFER_SIGNING_SECRET = _secret.encode()


def load_buyers() -> dict[str, dict]:
    data = json.loads(BUYERS_FILE.read_text(encoding="utf-8"))
    return {b["buyer_id"]: b for b in data["buyers"]}


def save_mandate(buyer_id: str, mandate: dict) -> dict:
    data = json.loads(BUYERS_FILE.read_text(encoding="utf-8"))
    for b in data["buyers"]:
        if b["buyer_id"] == buyer_id:
            b["mandate"].update({k: int(v) if isinstance(v, float) and v.is_integer() else v for k, v in mandate.items()})
            BUYERS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return b
    raise KeyError(buyer_id)


def buyer_for_key(api_key: str | None) -> dict | None:
    if not api_key:
        return None
    for b in load_buyers().values():
        if secrets.compare_digest(b["api_key"], api_key):
            return b
    return None


def load_scenarios() -> dict:
    data = json.loads(SCENARIOS_FILE.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}
