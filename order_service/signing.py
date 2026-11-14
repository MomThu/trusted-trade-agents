"""Signed offers. The offer_id is the offer's key facts (base64 JSON) + an
HMAC-SHA256 signature made with a secret only Valio's server holds, like a JWT.
Altering price/quantity/buyer/expiry breaks the signature. The server needs no
offer storage to verify; we still log offers for the audit trail.

Production next step: Ed25519 with a published public key so buyer agents and
auditors can verify offers themselves.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from order_service.config import OFFER_SIGNING_SECRET as SECRET


class OfferError(ValueError):
    pass


def sign_offer(offer: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(offer, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return f"of_{body}.{sig}"


def verify_offer(offer_id: str, buyer_id: str) -> dict:
    try:
        if not offer_id.startswith("of_"):
            raise ValueError
        body, sig = offer_id[3:].rsplit(".", 1)
    except ValueError:
        raise OfferError("Malformed offer_id: pass an offer_id exactly as returned by find_offers")
    expected = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise OfferError("Offer was altered or not issued by Valio (signature mismatch)")
    offer = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if offer["buyer_id"] != buyer_id:
        raise OfferError("Offer belongs to another buyer")
    if time.time() > offer["expires_at"]:
        raise OfferError("Offer expired (offers are valid 15 minutes): call find_offers again")
    return offer


def short_ref(offer_id: str) -> str:
    """Stable short handle for logs and UI."""
    return "of_" + hashlib.sha256(offer_id.encode()).hexdigest()[:10]
