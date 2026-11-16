"""Stdlib-only mock downstream API and alert sink for the n8n demo.

Behaviour is chosen by the order id, so each fixture triggers one path:
  *FLAKY*  -> 503 twice per Idempotency-Key, then 201
  *RATE*   -> 429 once (Retry-After: 1), then 201
  *REJECT* -> 422 every time (permanent, no retry)
  *DOWN*   -> 503 every time (retries exhausted, alert fires)
  other    -> 201

POST /alerts prints the alert and keeps the last 50 in memory (GET /alerts).
Run: python mock/mock_api.py  (listens on 0.0.0.0:8081, override with PORT)
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALLS: dict[str, int] = defaultdict(int)
ALERTS: list[dict] = []


def log(**fields):
    print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "service": "mock-api", **fields}), flush=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict, headers: dict | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence default access log
        pass

    def do_GET(self):
        if self.path == "/alerts":
            return self._send(200, {"alerts": ALERTS})
        if self.path == "/healthz":
            return self._send(200, {"status": "ok"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid json"})

        if self.path == "/alerts":
            ALERTS.append(body)
            del ALERTS[:-50]
            log(event="alert.received", text=body.get("text"))
            return self._send(200, {"ok": True})

        if self.path != "/orders":
            return self._send(404, {"error": "not found"})

        key = self.headers.get("Idempotency-Key", "none")
        order_id = str(body.get("external_order_id", ""))
        CALLS[key] += 1
        n = CALLS[key]
        if "REJECT" in order_id:
            status, resp, hdrs = 422, {"error": "unknown sku"}, None
        elif "DOWN" in order_id:
            status, resp, hdrs = 503, {"error": "maintenance"}, None
        elif "FLAKY" in order_id and n <= 2:
            status, resp, hdrs = 503, {"error": "temporarily unavailable"}, None
        elif "RATE" in order_id and n == 1:
            status, resp, hdrs = 429, {"error": "slow down"}, {"Retry-After": "1"}
        else:
            status, resp, hdrs = 201, {"id": f"mock-{order_id}", "attempt_seen": n}, None
        log(event="order.request", order_id=order_id, idempotency_key=key, call=n, status=status)
        return self._send(status, resp, hdrs)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    log(event="listening", port=port)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
