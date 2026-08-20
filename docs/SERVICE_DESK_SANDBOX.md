# Service Desk Sandbox Contract

**Status:** Accepted v1 contract verified by user-run disposable runtime on
2026-08-17

## Purpose

The Service Desk Sandbox is 1 small local FastAPI service that behaves like an
external ticket and action system. It is designed to prove that the primary
application can deliver an approved outcome through an authenticated API,
handle retries, and avoid duplicate downstream records.

The sandbox owns its own database. The primary application may call its API but
must never write directly to the sandbox tables.

## Checkpoint Boundary

This document defines the accepted v1 contract. Its prepared implementation
contains:

- 1 authenticated record-creation endpoint;
- 1 authenticated record-read endpoint;
- 1 unauthenticated health endpoint;
- 2 sandbox-owned tables;
- exact idempotency behavior; and
- controlled transient and permanent failure behavior.

The checkpoint does not include the primary-application delivery client, retry
worker, n8n workflow, requester notification, browser fallback, or a real
third-party service desk.

## Beginner Mental Model

| Step | Meaning |
| --- | --- |
| Purpose | Record an outcome that the service-request workflow has already authorized |
| Input | 1 authenticated JSON request and 1 delivery idempotency key |
| Action | Validate the request, apply the controlled failure rule, and create or replay the downstream record |
| Output | A stable service-record reference or a structured retryable or permanent error |
| Decision | The primary system uses the HTTP result to mark its outbox message for success, retry, or terminal failure |

The sandbox does not decide whether a request is safe, complete, or approved.
Those decisions remain in the primary application.

## Network and Authentication Contract

- The sandbox runs only on the local Docker network during v1 development.
- `GET /health` is available for container health checks.
- Every `/api/v1` endpoint requires
  `Authorization: Bearer <SERVICE_DESK_SANDBOX_TOKEN>`.
- The token comes from an environment variable and is never committed.
- A missing or invalid token returns HTTP `401` and creates no database row.
- All request and response bodies use UTF-8 JSON.

## Create Service Record

### Request

`POST /api/v1/service-records`

Required headers:

```text
Authorization: Bearer <local secret>
Content-Type: application/json
Idempotency-Key: <64 lowercase SHA-256 characters>
```

Required body:

```json
{
  "case_reference": "CASE-2026-0002",
  "case_version": 3,
  "action_type": "INCIDENT_TICKET",
  "title": "Warehouse portal unavailable",
  "summary": "The warehouse portal has been unavailable since 09:15.",
  "details": {
    "affected_service": "Warehouse Management System",
    "impact": "HIGH",
    "urgency": "HIGH",
    "description": "Users cannot open the warehouse portal."
  }
}
```

Common rules:

- `case_reference` follows the accepted `CASE-YYYY-NNNN` format, with 4 or
  more digits in its final sequence.
- `case_version` is a positive integer and binds delivery to the accepted case
  state.
- `title` and `summary` are required non-blank strings.
- `details` is a required JSON object.
- The sandbox stores only accepted delivery fields. It does not receive model
  prompts, raw model output, passwords, tokens, or the complete original
  request unless a specific accepted action requires that text.

### Action Types

| `action_type` | Required `details` fields |
| --- | --- |
| `POLICY_RESPONSE` | `answer`, non-empty `citation_ids` |
| `INCIDENT_TICKET` | `affected_service`, `impact`, `urgency`, `description` |
| `ACCESS_ACTION` | `target_system`, `access_level`, `approver_reference`, `approval_reference` |
| `DATA_CHANGE_ACTION` | `target_system`, `record_reference`, `requested_changes`, `approver_reference`, `approval_reference` |
| `STATUS_RESPONSE` | `referenced_case`, `visible_state`, `public_update` |

Policy citation IDs must already have passed the primary application's
retrieval and visibility checks. Access and data-change actions must already
have an approved human decision. The sandbox validates shape but does not
repeat those authority decisions.

### Created Response

The 1st accepted request returns HTTP `201`:

```json
{
  "service_record_reference": "SR-2026-0001",
  "status": "ACCEPTED",
  "idempotent_replay": false
}
```

The sandbox creates the service record and its `RECORD_CREATED` event in 1
database transaction.

Additive migration `002_reference_scale.sql` aligns the accepted case-reference
suffix with the primary application and permits service-record sequence values
beyond 9,999. The verified approved-action runner exercised this change with
5-digit fictional references as part of its 6/6 passing local check.

### Idempotent Replay

The sandbox canonicalizes the JSON body as UTF-8 with sorted object keys and no
insignificant whitespace, then stores its SHA-256 hash.

- The same idempotency key and same canonical request hash return the existing
  record with HTTP `200` and `idempotent_replay: true`.
- A replay never creates another service record.
- The same idempotency key with a different request hash returns HTTP `409`
  with error code `IDEMPOTENCY_CONFLICT`.
- Each authenticated call appends 1 trace event so replay and conflict behavior
  remains auditable.

## Read Service Record

`GET /api/v1/service-records/{service_record_reference}`

The authenticated endpoint returns the accepted record fields and creation
time. An unknown reference returns HTTP `404`. v1 has no list, update, or delete
endpoint.

## Controlled Failure Contract

Controlled failures are enabled only when
`SERVICE_DESK_SANDBOX_TEST_MODE=true`. The optional test header is:

```text
X-Sandbox-Test-Outcome: TRANSIENT_ONCE
```

Allowed values:

- `TRANSIENT_ONCE`: the 1st authenticated request for the exact idempotency key
  and request hash records a `TRANSIENT_FAILURE` event and returns HTTP `503`
  with `retryable: true`. The next exact request proceeds normally.
- `PERMANENT_FAILURE`: the key and request hash record a
  `PERMANENT_FAILURE` event and return HTTP `422` with `retryable: false`.
  Later exact requests remain terminal and do not create a service record.

The header is rejected with HTTP `400` when test mode is disabled or its value
is unsupported. Controlled failure events are synthetic integration fixtures,
not evidence of a real service-desk outage.

Structured error bodies use this shape:

```json
{
  "error_code": "SANDBOX_TEMPORARILY_UNAVAILABLE",
  "message": "The controlled transient failure is active.",
  "retryable": true
}
```

## Sandbox Database Contract

### `service_records`

| Column | Type | Rule |
| --- | --- | --- |
| `service_record_id` | `uuid` | Primary key |
| `service_record_reference` | `varchar(32)` | Required and unique; `SR-YYYY-NNNN` format with 4 or more final digits |
| `delivery_idempotency_key` | `char(64)` | Required and unique lowercase SHA-256 |
| `request_sha256` | `char(64)` | Required lowercase SHA-256 of canonical JSON |
| `source_case_reference` | `varchar(32)` | Required exact case reference with 4 or more final digits |
| `source_case_version` | `integer` | Required and greater than `0` |
| `action_type` | `varchar(30)` | 1 of the 5 accepted action types |
| `title` | `varchar(200)` | Required and non-blank |
| `summary` | `text` | Required and non-blank |
| `details` | `jsonb` | Required JSON object |
| `status` | `varchar(20)` | `ACCEPTED` in v1 |
| `created_at` | `timestamptz` | Required |

Accepted records are immutable. A revised outcome uses a new case version and
new delivery idempotency key.

### `service_record_events`

| Column | Type | Rule |
| --- | --- | --- |
| `service_record_event_id` | `bigserial` | Primary key |
| `service_record_id` | `uuid` | Nullable foreign key for failures before record creation |
| `delivery_idempotency_key` | `char(64)` | Required lowercase SHA-256 |
| `request_sha256` | `char(64)` | Required lowercase SHA-256 |
| `sequence_number` | `integer` | Required and greater than `0` |
| `event_type` | `varchar(30)` | `RECORD_CREATED`, `IDEMPOTENT_REPLAY`, `IDEMPOTENCY_CONFLICT`, `TRANSIENT_FAILURE`, or `PERMANENT_FAILURE` |
| `event_payload` | `jsonb` | Required JSON object |
| `occurred_at` | `timestamptz` | Required |

`delivery_idempotency_key` and `sequence_number` are unique together. Events
are append-only. A failure before successful record creation keeps
`service_record_id` null while preserving the exact key and request hash.

## HTTP Decision Table

| HTTP result | Meaning for the primary system |
| --- | --- |
| `200` | Exact replay; mark delivery successful using the existing reference |
| `201` | New record created; mark delivery successful |
| `400` | Invalid contract or disabled test header; permanent failure |
| `401` | Invalid local service credential; permanent configuration failure |
| `404` | Read target does not exist; do not invent a record |
| `409` | Idempotency key reused with different content; permanent conflict |
| `422` | Controlled permanent rejection; do not retry |
| `503` | Controlled transient failure; retry only within the primary attempt limit |

## Verified Acceptance Evidence

The accepted implementation required reproducible tests to prove:

- HTTP `201` creates exactly 1 record and 1 creation event;
- an exact replay returns HTTP `200` with the same reference;
- a conflicting replay returns HTTP `409` and creates no duplicate record;
- `TRANSIENT_ONCE` returns HTTP `503` once and succeeds on the bounded retry;
- `PERMANENT_FAILURE` returns HTTP `422` and remains terminal;
- invalid authentication returns HTTP `401` without persistent business data;
- service records are immutable and events are append-only; and
- the disposable stack keeps the sandbox database isolated from the primary
  database.

The user-run disposable check passed all 8/8 HTTP and database contract groups
on PostgreSQL 17.11 on 2026-08-17. It created exactly 2 service records and 7
append-only events, recovered from 1 controlled transient failure, preserved 1
controlled permanent failure, and exited with code 0. The runner then removed
its 3 containers and Docker network.

This result verifies the isolated local sandbox only. The primary delivery
client, outbox worker, n8n orchestration, and end-to-end lifecycle were later
verified through their separate controlled contracts.
