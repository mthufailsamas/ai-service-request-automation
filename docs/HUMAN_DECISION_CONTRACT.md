# Human-Decision Application Contract

**Status:** controlled local runtime verified; 6/6 groups and cleanup passed

**Contract version:** v1

## Purpose

This boundary lets an authenticated person continue a case that automation has
deliberately paused. It keeps the accepted division of responsibility:

- the requester supplies missing information;
- a service agent confirms, corrects, or rejects a review case; and
- the assigned approver authorizes or rejects an access or data-change request.

Every accepted command changes the case and appends its audit event in 1
transaction. The command does not call AI, n8n, the Service Desk Sandbox, or a
notification provider.

## HTTP Boundary

The signed-session adapter exposes:

```text
POST /api/v1/cases/{case_reference}/human-decisions
Cookie: service_request_session=<signed value>
X-CSRF-Token: <token bound to the session and command_id>
Content-Type: application/json
```

The separate local-portal checkpoint now prepares issuance of this session. The
human-decision checkpoint itself reused the signed-cookie format and did not add
a login form, password flow, or session table.

The CSRF token is an HMAC of the signed cookie and exact `command_id`. A missing,
expired, altered, or incorrectly signed session returns HTTP `401`. An invalid
CSRF token returns HTTP `403` before domain mutation.

## Exact Command

Every command contains:

- `schema_version = "1"`;
- 1 UUID `command_id`;
- `expected_case_version` greater than `0`;
- exactly 1 accepted `action`; and
- only the fields permitted for that action.

Unknown fields, blank populated text, oversized values, an unsupported action,
or an invalid field combination returns HTTP `422` without mutation.

| Action | Required role and ownership | Required state | Accepted input | Next state |
| --- | --- | --- | --- | --- |
| `SUBMIT_INFORMATION` | Active `REQUESTER` who owns the case | `NEEDS_INFORMATION` | `information` up to 4,000 characters | `ANALYZING` |
| `CONFIRM_REVIEW` | Active `SERVICE_AGENT` | `NEEDS_REVIEW` | Optional `note`; latest structured proposal must be confirmable | `ANALYZING` |
| `CORRECT_REVIEW` | Active `SERVICE_AGENT` | `NEEDS_REVIEW` | Complete request type, summary, and exact 13-field object | `ANALYZING` |
| `REJECT_REVIEW` | Active `SERVICE_AGENT` | `NEEDS_REVIEW` | Required decision `note` | `REJECTED` |
| `APPROVE_REQUEST` | Active assigned `APPROVER` with the exact active system permission | `PENDING_APPROVAL` | Optional decision `note` | `READY_FOR_ACTION` |
| `REJECT_REQUEST` | Active assigned `APPROVER` with the exact active system permission | `PENDING_APPROVAL` | Required decision `note` | `REJECTED` |

These transitions match `docs/BUSINESS_PROCESS.md`. Information submission and
accepted agent details return to `ANALYZING`; this boundary does not skip the
later deterministic continuation and does not create an approval directly from
a review command.

## Service-Agent Accepted Details

A correction uses the same 5 request types and 13 nullable field names as the
accepted AI proposal. All 13 keys are present so an unrelated retained value
cannot hide in a partial patch. Required fields depend on request type, and
every unrelated field must be `null`.

The application deterministically rechecks:

- active requester identity;
- exact active managed-system resolution;
- `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL` impact and urgency;
- exact requester permission for access or data-change requests;
- exact active approver identity, role, system, and approval permission; and
- ownership of the referenced case for a status request.

Accepted values are stored in `case_details` with
`accepted_by_type = SERVICE_AGENT` and the exact agent user ID. The case keeps
its immutable original subject and message. A confirmation reads the latest
bounded structured proposal; malformed or incomplete proposal data cannot be
confirmed and must be replaced by an explicit correction.

## Approval Decision

An approval command locks and checks the existing `approvals` row. The signed-in
user must be its assigned approver, still have the `APPROVER` role, and still
hold `APPROVE_ACCESS` or `APPROVE_DATA_CHANGE` for the exact target system.

The approval row, case state and version, and append-only case event commit
together. An approval becomes `APPROVED` and moves the case to
`READY_FOR_ACTION`. A rejection becomes `REJECTED` and moves the case to
`REJECTED`.

## Transaction, Replay, and Concurrency

The domain service:

1. verifies the active actor;
2. locks the case row;
3. checks whether the case already contains this `command_id`;
4. returns the original acknowledgement for an exact replay;
5. rejects reuse of the ID with a different actor, action, or payload;
6. checks the expected state and version;
7. applies the role-specific business checks; and
8. commits the business row, incremented case version, and 1 event atomically.

The command ID, canonical input SHA-256, action, and committed result version
are stored in `case_events.event_payload`. The existing case-row lock makes 2
same-case commands serial: an exact duplicate becomes a no-effect replay, while
different commands using the same expected version leave only 1 committed
effect. No command table or migration is required for the small v1 case-scoped
workflow.

## Audit Events

The boundary appends only these user-attributed event types:

- `REQUESTER_INFORMATION_SUBMITTED`;
- `SERVICE_AGENT_REVIEW_CONFIRMED`;
- `SERVICE_AGENT_CORRECTION_ACCEPTED`;
- `SERVICE_AGENT_REJECTED`;
- `APPROVAL_APPROVED`; and
- `APPROVAL_REJECTED`.

Requester information is retained in the bounded event payload for the later
analysis-resume workflow. Accepted service-agent values live in `case_details`;
the event names their accepted fields without duplicating all values. Approval
evidence references the existing approval ID and decision.

## Checkpoint Boundary

This focused checkpoint verified only the human command and its atomic business
effect. The local portal, n8n resume handoff, resumed analysis, deterministic
agent routing, downstream action, requester notification, and scheduled
recovery were verified later through their separate contracts. Real-user and
production behavior remain outside the local v1 evidence.

## Evidence Gate

Exactly 1 concise disposable runner covers 6 focused groups:

1. strict session, CSRF, command, state, and role guards;
2. requester ownership, durable information, and exact replay;
3. service-agent confirmation, correction, rejection, and rollback;
4. assigned-approver identity, permission, approval, and rejection;
5. same-command replay and competing-command concurrency; and
6. aggregate evidence plus AI, delivery, and notification isolation.

The runner uses fictional users and cases, temporary credentials, disposable
PostgreSQL, the current FastAPI image, 0 AI calls, and no hosted or paid service.
Passing it will verify only this controlled local application boundary.

The user-run check passed all 6/6 groups on 2026-08-19 with 11 fictional cases,
8 durable human-command events, 0 duplicate command effects, 0 external AI
calls, and cleanup `PASS`. This verifies the controlled human-command
application boundary.

The separately verified handoff is defined in
`docs/HUMAN_RESUME_CONTRACT.md`. It consumes only the already committed event
reference and does not alter this verified human-command contract.
