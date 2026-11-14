"""The declared need: the shared language between any buyer agent and Valio.

Valio defines this schema; every agent translates the buyer's words into it.
quantity, unit and deliver_by are required; the rest is optional.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Preferences(BaseModel):
    priority: Literal["price", "reliability", "carbon"] = "price"
    max_unit_price_eur: float | None = Field(None, description="Max price per sales unit, excl. VAT")
    allow_substitutes: bool = True
    accept_partial: bool = Field(False, description="Accept covering less than the full quantity (goes to approval)")
    country: str | None = Field(None, description="Preferred country of origin (ISO 3166-1 alpha-2), ranking hint only")


class Need(BaseModel):
    text: str = Field(..., description="The buyer's original words, verbatim")
    query: str = Field(..., description="Product words for catalog search, e.g. 'milk' (fi/sv/en)")
    quantity: float = Field(..., gt=0)
    unit: Literal["L", "KG", "SALES_UNIT"] = Field(..., description="L, KG, or SALES_UNIT (count of sales units)")
    deliver_by: date
    required_labels: list[str] = Field(default_factory=list)
    exclude_allergens: list[str] = Field(default_factory=list)
    preferences: Preferences = Field(default_factory=Preferences)
    category: str | None = Field(None, description="Optional product category code to search in")
    product_codes: list[str] | None = Field(None, description="Optional: restrict to these product codes (e.g. the usual product)")
    unresolved: str | None = Field(None, description="Anything in the buyer's words the agent could not express in this schema "
                                   "or verify, e.g. 'which cheese the chef liked last time' or 'must be fresh'. "
                                   "An order with this set always goes to a human, who sees the doubt next to the order.")

    @field_validator("unit", mode="before")
    @classmethod
    def _unit(cls, v):
        v = str(v).strip().upper()
        return {"LITRE": "L", "LITRES": "L", "LITER": "L", "LITERS": "L", "KILO": "KG", "KILOGRAM": "KG",
                "KILOGRAMS": "KG", "UNITS": "SALES_UNIT", "PCS": "SALES_UNIT", "UNIT": "SALES_UNIT"}.get(v, v)

    @field_validator("required_labels", "exclude_allergens", mode="before")
    @classmethod
    def _lower(cls, v):
        return sorted({str(x).strip().lower() for x in (v or [])})

    def digest(self) -> str:
        """Fingerprint of the declared need, carried inside every signed offer."""
        d = self.model_dump(mode="json")
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


class OrderLine(BaseModel):
    offer_id: str
    quantity: float = Field(..., gt=0, description="In sales units")
    is_substitution: bool | None = Field(None, description="Informational; the server uses the role signed into the offer")
