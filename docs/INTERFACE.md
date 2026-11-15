# Interface spec: Valio Aimo × external purchasing agents

**Principle.** The buyer talks to their agent in plain words; the agent talks to Valio only in JSON. The server
never interprets language and no AI makes a decision on Valio's side: rules decide, so every outcome is
predictable and auditable. Safety never depends on the agent's quality.

## 1. Transport and identity

| | |
|---|---|
| Standard | **MCP** (Model Context Protocol, streamable HTTP at `/mcp`, or stdio). Same tools as **REST/OpenAPI** at `/v1/*` (`/docs`). |
| Auth | One API key per customer: `Authorization: Bearer <key>` (stdio: `BUYER_API_KEY`). The server derives `buyer_id` from the key; agents never send it, so an agent cannot claim another customer's mandate. |
| Scope | An agent sees only its own customer's offers, orders and audit log. |
| Human-only | approve, reject, cancel, mandate changes: `/api/ui/*` with a staff key. **Not** agent tools. |
| Not exposed | margins, supplier terms, other customers' data, list-vs-net price logic. Only catalog price, VAT rate and comparison price. |

## 2. Agent tools

| Tool | Input | Output |
|---|---|---|
| `get_need_schema` | – | declared-need JSON schema, supported labels, allergen groups, units, example |
| `find_offers` | `need` | signed `offers[]` with flags + provenance, `suggested_plan`, how the query was read |
| `place_order` | `lines[{offer_id, quantity}]`, the **same** `need` | `placed` / `pending_approval` / `rejected` with reasons, or precise `errors` |
| `get_order_status` | `order_id` | status, lines, reasons, cancel window |
| `get_audit_log` | `order_id?` | human-readable record of what the agent did and why |

### Declared need (Valio defines it, every agent translates into it)

```json
{
  "text": "Need 1,000 L lactose-free milk by Monday",       // buyer's words, verbatim (shown to the approver)
  "query": "milk",                                          // product words only, fi/sv/en
  "quantity": 1000, "unit": "L",                           // L | KG | SALES_UNIT
  "deliver_by": "2026-11-16",
  "required_labels": ["lactose-free"],                      // see get_need_schema
  "exclude_allergens": ["nuts"],
  "preferences": {"priority": "price", "max_unit_price_eur": 70, "allow_substitutes": true,
                  "accept_partial": false, "country": "fi"},
  "category": null, "product_codes": null,                  // optional narrowing
  "unresolved": null          // the agent's own doubt, e.g. "which cheese the chef liked is unknown" -> always a human
}
```

Required: `text, query, quantity, unit, deliver_by`. Labels: lactose-free, low-lactose, gluten-free, vegan,
vegetarian, egg-free, soy-free, no-added-sugar, organic, finnish, msc, asc, fairtrade, rainforest-alliance,
nordic-swan, eu-ecolabel. Allergen groups: milk, lactose, eggs, fish, crustaceans, molluscs, peanuts, nuts, gluten,
wheat, sesame, soy, celery, mustard, lupin, sulphites. Unknown terms are an error, never silently ignored.
Requirements outside the schema ("the one the chef likes", "fresh") stay visible in `text`, and the agent describes
what it could not express or verify in `unresolved`. Any order with `unresolved` set goes to a human, who sees the
doubt next to the order; the AI cross-check is the backstop for agents that guess without saying so. Supporting a
new requirement properly means extending the schema, not teaching the server to read language.

### Offer (excerpt)

```json
{
  "offer_id": "of_<base64 facts>.<hmac>",  "ref": "of_a270d2303a",
  "role": "primary | official_substitute", "substitute_rank": "best | better | good",
  "product": {"product_code": "464306", "name": "...", "labels": [...], "allergens": {"nuts": "free_from|contains|may_contain|unknown"},
              "order_step": 1, "litres_per_sales_unit": 1.0, "delivery_reliability": 1.0, "carbon_kg_co2e_per_kg": 1.1,
              "official_substitutes": ["465039", "464398", "469363", "464201"]},
  "price": {"unit_price_eur": 1.01, "vat_rate_pct": 13.5, "price_per_need_unit_eur": 1.01},
  "availability": {"logistic_model": "Stock", "stock_known": true, "available_units": 600, "available_date": "2026-11-16",
                   "closing_time": "2026-11-15T17:00:00+02:00", "cutoff_passed": false},
  "flags": ["supplier_stock_unknown", "order_cutoff_passed", "low_reliability", "allergen_unknown:nuts", ...],
  "eligible_for_plan": true, "ineligible_reasons": [],
  "provenance": {"price": {"source": "Valio price API", "as_of": "..."}, "availability": {...},
                 "allergens": {"source": "manufacturer data (GS1 Synkka)", "last_updated": "..."},
                 "carbon": {"source": "product-group average kg CO2e/kg", "per_product": false}}
}
```

## 3. Rules

### find_offers and the suggested plan (rules, no AI)
1. **Catalog filter (static data only):** full-text match on fi/sv/en names and brand with a visible synonym table
   (maito ↔ milk ↔ mjölk), drop names that say *X-free* for the searched X, required labels, drop CONTAINS / MAY_CONTAIN
   of excluded allergens (unknown kept, flagged), measurable in the need's unit, then the dominant product category
   among the best matches ("milk" never returns chocolate drinks or quark). Relevance is transparent: exact word 3,
   prefix 1.5, phrase bonus 2, modifier penalty ("milk *coffee*", "*sour* milk", "*ice* cream").
2. **Live data only for the top 25 candidates** + their official substitutes (≤ 50 codes per call, 2-minute cache;
   `place_order` always re-checks fresh).
3. **Hard filters for the plan:** price found, deliverable, no sales block, cut-off not passed, not later than
   `deliver_by`, unit conversion known, under `max_unit_price_eur`.
4. **Ranking:** verified allergen data before unknown → known stock (Stock) before Terminal → above the reliability
   minimum → buyer's priority (price per L/kg, reliability, or carbon) → preferred country → relevance.
5. **Fill:** take the top product up to its stock (rounded to the order step), then its **official substitutes**
   ranked good / better / best (label match, price difference, reliability, carbon), then the next product.

### Gate 1, request check: did the agent get it right?
| Check | On failure |
|---|---|
| Offer signature valid, issued to this buyer, < 15 min old | error: call find_offers again |
| Need identical to the one the offers were issued for (digest inside the signed offer) | error `need_mismatch` |
| Labels and allergens match the need | error with the conflicting label/allergen; unknown → flag `allergen_unknown:<group>` |
| Quantity multiple of `allowedLotSize` (piece units never below 1) | error "Quantity 100 is not a multiple of 6. Try 96 or 102." |
| Lines add up to the need (litres/kg per sales unit) | error if short (unless `accept_partial` → flag) or over by more than one step |
| Live stock and price re-check | error with the new available amount; price higher → flag `price_changed`; lower/same → new price, noted |
| Cut-off, Terminal, later date, low reliability | flags `order_cutoff_passed`, `supplier_stock_unknown`, `later_than_requested`, `low_reliability` |
| Quantity vs. this buyer's history | flag `unusual_quantity` (> factor × average of ≥ 2 orders) |
| Substitution | from the **signed** role or Valio's substitution list, not from the agent's claim |

It writes the plain-language **"agent understood"** summary shown next to the buyer's words. The summary
only says "verified free of …" when manufacturer data proves it; otherwise it says "must exclude … but allergen
data is missing". Only placed and approved orders count as order history.

### Gate 2, policy: is the agent allowed? (whole order, so splitting cannot dodge the limit)
| Outcome | When |
|---|---|
| `rejected` | sales block, not deliverable, no price at re-check |
| `pending_approval` | agent declared a doubt (`unresolved`); over spend limit; any substitution (if mandate says so); missing allergen data for an excluded allergen; any risk flag |
| `placed` | within mandate, no flags; cancellable until the earliest `closingTime` of its lines |

**Spend limit.** `spend_limit_eur` caps a single order *and* what the agent places on its own within 24 hours, so
splitting a purchase into smaller orders does not slip under the limit (`over_spend_limit_24h`). Orders a human
approved do not use up the agent's budget.

**Approval re-checks before placing.** Approved means placed, so on approval Valio re-checks what may have changed
while the order waited: the order cut-off (after `closingTime` the order is refused: reject it and re-order for a
later date), live stock (refused if too low) and live price (a lower price is used; a higher price is written onto
the order, which stays pending until the approver confirms it again).

Mandate (per buyer, configuration in `order_service/buyers.json`, editable in the UI and logged):
`spend_limit_eur, allergen_policy, substitutions_need_approval, min_delivery_reliability,
unusual_quantity_factor, price_increase_tolerance_pct`.
Fixed rules (not configurable): unknown supplier stock (Terminal), passed cut-off, later-than-requested
delivery, a price rise above tolerance, and missing allergen data for any excluded allergen always go to a human.

**Customer allergen policy.** `allergen_policy = {"exclude": [...], "confirmed_by", "confirmed_at"}` is the
customer's standing allergen list, answered once at onboarding (an empty list confirmed by someone means "none").
Valio merges it into **every** order's `exclude_allergens`, whatever the agent sends, so allergen safety never
depends on the agent remembering it. Agents can add allergens for one order but never remove the customer's.
CONTAINS / MAY_CONTAIN → product filtered out, or a request-check error if ordered anyway; no statement → human.
The approval card says where each requirement came from ("nuts (customer allergen policy)", "celery (this order)").
`find_offers` returns the enforced labels and allergens under `enforced`. The need fingerprint is computed from the
enforced need, so an agent may resend its own need or the enforced one.

Order states: `placed`, `pending_approval`, `approved` (= placed after sign-off), `rejected`, `cancelled`.

### Trust protocol
- **Signed offers:** `offer_id` = base64 JSON of product, unit price, available amount, delivery date, role, buyer,
  need digest, expiry + HMAC-SHA256 with a server-only secret. Tampering, another buyer's offer or expiry → rejected.
- **Provenance** on every claim: source and timestamp for price, availability (incl. whether stock is known),
  allergens (GS1 Synkka update date), reliability, carbon (group average, not per product).
- **AI cross-check** (`AI_CROSSCHECK=1`): an LLM compares the buyer's words (`need.text`) with the order summary,
  given facts from Valio (the ranking rule used, how often this buyer ordered each product before). It flags
  requirements the order cannot confirm: an item identified by memory ("the cheese the chef liked", "same as last
  time" with no history), a quality the product name does not confirm ("fresh"), or a different product, quantity
  or date. It may only *add* `possible_misunderstanding` (→ human approval); it can never approve, place or change
  an order, and if the LLM is unavailable the order proceeds on the rules alone. This catches what the schema
  cannot express, so safety does not depend on the buyer agent noticing it.

## 4. How we read Valio's data (assumptions, visible on purpose)
- **Terminal + 999999** = supplier stock unknown, never "unlimited". Stock values ≥ 10⁸ are placeholders for ample stock.
- **Empty `scheduleLines`** = not deliverable; `availableAmount 0` = out of stock; `availableDate > deliver_by` = late.
- **`closingTime`** = order cut-off for that delivery; used as the cancel window. The mock returns fixed cut-offs
  (Terminal: 12.11 15:00, already passed for Monday), so Terminal products are flagged and kept out of the plan.
- The mock **ignores `requestedDate`** and stock is static, hence the labeled demo overlay for runs 2, 4 and 5.
- **Litres/kg per sales unit:** GS1 unit conversions × units per sales unit, else parsed from the name, then
  confirmed or corrected by the price API (`catalogPrice / comparisonPrice`, e.g. 31.31 / 1.57 ≈ 20 L).
- **`allowedLotSize` 0.01** on cartons/pieces is treated as 1 (no fractional cartons); weight units keep the real step.
- **Allergens:** `CONTAINS > MAY_CONTAIN > FREE_FROM` per GS1 code; a group is safe only with an explicit FREE_FROM
  (or a matching free-from claim). No statement = `unknown`, never safe. GS1 code → group mapping is in `data/vocab.py`.
- **Labels** come only from structured GS1 claims and package marks, not from product names.
- **Prices** are catalog prices excl. VAT (B2B), fixed; the `customer` parameter carries the buyer's customer number.
- **Delivery reliability** exists for ~25 % of products; missing = `reliability_unknown` (informational).

## 5. Tech choices
- **MCP** because agent vendors already speak it (Claude, Gemini/ADK, OpenAI Agents); **REST/OpenAPI** mirror for
  everything else. Both call the same functions.
- **Python + FastAPI + MCP Python SDK 1.x** (pinned `<2`: 2.x renamed the server API), **SQLite** (one file, zero
  ops; Postgres is a drop-in for production), **vanilla JS UI** (no build step), **stdlib HMAC** for signing.
- **Gemini** for the reference buyer agent only; it is outside Valio's trust boundary.

## 6. Next steps to production
0. **Authentication and roles for people.** Today the human side uses one shared staff key (the public hackathon demo
   is open), and approvers type their own name. Production: real logins (SSO) so the audit log records the actual
   person, and role-based authorization:
   - **customer approver**: approve, reject and cancel *their own company's* orders, read their audit log
   - **customer admin**: also edit their allergen policy and propose limits within Valio's ceiling
   - **Valio admin**: set limit ceilings, issue and revoke customer API keys, read every customer's audit log
   - **agents**: unchanged, a customer API key limited to the five agent tools and that customer's data.
   Split the UI into a customer portal and a Valio admin page.
1. `place_order` calls Valio's real order backend instead of the reference order service.
2. Ed25519-signed offers with a published public key, so any agent or auditor can verify offers themselves.
3. Customer-specific prices/availability (the APIs already take `customer`); keys issued at onboarding, hashed, rotated; SSO for the approval UI.
4. Mandates that adapt: limits rise automatically while the approval history stays clean; monthly budgets.
5. Notifications (confirmation / daily digest with cancel link), approver roles per buyer.
6. Recipe-based ordering, last-minute events, standing-order drift detection, proactive offers on the same tools.
7. Schema extensions as buyers need them (brand, organic share, pack size), never free-text interpretation on Valio's side.
