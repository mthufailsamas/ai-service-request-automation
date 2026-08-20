# Approved Downstream-Action Contract

**Status:** verified by the controlled 6-group local runtime check

**Contract version:** v1

## Purpose

This boundary consumes only a verified human-resume acknowledgement whose route
is `DOWNSTREAM_ACTION`. It converts an assigned approver's committed approval
into 1 immutable Service Desk action, delivers it through the existing
outbox worker, and changes the case only after terminal delivery evidence is
durable.

The boundary supports approved `ACCESS_REQUEST` and
`DATA_CHANGE_REQUEST` cases. It makes 0 AI calls and does not handle analysis
continuation or requester notification.

## Durable Trigger

The materializer requires all of this evidence for the same case:

- current state `READY_FOR_ACTION` and the exact committed case version;
- 1 append-only `HUMAN_DECISION_RESUME_ACKNOWLEDGED` event with action
  `APPROVE_REQUEST` and route `DOWNSTREAM_ACTION`;
- the referenced user event `APPROVAL_APPROVED` with the same result version;
- 1 assigned `APPROVED` approval row;
- matching accepted case details, active target system, and active approver;
  and
- no prior outbox intent for the same case and acknowledgement identity.

The acknowledgement event is already durable PostgreSQL evidence created
through the verified local n8n boundary. The action is derived from that event
instead of relying on a volatile n8n post-response branch. A worker interruption
therefore cannot erase the trigger.

## Exact Service Desk Payloads

Both payloads contain exactly:

- `case_reference` and committed `case_version`;
- `action_type`;
- original case `title`;
- accepted case `summary`; and
- the exact action-specific `details` object.

`ACCESS_ACTION` details are `target_system`, `access_level`,
`approver_reference`, and `approval_reference`.

`DATA_CHANGE_ACTION` details are `target_system`, `record_reference`,
`requested_changes`, `approver_reference`, and `approval_reference`.

Unknown, unrelated, null, or blank business values are rejected before an
outbox message is created. The immutable delivery idempotency key is SHA-256 of
the exact case and human-resume acknowledgement identity.

## Delivery and Terminal State

The materializer inserts 1 existing `DOWNSTREAM_ACTION` message addressed to
`service-desk-sandbox`. The verified delivery worker retains its bounded claim,
3-attempt limit, transport classification, append-only attempts, and downstream
idempotency behavior.

A separate reconciler locks the case and reads only terminal outbox and attempt
evidence:

- `SENT` plus a successful attempt moves `READY_FOR_ACTION` to `COMPLETED`;
- `FAILED` plus a terminal attempt moves `READY_FOR_ACTION` to `FAILED`;
- `PENDING` or `PROCESSING` cannot change the case.

The transition increments the case version and appends exactly 1
`DOWNSTREAM_ACTION_COMPLETED` or `DOWNSTREAM_ACTION_FAILED` integration event.
The event stores the immutable outbox ID, delivery attempt number, outcome,
idempotency identity, action type, and downstream reference when available.
Exact materialization or reconciliation replay creates no duplicate intent,
record, transition, or event.

## Reference Scale Alignment

Primary case references already permit 4 or more numeric digits. Additive
sandbox migration `002_reference_scale.sql` aligns the downstream case
constraint and removes the old 9,999 sequence ceiling. Existing 4-digit
references remain valid; no table or record is replaced.

## Evidence Gate

Exactly 1 concise disposable runner covers 6 focused groups:

1. trusted acknowledgement, case, approval, and state guards;
2. concurrent idempotent materialization and exact payloads for both actions;
3. new Service Desk delivery and terminal completion for both actions;
4. downstream exact replay without a duplicate record or case transition;
5. permanent delivery failure and the exact `FAILED` transition; and
6. aggregate append-only evidence, reference scaling, and AI/notification
   isolation.

The runner uses 4 fictional approved cases, 2 isolated disposable databases,
the local Service Desk Sandbox, temporary credentials, 0 AI calls, and no paid
or hosted service. The user-run check passed all 6/6 groups on 2026-08-19 with
4 durable downstream intents, 4 append-only delivery attempts, 3 completed
cases, 1 exact terminal failed case, 0 duplicate terminal transitions, and 0
unfinished actions. Cleanup also passed.

## Deferred Boundaries

This checkpoint does not verify resumed analysis, rejected-case notification,
interactive login, scheduled recovery, the final 50-case evaluation,
deployment, real users, or production behavior.
