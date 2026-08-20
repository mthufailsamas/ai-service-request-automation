# Human-Decision Resume-Orchestration Contract

**Status:** controlled local runtime verified; 6/6 groups and cleanup passed

**Contract version:** v1

## Purpose

This boundary durably hands each already committed human decision to local n8n
exactly once. It proves that the workflow can recognize which bounded route is
next without re-running the human command or letting n8n become the case-state
authority.

The handoff itself does not execute a route consumer. The approved downstream
action and the combined requester-information, service-agent routing, and
terminal-notification consumers were verified separately. Their boundaries are
recorded in `docs/APPROVED_ACTION_CONTRACT.md` and
`docs/HUMAN_RESUME_CONSUMERS_CONTRACT.md`.

## Durable Source and Intent

The append-only user event produced by the verified human-decision boundary is
the source of truth. A worker selects only these 6 event types:

| Human action | Committed event | Resume route | Committed state |
| --- | --- | --- | --- |
| `SUBMIT_INFORMATION` | `REQUESTER_INFORMATION_SUBMITTED` | `ANALYSIS_CONTINUATION` | `ANALYZING` |
| `CONFIRM_REVIEW` | `SERVICE_AGENT_REVIEW_CONFIRMED` | `ANALYSIS_CONTINUATION` | `ANALYZING` |
| `CORRECT_REVIEW` | `SERVICE_AGENT_CORRECTION_ACCEPTED` | `ANALYSIS_CONTINUATION` | `ANALYZING` |
| `REJECT_REVIEW` | `SERVICE_AGENT_REJECTED` | `TERMINAL_NOTIFICATION` | `REJECTED` |
| `APPROVE_REQUEST` | `APPROVAL_APPROVED` | `DOWNSTREAM_ACTION` | `READY_FOR_ACTION` |
| `REJECT_REQUEST` | `APPROVAL_REJECTED` | `TERMINAL_NOTIFICATION` | `REJECTED` |

The source event must be user-attributed and contain its verified command ID,
canonical input SHA-256, action, and committed result version. The worker then
creates 1 immutable `HUMAN_DECISION_RESUME` outbox message addressed to
`N8N_HUMAN_DECISION_RESUME`. Its SHA-256 idempotency identity is derived from
the exact case and event ID.

Migration `008_human_decision_resume.sql` only extends the accepted outbox
message-type constraint. It adds no table, dependency, mutable business row, or
second authority.

## Exact n8n Intent

The authenticated webhook accepts exactly these 8 fields:

- `schema_version = "1"`;
- canonical `case_id` and `case_reference`;
- committed `case_version`;
- `human_decision_reference = HD-<event_id>`;
- 1 accepted `action`;
- its exact `trigger_event`; and
- its exact `resume_route`.

The request also requires the immutable 64-character `Idempotency-Key` and the
temporary n8n webhook credential. Unknown fields, invalid identifiers, or a
mismatched action/event/route combination return HTTP `422` without calling the
primary API.

## Primary Acknowledgement

n8n calls:

```text
POST /internal/v1/cases/{case_id}/human-resume
Authorization: Bearer <primary workflow credential>
Idempotency-Key: <same outbox identity>
Content-Type: application/json
```

FastAPI and PostgreSQL remain authoritative. The endpoint locks the case and
requires the exact dispatched outbox payload, source human event, case
reference, version, action, event, route, and committed state. Acceptance
appends 1 same-state `HUMAN_DECISION_RESUME_ACKNOWLEDGED` integration event.
It does not increment the case version or repeat the human business mutation.

An exact replay returns the same `HDRESUME-<event_id>` reference. Conflicting
reuse returns HTTP `409`. n8n normalizes only an exact 8-field HTTP `200`
acknowledgement; malformed success is treated as a retryable invalid response.

## Delivery, Retry, and Recovery

The existing durable outbox machinery provides 1 claim, append-only delivery
attempts, a maximum of 3 attempts, retryable HTTP/transport classification, and
honest abandoned-lease recovery. 2 dispatchers cannot claim the same pending
message. A retry after a committed acknowledgement receives the stable replay
instead of creating a second acknowledgement.

`HUMAN_DECISION_RESUME` has its own message type and destination. Existing
workflow-start and Service Desk workers keep their narrow filters and cannot
claim it.

## Evidence Gate

Exactly 1 concise disposable runner covers 6 focused groups:

1. all 6 committed actions derive exact immutable intents;
2. strict n8n shape and distinct primary authentication;
3. all 3 bounded route acknowledgements through local n8n;
4. concurrent claim and exact acknowledgement replay;
5. transient retry, malformed success, fixed limit, and expired lease; and
6. aggregate evidence plus deferred-route and AI isolation.

The runner uses 10 fictional cases, temporary local credentials, disposable
PostgreSQL and n8n, 0 Ollama calls, and no hosted or paid service. Its runtime
evidence required all 6 groups and cleanup to pass.

The 1st user run passed groups 1 through 5 and cleanup. Group 6 did not produce
evidence because its test harness read a `dict_row` aggregate with positional
index `0`, raising `KeyError: 0`. The corrected check uses a named
`duplicate_count` field and compares every aggregate by name. This was a test
reporting defect after the route, retry, concurrency, and recovery behavior had
passed; the corrected rerun below supersedes that partial status.

The corrected user run then passed all 6/6 groups and cleanup on 2026-08-19.
Its 10 fictional cases created 10 durable intents, 9 successful
acknowledgements, 14 append-only delivery attempts, 1 bounded terminal invalid
acknowledgement, 0 duplicate acknowledgements, 0 unfinished intents, and 0
external AI calls. This verifies the controlled local handoff and route
classification boundary; the route consumers below remain separate.

## Deferred Boundaries

This gate verifies only durable handoff and route classification. The route
consumers, interactive portal, and scheduled recovery were verified through
their separate contracts. Deployment, real-user use, and production behavior
remain outside the local v1 evidence.
