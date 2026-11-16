"""Generates workflows/*.json in n8n's workflow export format.

The JavaScript for each Code node lives in this file so it can be reviewed and
diffed as code. Run `python tools/build_workflow.py` after editing.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NS = uuid.UUID("6f1c2b8e-1d2a-4f3b-9c4d-5e6f7a8b9c0d")  # stable ids between builds


def nid(name: str) -> str:
    return str(uuid.uuid5(NS, name))


LOG_HELPER = r"""
// Structured log helper: one JSON line to stdout (visible in n8n logs when
// CODE_ENABLE_STDOUT=true) and the same entry appended to item.logs so it is
// also visible in the execution view.
function log(item, step, outcome, extra = {}, level = 'info') {
  const entry = {
    ts: new Date().toISOString(),
    level,
    workflow: 'order-intake',
    execution_id: $execution.id,
    correlation_id: item.correlationId,
    step,
    outcome,
    ...extra,
  };
  console.log(JSON.stringify(entry));
  item.logs = [...(item.logs || []), entry];
  return item;
}
""".strip()

CONTEXT_JS = LOG_HELPER + r"""

// ---- Configuration: edit these values after import (no secrets here) ----
const CONFIG = {
  downstreamUrl: 'http://mock-api:8081/orders',     // the system you deliver to
  alertWebhookUrl: 'http://mock-api:8081/alerts',   // Slack/Teams/any generic webhook
  maxAttempts: 4,                                   // total attempts, including the first
  backoffBaseSeconds: 1,                            // 1s, 2s, 4s ... (with jitter)
  backoffMaxSeconds: 16,                            // cap per wait
};

const headers = $json.headers || {};
const item = {
  config: CONFIG,
  correlationId: headers['x-correlation-id'] || `n8n-${$execution.id}`,
  receivedAt: new Date().toISOString(),
  body: $json.body,
  attempt: 0,
  logs: [],
};
return { json: log(item, 'intake', 'received', { content_type: headers['content-type'] || null }) };
"""

VALIDATE_JS = LOG_HELPER + r"""

// Same contract as python-webhook-reliability-demo (order.created).
const b = $json.body;
const errors = [];
const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const str = (v, min = 1, max = 128) => typeof v === 'string' && v.length >= min && v.length <= max;

if (!isObj(b)) {
  errors.push({ field: 'body', message: 'JSON object expected' });
} else {
  if (!str(b.event_id, 8)) errors.push({ field: 'event_id', message: 'string, 8-128 chars' });
  if (b.event_type !== 'order.created') errors.push({ field: 'event_type', message: "must be 'order.created'" });
  if (!str(b.occurred_at) || Number.isNaN(Date.parse(b.occurred_at))) errors.push({ field: 'occurred_at', message: 'ISO-8601 timestamp' });
  if (!str(b.order_id, 1, 64)) errors.push({ field: 'order_id', message: 'string, 1-64 chars' });
  if (typeof b.currency !== 'string' || !/^[A-Z]{3}$/.test(b.currency)) errors.push({ field: 'currency', message: 'ISO-4217 code, e.g. EUR' });
  if (!isObj(b.customer)) {
    errors.push({ field: 'customer', message: 'object required' });
  } else {
    if (!str(b.customer.id, 1, 64)) errors.push({ field: 'customer.id', message: 'string, 1-64 chars' });
    if (typeof b.customer.email !== 'string' || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(b.customer.email)) errors.push({ field: 'customer.email', message: 'valid email' });
  }
  if (!Array.isArray(b.lines) || b.lines.length < 1 || b.lines.length > 100) {
    errors.push({ field: 'lines', message: 'array with 1-100 lines' });
  } else {
    b.lines.forEach((l, i) => {
      if (!isObj(l)) { errors.push({ field: `lines[${i}]`, message: 'object' }); return; }
      if (!str(l.sku, 1, 64)) errors.push({ field: `lines[${i}].sku`, message: 'string, 1-64 chars' });
      if (!Number.isInteger(l.quantity) || l.quantity < 1 || l.quantity > 1000) errors.push({ field: `lines[${i}].quantity`, message: 'integer 1-1000' });
      if (typeof l.unit_price !== 'string' || !/^\d{1,10}(\.\d{1,2})?$/.test(l.unit_price)) errors.push({ field: `lines[${i}].unit_price`, message: "decimal string, e.g. '12.50'" });
    });
  }
}

const item = { ...$json, valid: errors.length === 0, errors };
if (item.valid) {
  item.eventId = b.event_id;
  item.orderId = b.order_id;
  // Integer cents avoid floating point drift in the total.
  const cents = b.lines.reduce((sum, l) => sum + Math.round(Number(l.unit_price) * 100) * l.quantity, 0);
  item.downstreamPayload = {
    external_order_id: b.order_id,
    customer_id: b.customer.id,
    customer_email: b.customer.email,
    currency: b.currency,
    total: (cents / 100).toFixed(2),
    lines: b.lines,
  };
  log(item, 'validate', 'ok', { event_id: item.eventId, order_id: item.orderId });
} else {
  log(item, 'validate', 'rejected', { error_count: errors.length, fields: errors.map((e) => e.field) }, 'warn');
}
return { json: item };
"""

PREPARE_JS = LOG_HELPER + r"""

// Entered once from validation and again after every backoff wait.
const item = { ...$json, attempt: ($json.attempt || 0) + 1 };
delete item.decision;
return { json: log(item, 'deliver', 'attempt', { attempt: item.attempt, max_attempts: item.config.maxAttempts, event_id: item.eventId }) };
"""

CLASSIFY_JS = LOG_HELPER + r"""

// The HTTP node's error output only carries the error, so read the state
// from the paired item of "Prepare Attempt".
const state = { ...$('Prepare Attempt').item.json };
const err = $json.error || {};
const text = [err.message, err.description, err.httpCode].filter(Boolean).join(' ');
const statusCode = Number(err.httpCode || err.status || err.statusCode || (text.match(/\b([1-5]\d\d)\b/) || [])[1]) || null;

const RETRYABLE = new Set([408, 425, 429, 500, 502, 503, 504]);
const networkError = statusCode === null; // timeouts, DNS, connection refused
const retryable = networkError || RETRYABLE.has(statusCode);
const attemptsLeft = state.attempt < state.config.maxAttempts;

// Exponential backoff with "equal jitter": half fixed, half random, so a wait is never ~0.
const ceiling = Math.min(state.config.backoffMaxSeconds, state.config.backoffBaseSeconds * 2 ** (state.attempt - 1));
const waitSeconds = Math.max(1, Math.round((ceiling / 2 + Math.random() * ceiling / 2) * 10) / 10);

state.lastError = { statusCode, message: String(err.message || 'unknown error').slice(0, 300) };
state.decision = { retry: retryable && attemptsLeft, retryable, waitSeconds };

if (state.decision.retry) {
  log(state, 'deliver', 'retry_scheduled', { attempt: state.attempt, status_code: statusCode, retry_in_seconds: waitSeconds }, 'warn');
} else {
  log(state, 'deliver', 'failed', {
    attempt: state.attempt, status_code: statusCode,
    reason: retryable ? 'retries_exhausted' : 'permanent_error',
  }, 'error');
}
return { json: state };
"""

DELIVERED_JS = LOG_HELPER + r"""

const state = { ...$('Prepare Attempt').item.json };
state.result = { status: 'delivered', attempts: state.attempt, downstream: $json };
return { json: log(state, 'deliver', 'delivered', { attempt: state.attempt, event_id: state.eventId, order_id: state.orderId }) };
"""

FAILED_JS = LOG_HELPER + r"""

const item = { ...$json };
item.alert = {
  text: `Order webhook failed: ${item.orderId} (${item.decision.retryable ? 'retries exhausted' : 'permanent error'})`,
  details: {
    workflow: 'order-intake',
    execution_id: $execution.id,
    correlation_id: item.correlationId,
    event_id: item.eventId,
    order_id: item.orderId,
    attempts: item.attempt,
    status_code: item.lastError.statusCode,
    retryable: item.decision.retryable,
    error: item.lastError.message,
    replay_hint: 'Fix the cause, then re-send the original payload to the webhook (same event_id).',
  },
};
// Only references go into the alert: no payload body, no customer email.
return { json: log(item, 'alert', 'sending', { attempts: item.attempt, status_code: item.lastError.statusCode }, 'error') };
"""

REJECTED_JS = LOG_HELPER + r"""

return { json: log({ ...$json }, 'respond', 'rejected', { status: 422 }, 'warn') };
"""


def code(name, js, pos, each=True):
    params = {"jsCode": js.strip() + "\n"}
    if each:
        params = {"mode": "runOnceForEachItem", **params}
    return {
        "parameters": params,
        "id": nid(name),
        "name": name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": pos,
    }


def if_node(name, left, pos):
    return {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                "conditions": [
                    {
                        "id": nid(name + "-cond"),
                        "leftValue": left,
                        "rightValue": "",
                        "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                    }
                ],
                "combinator": "and",
            },
            "options": {},
        },
        "id": nid(name),
        "name": name,
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": pos,
    }


def respond(name, code_, body_expr, pos):
    return {
        "parameters": {
            "respondWith": "json",
            "responseBody": body_expr,
            "options": {
                "responseCode": code_,
                "responseHeaders": {"entries": [{"name": "X-Correlation-Id", "value": "={{ $json.correlationId }}"}]},
            },
        },
        "id": nid(name),
        "name": name,
        "type": "n8n-nodes-base.respondToWebhook",
        "typeVersion": 1.1,
        "position": pos,
    }


def link(*targets):
    return [{"node": t, "type": "main", "index": 0} for t in targets]


def main_workflow():
    nodes = [
        {
            "parameters": {
                "httpMethod": "POST",
                "path": "order-intake",
                "responseMode": "responseNode",
                "options": {},
            },
            "id": nid("Webhook: Order Intake"),
            "name": "Webhook: Order Intake",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [0, 300],
            "webhookId": nid("webhook-order-intake"),
        },
        code("Set Context & Config", CONTEXT_JS, [220, 300]),
        code("Validate Payload", VALIDATE_JS, [440, 300]),
        if_node("Valid?", "={{ $json.valid }}", [660, 300]),
        code("Log: Rejected", REJECTED_JS, [880, 480]),
        respond(
            "Respond 422 Invalid",
            422,
            "={{ JSON.stringify({ status: 'invalid', correlation_id: $json.correlationId, errors: $json.errors }) }}",
            [1100, 480],
        ),
        respond(
            "Respond 202 Accepted",
            202,
            "={{ JSON.stringify({ status: 'accepted', correlation_id: $json.correlationId, event_id: $json.eventId }) }}",
            [880, 200],
        ),
        code("Prepare Attempt", PREPARE_JS, [1100, 200]),
        {
            "parameters": {
                "method": "POST",
                "url": "={{ $json.config.downstreamUrl }}",
                "sendHeaders": True,
                "headerParameters": {
                    "parameters": [
                        {"name": "Idempotency-Key", "value": "={{ $json.eventId }}"},
                        {"name": "X-Correlation-Id", "value": "={{ $json.correlationId }}"},
                    ]
                },
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ JSON.stringify($json.downstreamPayload) }}",
                "options": {"timeout": 10000},
            },
            "id": nid("HTTP: Deliver to Downstream"),
            "name": "HTTP: Deliver to Downstream",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [1320, 200],
            "onError": "continueErrorOutput",
            "notes": "Retries are handled explicitly by the loop below (exponential backoff with jitter), "
            "so the node's built-in fixed-interval retry is off.",
        },
        code("Log: Delivered", DELIVERED_JS, [1560, 100]),
        code("Classify Failure", CLASSIFY_JS, [1560, 320]),
        if_node("Retry?", "={{ $json.decision.retry }}", [1780, 320]),
        {
            "parameters": {"amount": "={{ $json.decision.waitSeconds }}", "unit": "seconds"},
            "id": nid("Wait: Backoff"),
            "name": "Wait: Backoff",
            "type": "n8n-nodes-base.wait",
            "typeVersion": 1.1,
            "position": [2000, 220],
            "webhookId": nid("wait-backoff"),
        },
        code("Build Failure Alert", FAILED_JS, [2000, 440]),
        {
            "parameters": {
                "method": "POST",
                "url": "={{ $json.config.alertWebhookUrl }}",
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ JSON.stringify($json.alert) }}",
                "options": {"timeout": 10000},
            },
            "id": nid("Alert: Generic Webhook"),
            "name": "Alert: Generic Webhook",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [2220, 360],
            "retryOnFail": True,
            "maxTries": 3,
            "waitBetweenTries": 2000,
            "onError": "continueRegularOutput",
            "notes": "Works with Slack/Teams incoming webhooks or any endpoint that accepts JSON.",
        },
        {
            "parameters": {
                "fromEmail": "n8n-alerts@example.com",
                "toEmail": "ops@example.com",
                "subject": "={{ $json.alert.text }}",
                "emailFormat": "text",
                "text": "={{ JSON.stringify($json.alert.details, null, 2) }}",
                "options": {},
            },
            "id": nid("Alert: Email (optional)"),
            "name": "Alert: Email (optional)",
            "type": "n8n-nodes-base.emailSend",
            "typeVersion": 2.1,
            "position": [2220, 540],
            "disabled": True,
            "onError": "continueRegularOutput",
            "notes": "Disabled by default. Add SMTP credentials and enable to get email alerts too.",
        },
        {
            "parameters": {
                "content": "## Production-ready order intake (demo)\n"
                "Webhook -> validate -> 202 -> deliver with **exponential backoff + jitter** -> "
                "alert on permanent failure.\n\n"
                "Edit **Set Context & Config** after import. No secrets live in this workflow.\n"
                "See README and docs/runbook.md.",
                "height": 200,
                "width": 420,
                "color": 4,
            },
            "id": nid("Sticky: Overview"),
            "name": "Sticky: Overview",
            "type": "n8n-nodes-base.stickyNote",
            "typeVersion": 1,
            "position": [0, 0],
        },
    ]
    connections = {
        "Webhook: Order Intake": {"main": [link("Set Context & Config")]},
        "Set Context & Config": {"main": [link("Validate Payload")]},
        "Validate Payload": {"main": [link("Valid?")]},
        "Valid?": {"main": [link("Respond 202 Accepted"), link("Log: Rejected")]},
        "Log: Rejected": {"main": [link("Respond 422 Invalid")]},
        "Respond 202 Accepted": {"main": [link("Prepare Attempt")]},
        "Prepare Attempt": {"main": [link("HTTP: Deliver to Downstream")]},
        "HTTP: Deliver to Downstream": {"main": [link("Log: Delivered"), link("Classify Failure")]},
        "Classify Failure": {"main": [link("Retry?")]},
        "Retry?": {"main": [link("Wait: Backoff"), link("Build Failure Alert")]},
        "Wait: Backoff": {"main": [link("Prepare Attempt")]},
        "Build Failure Alert": {"main": [link("Alert: Generic Webhook", "Alert: Email (optional)")]},
    }
    return {
        "name": "Order Intake - production-ready demo",
        "nodes": nodes,
        "connections": connections,
        "active": False,
        "settings": {
            "executionOrder": "v1",
            "saveDataErrorExecution": "all",
            "saveDataSuccessExecution": "all",
            "saveExecutionProgress": True,
            "saveManualExecutions": True,
            "callerPolicy": "workflowsFromSameOwner",
        },
        "pinData": {},
        "meta": {"templateCredsSetupCompleted": True},
        "tags": [],
    }


ERROR_JS = r"""
// Runs when the main workflow fails in a way its own error branch did not catch
// (for example a bug in a Code node). Sends a short alert with references only.
const e = $json.execution || {};
const alert = {
  text: `n8n workflow crashed: ${($json.workflow || {}).name || 'unknown'}`,
  details: {
    workflow_id: ($json.workflow || {}).id,
    execution_id: e.id,
    execution_url: e.url,
    last_node: e.lastNodeExecuted,
    error: String((e.error || {}).message || 'unknown').slice(0, 300),
  },
};
console.log(JSON.stringify({ ts: new Date().toISOString(), level: 'error', step: 'global-error-handler', ...alert.details }));
return [{ json: { alert } }];
"""


def error_workflow():
    nodes = [
        {
            "parameters": {},
            "id": nid("Error Trigger"),
            "name": "Error Trigger",
            "type": "n8n-nodes-base.errorTrigger",
            "typeVersion": 1,
            "position": [0, 0],
        },
        code("Build Crash Alert", ERROR_JS, [220, 0], each=False),
        {
            "parameters": {
                "method": "POST",
                "url": "http://mock-api:8081/alerts",
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ JSON.stringify($json.alert) }}",
                "options": {"timeout": 10000},
            },
            "id": nid("Alert: Crash Webhook"),
            "name": "Alert: Crash Webhook",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [440, 0],
            "retryOnFail": True,
            "maxTries": 3,
            "waitBetweenTries": 2000,
        },
    ]
    connections = {
        "Error Trigger": {"main": [link("Build Crash Alert")]},
        "Build Crash Alert": {"main": [link("Alert: Crash Webhook")]},
    }
    return {
        "name": "Global Error Handler - demo",
        "nodes": nodes,
        "connections": connections,
        "active": False,
        "settings": {"executionOrder": "v1"},
        "pinData": {},
        "meta": {"templateCredsSetupCompleted": True},
        "tags": [],
    }


if __name__ == "__main__":
    out = ROOT / "workflows"
    out.mkdir(exist_ok=True)
    (out / "order-intake.json").write_text(json.dumps(main_workflow(), indent=2) + "\n")
    (out / "global-error-handler.json").write_text(json.dumps(error_workflow(), indent=2) + "\n")
    print("wrote", out / "order-intake.json", "and", out / "global-error-handler.json")
