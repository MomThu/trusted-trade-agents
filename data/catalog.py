"""Static catalog: search, product facts, allergen status, official substitutions.

Search runs only on the local product DB (static data). The live Valio APIs are
called later, and only for the top candidates.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path

from data.vocab import ALLERGENS, LABEL_WORDS, LABELS, SYNONYMS, WEIGHT_SALES_UNITS

DB_PATH = Path(__file__).resolve().parent / "valio.db"
_local = threading.local()


def _con() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is None:
        if not DB_PATH.exists():
            raise RuntimeError("data/valio.db missing: run `python -m data.load_data` first")
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        con.row_factory = sqlite3.Row
        _local.con = con
    return con


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("labels", "claims", "allergens", "deposits"):
        d[k] = json.loads(d[k]) if d.get(k) else ([] if k != "allergens" else {})
    d["allergen_data"] = bool(d["allergen_data"])
    return d


def get_product(code: str) -> dict | None:
    r = _con().execute("SELECT * FROM products WHERE code=?", (str(code),)).fetchone()
    return _row(r) if r else None


def get_products(codes: list[str]) -> dict[str, dict]:
    if not codes:
        return {}
    q = ",".join("?" * len(codes))
    return {r["code"]: _row(r) for r in _con().execute(f"SELECT * FROM products WHERE code IN ({q})", list(codes))}


def official_substitutes(code: str) -> list[str]:
    rows = _con().execute("SELECT substitute FROM substitutions WHERE product=? ORDER BY rank", (code,))
    return [r["substitute"] for r in rows]


def display_name(p: dict) -> str:
    return p.get("name_en") or p.get("name_fi") or p.get("name_sv") or p["code"]


def measure_per_unit(p: dict, unit: str) -> float | None:
    """Amount of `unit` (L, KG or SALES_UNIT) in one sales unit."""
    unit = unit.upper()
    if unit == "SALES_UNIT":
        return 1.0
    if unit == "L":
        return p.get("litres_per_unit")
    if unit == "KG":
        return p.get("kg_per_unit")
    return None


def order_step(p: dict) -> float:
    """Valid quantity step in sales units. allowedLotSize applies; piece-type units
    are never split below one piece (a lot size of 0.01 on a carton means 1)."""
    lot = float(p.get("lot_size") or 1)
    if p.get("sales_unit") in WEIGHT_SALES_UNITS:
        return lot
    return lot if lot >= 1 else 1.0


def allergen_status(p: dict, group: str) -> str:
    """'contains' | 'may_contain' | 'free_from' | 'unknown'. No statement is never safe."""
    spec = ALLERGENS[group]
    stmt = p.get("allergens") or {}
    levels = {stmt.get(c) for c in spec["codes"]}
    if "CONTAINS" in levels:
        return "contains"
    if "MAY_CONTAIN" in levels:
        return "may_contain"
    if stmt.get(spec["primary"]) == "FREE_FROM" or set(p.get("claims") or []) & spec["free_claims"]:
        return "free_from"
    return "unknown"


def label_ok(p: dict, label: str) -> bool:
    return label in (p.get("labels") or [])


class UnknownTerm(ValueError):
    pass


def validate_labels(labels: list[str]) -> list[str]:
    bad = [l for l in labels if l not in LABELS]
    if bad:
        raise UnknownTerm(f"Unknown label(s) {bad}. Supported labels: {sorted(LABELS)}")
    return labels


def validate_allergens(groups: list[str]) -> list[str]:
    bad = [g for g in groups if g not in ALLERGENS]
    if bad:
        raise UnknownTerm(f"Unknown allergen group(s) {bad}. Supported: {sorted(ALLERGENS)}")
    return groups


def split_query(query: str) -> tuple[list[str], list[str]]:
    """Split a product query into search words and label words (deterministic table)."""
    words, labels = [], []
    for raw in re.findall(r"[\wåäöÅÄÖ-]+", (query or "").lower()):
        if raw in LABEL_WORDS:
            labels.append(LABEL_WORDS[raw])
        else:
            words.append(raw)
    # "lactose free" written as two words
    joined = " ".join(words)
    for two, lab in (("lactose free", "lactose-free"), ("gluten free", "gluten-free")):
        if two in joined:
            labels.append(lab)
            joined = joined.replace(two, " ")
    return [w for w in joined.split() if len(w) > 1], sorted(set(labels))


def _alts(w: str) -> set[str]:
    """The word plus its synonyms (symmetric: maito <-> milk)."""
    alts = {w, *SYNONYMS.get(w, [])}
    for k, vs in SYNONYMS.items():
        if w in vs:
            alts |= {k, *vs}
    return alts


def _fts_query(words: list[str]) -> str:
    groups = []
    for w in words:
        terms = " OR ".join('"' + a.replace('"', "") + '"*' for a in sorted(_alts(w)))
        groups.append(f"({terms})")
    return " AND ".join(groups)


# A matched word directly followed by one of these is a modifier, not the product:
# "milk chocolate", "milk coffee drink", "orange cola drink", "milk powder".
MODIFIER_NOUNS = {
    "chocolate", "coffee", "cocoa", "powder", "shake", "protein", "bread", "jam", "sauce", "soup",
    "biscuit", "biscuits", "cake", "candy", "porridge", "pudding", "cola", "flavour", "flavor", "flavored",
    "flavoured", "marmalade", "drops", "tea", "latte", "cappuccino", "mocca", "icecream", "ice", "dessert",
    "mix", "seasoning", "spice", "salad", "pie", "bun", "snack", "bar", "filling",
}


# A matched word directly preceded by one of these is a different product:
# "ice cream", "sour milk", "coffee milk", "oat milk", "coconut cream".
PRE_MODIFIERS = {"ice", "sour", "coffee", "coconut", "oat", "soy", "almond", "rice", "chocolate", "condensed",
                 "powdered", "portion", "cooking"}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\wåäö]+", (text or "").lower())


def relevance(p: dict, words: list[str]) -> float:
    """Transparent relevance: exact word 3, word prefix 1.5, phrase bonus 2,
    modifier penalty 2.5, tiny length penalty so generic products win ties."""
    names = [p.get("name_en"), p.get("name_fi"), p.get("name_sv"), p.get("brand")]
    toks = [_tokens(n) for n in names]
    score = 0.0
    positions = []
    for w in words:
        alts = _alts(w)
        best, pos = 0.0, None
        for t in toks:
            for i, tok in enumerate(t):
                q = 3.0 if tok in alts else 1.5 if any(tok.startswith(a) for a in alts) else 0.0
                if q > best:
                    best, pos = q, (t, i)
        score += best
        if pos:
            positions.append(pos)
            t, i = pos
            if i + 1 < len(t) and t[i + 1] in MODIFIER_NOUNS and t[i + 1] not in words:
                score -= 2.5
            if i > 0 and t[i - 1] in PRE_MODIFIERS and t[i - 1] not in words:
                score -= 2.5
    if len(words) > 1 and len(positions) == len(words):
        same = all(pt[0] is positions[0][0] for pt in positions)
        idx = [pt[1] for pt in positions]
        if same and idx == list(range(idx[0], idx[0] + len(idx))):
            score += 2.0
    return score - len(display_name(p)) / 400


def _negated(p: dict, words: list[str]) -> bool:
    """True when the name says the product is FREE of the searched thing ('milk free', 'maidoton')."""
    name = f"{p.get('name_en') or ''} {p.get('name_fi') or ''}".lower()
    for w in words:
        if re.search(rf"\b{re.escape(w)}[\s-]*free\b", name):
            return True
        if w in ("milk", "maito") and "maidoton" in name:
            return True
    return False


def search_local_products(
    query: str,
    required_labels: list[str] | None = None,
    exclude_allergens: list[str] | None = None,
    unit: str = "SALES_UNIT",
    category: str | None = None,
    product_codes: list[str] | None = None,
    limit: int = 30,
) -> dict:
    """Filter the static catalog. Returns candidates plus how the query was read.

    Steps: full-text match on fi/sv/en names + brand (+ synonyms) -> drop names that say
    'X-free' for the searched X -> label filter -> drop products that CONTAIN or
    MAY_CONTAIN an excluded allergen (unknown is kept but marked, never called safe)
    -> keep products measurable in the requested unit -> rank by `relevance()` -> keep
    the dominant category of the best matches (so 'milk' does not return chocolate drinks or quark).
    """
    required_labels = validate_labels(list(required_labels or []))
    exclude_allergens = validate_allergens(list(exclude_allergens or []))
    words, label_words = split_query(query)
    moved = [l for l in label_words if l not in required_labels]
    labels = sorted(set(required_labels) | set(label_words))
    notes = []
    if moved:
        notes.append(f"Query words read as required labels: {moved}")

    con = _con()
    if product_codes:
        rows = list(get_products([str(c) for c in product_codes]).values())
        ranked = [(p, 0.0) for p in rows]
    else:
        if not words:
            raise UnknownTerm("Query has no product words. Example: {'query': 'milk', 'required_labels': ['lactose-free']}")
        fts = _fts_query(words)
        hits = con.execute("SELECT code FROM products_fts WHERE products_fts MATCH ? LIMIT 2000", (fts,)).fetchall()
        prods = get_products([h["code"] for h in hits])
        ranked = [(p, relevance(p, words)) for p in prods.values()]

    candidates, dropped = [], defaultdict(int)
    for p, score in ranked:
        if not product_codes and _negated(p, words):
            dropped["name says free of searched item"] += 1
            continue
        if any(not label_ok(p, l) for l in labels):
            dropped["missing required label"] += 1
            continue
        status = {g: allergen_status(p, g) for g in exclude_allergens}
        if any(s in ("contains", "may_contain") for s in status.values()):
            dropped["contains excluded allergen"] += 1
            continue
        if unit.upper() != "SALES_UNIT" and measure_per_unit(p, unit) is None and not product_codes:
            dropped[f"not measurable in {unit}"] += 1
            continue
        if category and p["category"] != category:
            dropped["other category"] += 1
            continue
        candidates.append({"product": p, "relevance": round(score, 3), "allergen_status": status})

    candidates.sort(key=lambda c: -c["relevance"])
    chosen_category = category
    if not category and not product_codes and candidates:
        weight = defaultdict(float)
        for i, c in enumerate(candidates[:30]):
            weight[c["product"]["category"]] += max(c["relevance"], 0.1) / (i + 1)
        chosen_category = max(weight, key=weight.get)
        before = len(candidates)
        candidates = [c for c in candidates if c["product"]["category"] == chosen_category]
        if before - len(candidates):
            dropped["outside dominant category"] += before - len(candidates)
            notes.append(f"Narrowed to product category {chosen_category} (dominant among best matches); "
                         "pass `category` to override")

    return {
        "query_words": words,
        "required_labels": labels,
        "exclude_allergens": exclude_allergens,
        "category": chosen_category,
        "candidates": candidates[:limit],
        "total_matches": len(candidates),
        "dropped": dict(dropped),
        "notes": notes,
    }


def product_card(p: dict, exclude_allergens: list[str] | None = None) -> dict:
    """Static facts about a product, as exposed to agents (no confidential data)."""
    return {
        "product_code": p["code"],
        "name": display_name(p),
        "names": {"en": p.get("name_en"), "fi": p.get("name_fi"), "sv": p.get("name_sv")},
        "brand": p.get("brand"),
        "category": p.get("category"),
        "sales_unit": p.get("sales_unit"),
        "order_step": order_step(p),
        "litres_per_sales_unit": p.get("litres_per_unit"),
        "kg_per_sales_unit": p.get("kg_per_unit"),
        "labels": p.get("labels"),
        "allergen_data_available": p.get("allergen_data"),
        "allergens": {g: allergen_status(p, g) for g in (exclude_allergens or [])},
        "country_of_origin": p.get("country"),
        "delivery_reliability": p.get("reliability"),
        "carbon_kg_co2e_per_kg": p.get("carbon_kg_per_kg"),
        "official_substitutes": official_substitutes(p["code"]),
    }
