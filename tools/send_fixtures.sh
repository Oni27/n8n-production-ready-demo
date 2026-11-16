#!/usr/bin/env bash
# Sends every fixture to the production webhook and prints the HTTP status.
# Usage: tools/send_fixtures.sh [base_url]   (default http://localhost:5678)
set -euo pipefail
BASE="${1:-http://localhost:5678}"
cd "$(dirname "$0")/.."
for f in fixtures/*.json; do
  name="$(basename "$f" .json)"
  code=$(curl -s -o /tmp/n8n-demo-response.json -w '%{http_code}' \
    -H 'Content-Type: application/json' -H "X-Correlation-Id: demo-$name" \
    --data @"$f" "$BASE/webhook/order-intake")
  printf '%-28s %s  %s\n' "$name" "$code" "$(head -c 140 /tmp/n8n-demo-response.json)"
done
echo
echo "Alerts received by the mock (after retries finish, ~10 s):"
echo "  curl -s http://localhost:8081/alerts"
