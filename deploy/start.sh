#!/bin/sh
# Container entrypoint: fetch Valio's data with the API key (never baked into the image), build the catalog, serve.
set -e
D="${VALIO_DATA_DIR:-data}"
if [ ! -f "$D/raw/valio_aimo_junction26_product_data.json" ]; then
  python -m data.load_data --download
elif [ ! -f "$D/valio.db" ]; then
  python -m data.load_data
fi
exec uvicorn ui.app:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips "*"
