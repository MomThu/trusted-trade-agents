"""Load Valio's product JSON + delivery reliability CSV + carbon CSV into SQLite.

    python -m data.load_data              # build data/valio.db
    python -m data.load_data --sample 3   # print 3 parsed products, no DB write

Joined by product code. Everything the search and request check need is
precomputed here: names (fi/sv/en), labels, allergen statements, official
substitutions, lot size, litres/kg per sales unit, reliability and carbon.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from pathlib import Path

from data.vocab import LABELS

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw"
DB_PATH = ROOT / "valio.db"
PRODUCTS = RAW / "valio_aimo_junction26_product_data.json"
RELIABILITY = RAW / "valio_aimo_junction26_delivery_reliability.csv"
CARBON = RAW / "valio_aimo_junction26_carbon_footprint.csv"

VOL_TO_L = {"LTR": 1.0, "DLT": 0.1, "CLT": 0.01, "MLT": 0.001}
WT_TO_KG = {"KGM": 1.0, "GRM": 0.001}

# "1 l", "1,75 l", "5 dl", "220 ml", "100x2 cl", "400g", "2,5 kg"
SIZE_RE = re.compile(r"(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(ml|cl|dl|l|kg|g)\b", re.I)
PREFIX_RE = re.compile(r"^\s*(\d+)\s*[x×]\s+", re.I)
NAME_UNIT = {"ml": ("L", 0.001), "cl": ("L", 0.01), "dl": ("L", 0.1), "l": ("L", 1.0), "g": ("KG", 0.001), "kg": ("KG", 1.0)}


def _name(names: list[dict], lang: str) -> str | None:
    for n in names or []:
        if n.get("language") == lang and n.get("value"):
            return n["value"].strip()
    return None


def _classification(syn: dict, name: str) -> list[dict]:
    for c in syn.get("classifications") or []:
        if c.get("name") == name:
            return c.get("values") or []
    return []


def parse_name_size(name: str) -> tuple[str, float, bool] | None:
    """Return (unit, amount per piece incl. inner multipacks, has 'N x' prefix)."""
    if not name:
        return None
    prefix = bool(PREFIX_RE.match(name))
    body = PREFIX_RE.sub("", name)
    m = SIZE_RE.search(body)
    if not m:
        return None
    inner = int(m.group(1)) if m.group(1) else 1
    unit, factor = NAME_UNIT[m.group(3).lower()]
    amount = float(m.group(2).replace(",", ".")) * factor * inner
    prefix_n = int(PREFIX_RE.match(name).group(1)) if prefix else 1
    return unit, amount, prefix_n if prefix else 0


def measures(p: dict, name_en: str | None, name_fi: str | None) -> dict:
    """Litres / kg per *sales unit* from structured data, falling back to the name.

    The price API's catalogPrice / comparisonPrice later confirms or corrects this.
    """
    syn = p.get("synkkaData") or {}
    units = {u.get("unitId"): u.get("sizeInBaseUnits") for u in p.get("units") or []}
    sales, base = p.get("salesUnit"), p.get("baseUnit")
    base_per_sales = units.get(sales) or 1.0
    if sales == base:
        base_per_sales = 1.0
    out: dict = {"L": None, "KG": None, "source": None}
    if sales == "KG":
        out.update(KG=1.0, source="sales unit is kg")
    for conv in syn.get("unitConversions") or []:
        if conv.get("synkkaPackagingType") != "BASE_UNIT_OR_EACH":
            continue
        vol, net = conv.get("volume") or {}, conv.get("netWeight") or {}
        if vol.get("unit") in VOL_TO_L and out["L"] is None:
            out["L"] = round(vol["value"] * VOL_TO_L[vol["unit"]] * base_per_sales, 4)
            out["source"] = out["source"] or "GS1 unit conversions"
        if net.get("unit") in WT_TO_KG and out["KG"] is None:
            out["KG"] = round(net["value"] * WT_TO_KG[net["unit"]] * base_per_sales, 4)
            out["source"] = out["source"] or "GS1 unit conversions"
    parsed = parse_name_size(name_en or "") or parse_name_size(name_fi or "")
    if parsed and out[parsed[0]] is None:
        unit, amount, prefix_n = parsed
        total = amount * (prefix_n or 1) if sales == base else amount * base_per_sales
        out[unit] = round(total, 4)
        out["source"] = out["source"] or "parsed from product name"
    return out


def labels_of(claims: set[str], marks: set[str], allergen_stmt: dict) -> list[str]:
    have = claims | marks
    out = [k for k, codes in LABELS.items() if have & codes]
    if allergen_stmt.get("ML") == "FREE_FROM" and "lactose-free" not in out:
        out.append("lactose-free")
    return sorted(out)


def parse_product(p: dict, reliability: dict, carbon: dict) -> dict:
    syn = p.get("synkkaData") or {}
    names = syn.get("names") or []
    en, fi, sv = _name(names, "en"), _name(names, "fi"), _name(names, "sv")
    stmt: dict[str, str] = {}
    for v in _classification(syn, "allergen") + _classification(syn, "nonAllergen"):
        code, level = v.get("id"), v.get("unit")
        if not code or not level:
            continue
        # strongest statement wins: CONTAINS > MAY_CONTAIN > FREE_FROM
        rank = {"CONTAINS": 3, "MAY_CONTAIN": 2, "FREE_FROM": 1}
        if rank.get(level, 0) > rank.get(stmt.get(code, ""), 0):
            stmt[code] = level
    claims = {v.get("id") for v in _classification(syn, "nutritionalClaim") if v.get("id")}
    marks = {v.get("id") for v in _classification(syn, "packagingMarkedLabel") if v.get("id")}
    m = measures(p, en, fi)
    code = p["id"]
    return {
        "code": code,
        "name_en": en, "name_fi": fi, "name_sv": sv,
        "brand": (syn.get("brand") or {}).get("value") if isinstance(syn.get("brand"), dict) else syn.get("brand"),
        "vendor": p.get("vendorName"),
        "category": p.get("category"),
        "sales_unit": p.get("salesUnit"),
        "base_unit": p.get("baseUnit"),
        "lot_size": p.get("allowedLotSize") or 1,
        "litres_per_unit": m["L"],
        "kg_per_unit": m["KG"],
        "measure_source": m["source"],
        "labels": labels_of(claims, marks, stmt),
        "claims": sorted(claims),
        "allergens": stmt,
        "allergen_data": bool(stmt),
        "country": p.get("countryOfOrigin"),
        "synkka_updated": syn.get("lastUpdated"),
        "reliability": reliability.get(code),
        "carbon_kg_per_kg": carbon.get(code),
        "deposits": p.get("deposits") or [],
        "temperature": p.get("temperatureCondition"),
        "substitutes": [s["substituteProduct"] for s in p.get("substitutions") or [] if s.get("product") == code],
    }


def read_csv_map(path: Path, value_col: str) -> dict[str, float]:
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                out[row["productId"].strip()] = float(row[value_col])
            except (ValueError, KeyError):
                continue
    return out


SCHEMA = """
DROP TABLE IF EXISTS products; DROP TABLE IF EXISTS substitutions; DROP TABLE IF EXISTS products_fts;
CREATE TABLE products (
  code TEXT PRIMARY KEY, name_en TEXT, name_fi TEXT, name_sv TEXT, brand TEXT, vendor TEXT,
  category TEXT, sales_unit TEXT, base_unit TEXT, lot_size REAL,
  litres_per_unit REAL, kg_per_unit REAL, measure_source TEXT,
  labels TEXT, claims TEXT, allergens TEXT, allergen_data INTEGER,
  country TEXT, synkka_updated TEXT, reliability REAL, carbon_kg_per_kg REAL,
  deposits TEXT, temperature INTEGER
);
CREATE TABLE substitutions (product TEXT, substitute TEXT, rank INTEGER, PRIMARY KEY (product, substitute));
CREATE VIRTUAL TABLE products_fts USING fts5(code UNINDEXED, name_en, name_fi, name_sv, brand, tokenize='unicode61 remove_diacritics 0');
CREATE INDEX idx_products_category ON products(category);
"""


def build(db_path: Path = DB_PATH) -> dict:
    rel = read_csv_map(RELIABILITY, "deliveryReliability")
    carbon = read_csv_map(CARBON, "AVG kg CO2-eq/kg")
    raw = json.loads(PRODUCTS.read_text(encoding="utf-8"))
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    stats = {"products": 0, "english_name": 0, "allergen_data": 0, "litres": 0, "kg": 0,
             "substitution_rules": 0, "reliability": 0, "carbon": 0, "skipped_deleted": 0}
    for p in raw:
        if p.get("deleted"):
            stats["skipped_deleted"] += 1
            continue
        r = parse_product(p, rel, carbon)
        con.execute(
            "INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["code"], r["name_en"], r["name_fi"], r["name_sv"], r["brand"], r["vendor"], r["category"],
             r["sales_unit"], r["base_unit"], r["lot_size"], r["litres_per_unit"], r["kg_per_unit"], r["measure_source"],
             json.dumps(r["labels"]), json.dumps(r["claims"]), json.dumps(r["allergens"]), int(r["allergen_data"]),
             r["country"], r["synkka_updated"], r["reliability"], r["carbon_kg_per_kg"], json.dumps(r["deposits"]), r["temperature"]))
        con.execute("INSERT INTO products_fts VALUES (?,?,?,?,?)",
                    (r["code"], r["name_en"] or "", r["name_fi"] or "", r["name_sv"] or "", r["brand"] or ""))
        for i, s in enumerate(r["substitutes"]):
            con.execute("INSERT OR IGNORE INTO substitutions VALUES (?,?,?)", (r["code"], s, i))
            stats["substitution_rules"] += 1
        stats["products"] += 1
        stats["english_name"] += bool(r["name_en"])
        stats["allergen_data"] += r["allergen_data"]
        stats["litres"] += r["litres_per_unit"] is not None
        stats["kg"] += r["kg_per_unit"] is not None
        stats["reliability"] += r["reliability"] is not None
        stats["carbon"] += r["carbon_kg_per_kg"] is not None
    con.commit()
    con.close()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=0, help="print N parsed products and exit")
    args = ap.parse_args()
    if not PRODUCTS.exists():
        sys.exit(f"Missing {PRODUCTS}. Download the data files from the Valio resource portal into data/raw/.")
    if args.sample:
        rel = read_csv_map(RELIABILITY, "deliveryReliability")
        carbon = read_csv_map(CARBON, "AVG kg CO2-eq/kg")
        for p in json.loads(PRODUCTS.read_text(encoding="utf-8"))[: args.sample]:
            print(json.dumps(parse_product(p, rel, carbon), ensure_ascii=False, indent=2))
        return
    stats = build()
    n = stats["products"]
    print(f"Loaded {n} products into {DB_PATH.relative_to(ROOT.parent)}")
    for k, v in stats.items():
        if k != "products":
            print(f"  {k:<20} {v:>6}" + (f"  ({v / n:.0%})" if k not in ("substitution_rules", "skipped_deleted") else ""))


if __name__ == "__main__":
    main()
