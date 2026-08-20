# Scheduled Recovery and Operations Contract

**Status:** verified by controlled local runtime on 2026-08-20

**Contract version:** v1

## Purpose

This checkpoint adds the third accepted n8n entry workflow and a small admin-only
operations view without creating another business process or another database
table. It uses the existing outbox, append-only delivery attempts, case records,
and AI attempt records as operational evidence.

## Recovery Boundary

The n8n workflow has an authenticated test trigger and a 5-minute schedule. Both
call one strict primary command with a 60-second lease, immediate retry
availability, and a maximum of 20 claims per sweep. The primary endpoint accepts
only the existing type-and-destination pairs for workflow start, human-decision
resume, approved downstream action, and requester notification.

For each expired `PROCESSING` claim that lacks an attempt result, one transaction:

1. locks the bounded eligible set with `FOR UPDATE SKIP LOCKED`;
2. appends one `RECOVERY_LEASE_EXPIRED` transient-failure attempt with an unknown
   transport outcome; and
3. returns the outbox row to `PENDING` when attempts remain, or moves it to
   `FAILED` when the fixed limit has been reached.

The sweep does not invent success and does not call AI. Existing destination
idempotency keys protect a later worker retry. Ready messages remain the
responsibility of their existing bounded workers; this endpoint only repairs
abandoned ownership evidence.

## Operational Evidence

The administrator dashboard and JSON summary derive current counts from durable
records: cases by state, outbox type and status, ready retries, active and expired
claims, terminal delivery failures, recovery count, delivery-attempt p50/p95
elapsed time, and active or expired AI attempts. Anonymous and non-admin sessions
cannot read the summary. The view does not expose secrets, request bodies, or raw
model output.

Expired AI attempts are reported rather than changed by this sweep because the
verified analysis boundary requires the original immutable trigger reference and
provider-specific replay path. That recovery remains owned by `analyze_case` or
`analyze_resumed_case`; a generic scheduler must not guess it.

## Evidence Gate

One disposable 6-group runner covers the scheduled workflow definition, distinct
n8n and primary authentication, strict commands, all four accepted outbox types,
fixed attempt limits, replay, concurrent sweeps, destination and active-lease
isolation, admin-only evidence, and 0 AI calls. It uses fictional records,
disposable PostgreSQL and n8n storage, no model download, and no paid or hosted
service. The corrected user run passed all 6/6 groups: 6 of 8 fictional claims
were eligible and recovered, 5 returned to bounded retry, 1 reached its fixed
attempt limit, 2 active or unsupported claims remained isolated, duplicate
recovery attempts were 0, external AI calls were 0, and cleanup passed.

The new root `check.cmd` is a stable launcher. This checkpoint stores its runner
and Compose definition under `checks/`, so later checks can reuse the launcher
without adding another root-level command-and-Compose pair.
