# Trusted Trade Agents – Junction 2026 (Valio Aimo challenge)

An open interface and operating model that lets external AI purchasing agents
find, compare and order from Valio Aimo, with guardrails and human approval.

Flow: **Request → Lookup → Order (fixed price) → Approval**

## Architecture
- `mcp_server/` – MCP server: the open tool interface external agents call
- `order_service/` – our own order service (Valio has no order API): order states, policy engine, audit log
- `data/` – product data, delivery reliability and carbon footprint, loaded into a local DB
- `agent/` – demo buyer agent
- `ui/` – approval UI for the human buyer

## Setup
```bash
cp .env.example .env   # add your keys
pip install -r requirements.txt
```

## Team
- Rachel Nguyen – MCP server, order service, agent, UI
- Akzhurek – data, search, substitution ranking

## License
MIT (all Junction submissions are MIT licensed)
