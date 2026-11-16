# Fixtures

Made-up `order.created` payloads. The mock API (`mock/mock_api.py`) picks its behaviour from the order id, so each file exercises one path. All names, emails and ids are fictional (`example.com`).

| File | Order id | Expected HTTP response | What happens next |
| --- | --- | --- | --- |
| `valid-order.json` | `ORD-1001` | `202 accepted` | Delivered on attempt 1 |
| `invalid-order.json` | n/a | `422 invalid` with a field list | Nothing is sent downstream |
| `transient-failure-order.json` | `ORD-FLAKY-2001` | `202 accepted` | `503`, `503`, then delivered on attempt 3 |
| `rate-limited-order.json` | `ORD-RATE-2002` | `202 accepted` | `429`, then delivered on attempt 2 |
| `permanent-failure-order.json` | `ORD-REJECT-2003` | `202 accepted` | `422` downstream, no retry, alert sent |
| `downstream-down-order.json` | `ORD-DOWN-2004` | `202 accepted` | 4 attempts of `503` with backoff, then alert |

The mock counts calls per `Idempotency-Key`. Restart it (`docker compose restart mock-api`) before re-running the transient and rate-limited cases, or change their `event_id`.
