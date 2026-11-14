# Trusted Trade Agents – Junction 2026 (Valio Aimo challenge)

The Valio side of agentic ordering: an open interface (MCP + REST/OpenAPI) and an operating model that lets
**any** external AI purchasing agent find, compare and order from Valio Aimo, while the **rules decide** and
**humans sign off on exceptions**.

> Look before buying, check twice, stay reversible.

```
Buyer (person) ── plain words ──> Buyer agent (any vendor) ── JSON tool calls ──> MCP server (/mcp)
                                                                                   │
  find_offers: local catalog filter ──> live price + stock (top candidates only) ──┤──> signed offers + suggested plan
  place_order: Gate 1 request check ("did the agent understand?")                  │
               Gate 2 policy ("is it allowed?", buyer mandate) ───> placed | pending_approval | rejected
                                                                                   │
  Approval UI (human): buyer said vs. agent understood ──> approve / reject / remove line
  Order service + audit log: every outcome stored with its reason, for buyer and Valio
```

## Quick start

1. Put the Valio data files in `data/raw/` (from the resource portal):
   `valio_aimo_junction26_product_data.json`, `valio_aimo_junction26_delivery_reliability.csv`,
   `valio_aimo_junction26_carbon_footprint.csv`.
2. `cp .env.example .env` and set `VALIO_API_KEY` (and `OFFER_SIGNING_SECRET`, any long random string;
   `GEMINI_API_KEY` is optional, only for the free-text demo agent).
3. Run:

```bash
./run.sh            # venv + deps + loads data/valio.db + starts http://localhost:8000
```

Open http://localhost:8000 → **Agent console** → run the five scripted demo runs, then handle them in **Approvals**.

| URL | Who | What |
|---|---|---|
| `/mcp` | buyer agents | MCP (streamable HTTP), `Authorization: Bearer <customer key>` |
| `/v1/*`, `/docs` | buyer agents | the same tools as REST, OpenAPI at `/docs` |
| `/api/ui/*` | humans only | approve, reject, cancel, mandates (never agent tools) |
| `/` | humans | approval UI, orders, audit log, mandates, agent console |

Other entry points:

```bash
.venv/bin/python -m agent.buyer_agent --preset hotel-milk-partial            # scripted agent vs. running server
.venv/bin/python -m agent.buyer_agent "Need 1,000 L lactose-free milk by Monday"   # Gemini agent
BUYER_API_KEY=demo-hotel-aurora-key .venv/bin/python -m mcp_server.server     # MCP over stdio (Claude Desktop, Inspector)
.venv/bin/python -m pytest -q                                                  # offline tests (Valio APIs stubbed)
```

## Demo runs (Agent console)

| # | Buyer | Request | What it shows |
|---|---|---|---|
| 1 | Hotel Aurora | 1,000 L lactose-free milk by Monday | Cheapest milk has only 600 units → plan adds 400 of **Valio's official substitute** (ranked best). Substitution + over €1,000 limit → **pending approval**; manager sees *buyer said vs. agent understood*, can approve or drop a line |
| 2 | Hotel Aurora | weekly 60 L lactose-free milk | Within mandate, no flags → **placed automatically**, cancellable until cut-off (Sun 17:00) |
| 3 | Koivu School (allergen policy: nuts, peanuts, sesame) | 40 kg potatoes | The agent declares **no** allergens, but Valio applies the school's standing allergen list anyway. Fresh produce has **no allergen statement** → never treated as safe → escalated |
| 4 | Bistro Satama | 100 kg sea bass | Agent sends 100 of a lot-6 product → *"Quantity 100 … is not a multiple of 6. Try 96 or 102."* → agent fixes itself → placed |
| 5 | Hotel Aurora | 80 L lactose-free milk | Price rises between offer and order → `price_changed` → approval with old and new price |

**Reset demo** also seeds four past weekly 60 L milk orders for Hotel Aurora (marked "seeded demo history"), so
"our usual milk" and the unusual-quantity check have real history. Set `AI_CROSSCHECK=1` in `.env` to switch on the
Valio-side AI cross-check (it can only send orders to a human, never approve them).

Run 1 and 5 use a **demo overlay** (top bar) because the mock API is static (it ignores the requested date and
the cheapest milk has unlimited stock). The overlay only edits the live response, and every affected offer and order
is tagged `demo_scenario_override` in its provenance. Real exceptions from the live mock also show up without any
overlay: Terminal products (stock unknown, cut-off already passed), empty schedule lines, out-of-stock fresh salmon.

## Repository

```
data/          load_data.py (JSON + CSVs → SQLite), catalog.py (search, allergens, units), vocab.py (labels, allergen groups, synonyms)
order_service/ valio_client.py (APIs: cap 50, 2-min cache, quirks), offers.py (find_offers + plan), signing.py (HMAC offers),
               checks.py (gate 1), policy.py (gate 2), orders.py (states, approve/reject/cancel), db.py (orders + audit),
               buyers.json (customers + mandates), scenarios.json (demo overlays), crosscheck.py (optional AI flag)
mcp_server/    server.py: the 5 agent tools
ui/            app.py (FastAPI: /mcp, /v1, /api/ui), static/ (approval UI)
agent/         buyer_agent.py: reference buyer agent (Gemini or scripted), external MCP client
demo/          presets.json: the demo runs
docs/          INTERFACE.md: tools, data contract, rules, assumptions, next steps
tests/         offline tests of search, plan, both gates, signing
```

See **[docs/INTERFACE.md](docs/INTERFACE.md)** for the interface spec, rules, assumptions and the path to production.

## Team
- Rachel Nguyen – MCP server, order service, agent, UI
- Akzhurek – data, search, substitution ranking

## License
MIT (all Junction submissions are MIT licensed)
