# Runbook: Order Intake workflow

> Public sample. Replace the mock URLs with real ones before using this pattern anywhere real.

## Purpose

Receives `order.created` webhooks, validates them, and delivers each order to a downstream API with retries. Raises an alert when an order cannot be delivered.

## At a glance

| Item | Value |
| --- | --- |
| Trigger | `POST /webhook/order-intake` (production URL, the workflow must be published or active) |
| Success response | `202 {"status":"accepted","correlation_id":...,"event_id":...}` |
| Invalid input | `422 {"status":"invalid","errors":[{"field":...,"message":...}]}`; nothing is sent downstream |
| Retries | Up to 4 attempts in total. Waits are about 1 s, 2 s, 4 s (jittered, capped at 16 s). Only `408/425/429/5xx` and network errors are retried. |
| Alerts | JSON `{"text": ..., "details": {...}}` to `alertWebhookUrl`. Optional email node, disabled by default. |
| Crash alerts | "Global Error Handler" workflow, if it is set as the error workflow |
| Logs | JSON lines in the n8n log (`CODE_ENABLE_STDOUT=true`) and in each execution's `logs` field |

## Configuration

Open **Set Context & Config** and edit `CONFIG`:

| Key | Default | Notes |
| --- | --- | --- |
| `downstreamUrl` | `http://mock-api:8081/orders` | The API that receives orders |
| `alertWebhookUrl` | `http://mock-api:8081/alerts` | Slack, Teams or any JSON webhook. If the URL contains a token, store it in an n8n credential or environment variable instead. |
| `maxAttempts` | `4` | Total attempts, including the first |
| `backoffBaseSeconds` | `1` | First wait ceiling |
| `backoffMaxSeconds` | `16` | Cap per wait |

Then:

1. Import `workflows/global-error-handler.json`, set its alert URL, and save it.
2. In **Order Intake → Settings → Error workflow**, select "Global Error Handler - demo".
3. Publish (activate) Order Intake.

## Day-to-day checks

- **Executions list:** filter by status "Error". With the error branch in place, delivery failures finish as *successful executions that sent an alert*; real errors here mean a bug or an n8n problem.
- **Alert channel:** every alert carries `execution_id` and `correlation_id`. Open the execution in n8n to see each attempt in `logs`.

## When an alert fires

1. Read `details.retryable` and `details.status_code`.
   - `retryable: true`, e.g. `503` after 4 attempts: the downstream was unavailable. Check its status page or owner.
   - `retryable: false`, e.g. `400` or `422`: the downstream rejected the data. Read `details.error` for the reason, such as an unknown SKU.
2. Fix the cause (wait for the downstream to recover, or correct the data or mapping).
3. Replay the order (below).
4. Note the incident and fix in the change log.

## Replaying a failed order

The workflow is safe to replay because the downstream call always sends `Idempotency-Key: <event_id>`.

- **Preferred:** ask the sender to resend the original webhook with the same `event_id`.
- **From n8n:** open the failed execution, copy the original `body` from the **Webhook: Order Intake** node output, and `POST` it again:

  ```bash
  curl -X POST https://<your-n8n>/webhook/order-intake \
    -H 'Content-Type: application/json' -H 'X-Correlation-Id: replay-<original-correlation-id>' \
    --data @order.json
  ```

- Do **not** change `event_id` when replaying. A new id defeats downstream de-duplication.
- If the downstream does not support idempotency keys, check first that the order really did not arrive.

## Changing the mapping

The downstream payload is built in **Validate Payload** (`item.downstreamPayload`). Change it there, then:

1. Run `tools/send_fixtures.sh` against a test instance.
2. Check one delivered execution and one alert.
3. Export the workflow and commit it.

## Known limits

- n8n's own execution storage is the record of attempts. Set execution retention (`EXECUTIONS_DATA_MAX_AGE`) to cover your audit needs.
- Waits under about 65 seconds stay in memory. If n8n restarts mid-wait, that delivery is lost, and the sender's resend (or a replay) is the recovery path. For very long outages, raise `backoffMaxSeconds` or move to a queue.
- Duplicate *incoming* webhooks are de-duplicated by the downstream via the idempotency key, not inside n8n. The Python companion repo shows receiver-side de-duplication.
