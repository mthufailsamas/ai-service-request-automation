# Full End-to-End Integration Contract

**Status:** verified by controlled local runtime on 2026-08-20

The first controlled runtime attempt stopped before the lifecycle groups because
the combined n8n setup used a stale hard-coded workflow ID. The setup now reads
both workflow IDs from their imported definitions, validates them, and the
runner's failure-log filter includes setup-service diagnostics. Cleanup passed;
a second attempt then reached the first safe-action materialization but exposed
an untyped optional PostgreSQL parameter. That lookup now uses one explicit
`bigint` parameter, and no matching ambiguous query pattern remains in the
application.

The corrected runner subsequently passed all 7/7 lifecycle groups: 6 cases
completed, 1 was rejected and notified, 8 durable analysis attempts and 6
Service Desk records were stored, duplicate terminal effects and unfinished
workflow work were both 0, hosted or paid AI calls were 0, and cleanup passed.

**Contract version:** v1

## Purpose

This checkpoint connects the already verified boundaries into one controlled
local lifecycle rather than rerunning their isolated test suites. It uses REST
intake, durable outbox dispatch, both accepted n8n workflows, fixture analysis,
grounded fixture retrieval, signed human decisions, human-resume consumers,
the Service Desk Sandbox, requester notification, reconciliation, replay, and
aggregate evidence.

## Safe Automatic Outcomes

`application/safe_action.py` closes the remaining deterministic delivery gap for
three outcomes that never require approval:

- a checked incident becomes `INCIDENT_TICKET`;
- a grounded visible policy answer becomes `POLICY_RESPONSE`; and
- an owned status lookup becomes `STATUS_RESPONSE`.

The materializer rechecks the current case, active requester role, accepted
details, exact analysis or service-agent source event, validation record, policy
citations, active system, or referenced-case ownership as applicable. It then
creates one immutable `DOWNSTREAM_ACTION` with an idempotency key bound to the
authoritative source event. The terminal reconciler rebuilds and compares the
payload before moving the case from `READY_FOR_ACTION` to `COMPLETED` or
`FAILED`. Approved access and data-change actions continue to use their stricter
approval-specific materializer.

## Evidence Gate

One 7-group disposable runner covers 7 fictional lifecycle cases:

1. automatic incident, grounded policy, and owned status completion;
2. access approval followed by downstream completion;
3. rejected data change followed by requester notification;
4. requester information followed by distinct resumed analysis;
5. service-agent correction followed by deterministic routing without another
   AI call;
6. exact intake, action, and reconciliation replay; and
7. aggregate terminal evidence with zero unfinished workflow work.

The runner includes 2 existing fictional seed cases only as reference data. It
uses deterministic fixture providers, local n8n, PostgreSQL, and the Service
Desk Sandbox. It downloads no model, makes no Ollama, hosted, or paid AI call,
and removes all disposable data during cleanup. Passing it is controlled local
integration evidence, not production, real-user, or model-quality evidence.

The stable root launcher is reused as `check.cmd end-to-end`; its Compose and
PowerShell files remain under `checks/`.
