# Primary Intake Execution Contract

**Status:** controlled local runtime verified; 10/10 groups and cleanup passed

**Contract version:** v1

**Prepared:** 2026-08-17

**Accepted:** 2026-08-17

**Accepted decision:** A committed new case queues a distinct
`WORKFLOW_START` outbox message. A requester receipt remains a separate
`REQUESTER_NOTIFICATION` message with its own destination and retry history.

## Purpose

This contract defines how the web form and REST webhook create the same kind
of primary case safely. It locks the boundary before any intake migration,
FastAPI route, session flow, n8n workflow, or runtime check is created.

| Item | v1 meaning |
| --- | --- |
| Input | 1 authenticated web-form submission or REST webhook request |
| Action | Validate identity and input, apply replay rules, then commit the case, creation event, and workflow-start intent atomically |
| Output | 1 traceable case reference or 1 explicit non-persistent error |
| Decision owner | Authentication and deterministic application code |

The intake layer stores the request and queues work. It does not classify the
request, call Ollama, retrieve policy, make an approval decision, call the
Service Desk Sandbox, or wait for n8n.

## Shared Domain Command

Both channels must map their transport-specific fields into this internal
command before case creation:

| Field | Rule |
| --- | --- |
| `source_channel` | Required enum set by the server: `WEB` or `WEBHOOK` |
| `external_request_id` | Required normalized source identifier, at most 100 characters |
| `requester_id` | Required internal UUID resolved from an active user with the `REQUESTER` role |
| `subject` | Required, non-blank, at most 200 characters |
| `message` | Required, non-blank, at most 20,000 characters |
| `attachment_metadata` | Optional JSON array; defaults to `[]` |
| `received_at` | Required UTC timestamp assigned or validated by the channel adapter |

`subject` and `message` are stored exactly as decoded from the accepted
request. Surrounding whitespace is inspected to reject a blank value but is
not removed from the stored original text.

Each attachment-metadata item may contain only `name`, `media_type`, and
`size_bytes`. `name` and `media_type` are required non-blank strings;
`size_bytes` is an optional non-negative integer. v1 stores metadata only. It
does not accept or persist file bytes, local paths, or remote credentials.

## Web-Form Channel

- `GET /requests/new` requires an authenticated application session and
  creates an opaque `WEB-<UUID v4>` submission ID.
- `POST /requests` accepts that submission ID, `subject`, `message`, optional
  attachment metadata, and a valid CSRF token.
- The server sets `source_channel = WEB`, obtains `requester_id` from the
  signed session, and sets `received_at` when the POST is accepted.
- The form body cannot select or override the requester.
- The same submission ID must be retained when the browser retries the same
  POST. Loading a genuinely new blank form creates a new submission ID.
- The signed session cookie is `HttpOnly` and `SameSite=Lax`. Its signing
  secret comes from the environment and is never committed or logged.
- Every submission rechecks that the session user is active and has the
  `REQUESTER` role; session possession alone is insufficient.

Unauthenticated browser requests redirect to the login page. An authenticated
user who is inactive or lacks the required role receives a forbidden result.
Neither result creates a case, event, or outbox row.

## REST-Webhook Channel

`POST /api/v1/requests` accepts UTF-8 JSON with this shape:

```json
{
  "external_request_id": "HR-PORTAL-000184",
  "requester_reference": "REQ-101",
  "subject": "Warehouse access request",
  "message": "Please grant read access to WMS for inventory checks.",
  "attachments": [],
  "received_at": "2026-08-17T09:30:00Z"
}
```

- The endpoint requires `Authorization: Bearer <token>`.
- The expected token comes from the environment and is compared in constant
  time. The token is never stored in PostgreSQL or written to logs.
- The server sets `source_channel = WEBHOOK`; a body field cannot override it.
- The webhook token authenticates the sending integration, not the requester.
- `requester_reference` is trimmed, uppercased, then matched exactly to
  `users.employee_reference`. The resolved user must be active and have the
  `REQUESTER` role.
- `received_at` must be a timezone-aware timestamp no later than server
  acceptance time and is stored in UTC.

An unknown, inactive, or unauthorized requester returns the same generic
forbidden result so the endpoint does not reveal which identity check failed.
No database row is created.

## Input Limits and Rejection

- The complete HTTP request body is limited to 256 KiB.
- At most 10 attachment-metadata items are accepted.
- Unknown JSON fields are rejected so misspelled input is not silently lost.
- Empty strings, malformed timestamps, malformed submission IDs, invalid
  attachment metadata, and values beyond the fixed limits are rejected before
  the creation transaction.
- Authentication secrets, session cookies, raw authorization headers, and
  password values are excluded from application logs and audit payloads.

These limits protect the local application, but character count alone does not
guarantee that later AI input fits the accepted 4,096-token context. The
analysis checkpoint must define its own input budgeting without altering the
stored original text. An oversized intake request is rejected explicitly.

## Idempotency and Conflicting Replay

The normalized source values create the lowercase SHA-256 idempotency key:

```text
sha256("<source_channel>|<external_request_id>")
```

`source_channel` is already the server-owned uppercase enum.
`external_request_id` is stored after removing only surrounding whitespace and
remains case-sensitive.

The uniqueness constraints on `cases.idempotency_key` and
`(source_channel, external_request_id)` remain the concurrency authority.
After a matching case is found, deterministic code compares the resolved
`requester_id`, exact `subject`, exact `message`, and canonical JSON attachment
metadata with the stored immutable values.

- The same key and same business input return the existing case. No case,
  event, outbox message, AI call, or downstream action is repeated.
- The same key with different business input returns
  `IDEMPOTENCY_CONFLICT`. No database row is added or changed.
- `received_at` is excluded from replay comparison because it is transport
  timing. The timestamp from the 1st accepted submission remains immutable.
- A uniqueness race is resolved by rolling back the losing insert, loading the
  committed case, and applying the same comparison. A database uniqueness
  exception is never exposed as the public response.

## Possible-Duplicate Fingerprint

Possible duplicates use a separate signal from idempotency. The application
normalizes the subject and message with Unicode NFKC, case folding, and
collapsed whitespace. It then creates canonical sorted-key JSON containing
the normalized `requester_id`, subject, and message and stores its lowercase
SHA-256 as `content_fingerprint`.

- A fingerprint is indexed but is not unique.
- A new external request ID always creates a new case, even when its
  fingerprint matches an older case.
- Attachment metadata is excluded because this check is a conservative text
  signal, not proof that 2 requests are identical.
- Intake leaves every new case in `RECEIVED`. The later deterministic analysis
  step checks matching case IDs and routes a possible duplicate through
  `ANALYZING` to `NEEDS_REVIEW` with an audit event.

This prevents a false-positive fingerprint from silently discarding a valid
request.

## Atomic Creation Transaction

After authentication and shape validation, a genuinely new request performs
exactly these writes in 1 PostgreSQL transaction:

1. Insert 1 `cases` row with immutable input, `current_state = RECEIVED`, and
   `version = 1`.
2. Insert sequence `1` in `case_events` with `from_state = NULL`,
   `to_state = RECEIVED`, and `event_type = CASE_RECEIVED`.
3. Insert 1 `outbox_messages` row with
   `message_type = WORKFLOW_START`, `destination = N8N_REQUEST_INTAKE`,
   `status = PENDING`, and `max_attempts = 3`.
4. Commit all 3 rows, then return the transport response.

The creation event uses `actor_type = USER` and the requester user ID for the
web form. It uses `actor_type = INTEGRATION` and no actor user ID for the
webhook because the webhook credential represents the sending integration.
The resolved requester remains authoritative in `cases.requester_id`.

The workflow-start idempotency key is:

```text
sha256("WORKFLOW_START|<case_id>")
```

Its immutable payload contains only:

```json
{
  "schema_version": "1",
  "case_id": "00000000-0000-0000-0000-000000000000",
  "case_reference": "CASE-2026-0001",
  "case_version": 1,
  "trigger_event": "CASE_RECEIVED"
}
```

n8n retrieves authoritative case data through the primary application later;
the outbox payload does not duplicate the original request text. Intake does
not make a direct n8n network call. If any of the 3 inserts fails, all 3 roll
back and the client receives a retryable unavailable result.

`WORKFLOW_START` is not a requester receipt. A later workflow transition may
create `REQUESTER_NOTIFICATION` with its own payload, destination,
idempotency key, attempts, and outcome.

## Case Reference

The implementation adds 1 PostgreSQL sequence and formats a new reference as
`CASE-<UTC year>-<sequence padded to at least 4 digits>`. The sequence prevents
concurrent creation from selecting the same reference. Sequence gaps after a
rollback are valid and must not be repaired or reused.

## Transport Results

The webhook returns:

| Result | HTTP | Body meaning |
| --- | ---: | --- |
| New case committed | `201` | Stable reference, `RECEIVED`, and `idempotent_replay: false` |
| Same request replayed | `200` | Existing reference, current state, and `idempotent_replay: true` |
| Missing or invalid integration token | `401` | `AUTHENTICATION_REQUIRED` |
| Requester lookup or role rejected | `403` | `REQUESTER_NOT_AUTHORIZED` |
| Same key with different input | `409` | `IDEMPOTENCY_CONFLICT` |
| Body exceeds 256 KiB | `413` | `REQUEST_TOO_LARGE` |
| Invalid request shape or value | `422` | `INVALID_REQUEST` |
| Atomic persistence unavailable | `503` | `INTAKE_UNAVAILABLE` with `retryable: true` |

Created and replay responses use this shape:

```json
{
  "case_reference": "CASE-2026-0001",
  "current_state": "RECEIVED",
  "idempotent_replay": false
}
```

Error responses use the existing project-wide shape:

```json
{
  "error_code": "IDEMPOTENCY_CONFLICT",
  "message": "The request identifier was already used for different input.",
  "retryable": false
}
```

The web form maps the same domain results to browser behavior: a new case or
replay redirects with HTTP `303` to its case page; invalid input re-renders the
form; a conflict returns HTTP `409`; and unavailable persistence returns HTTP
`503`. Browser messages do not expose internal UUIDs, hashes, secrets, or SQL
details.

## Current Schema Impact

The verified Stage 1 through Stage 5 migrations remain valid evidence for the
database slices they tested. They do not yet implement this intake contract.
The prepared `006_intake_execution.sql` additive migration:

- adds the case-reference sequence; and
- replaces the outbox message-type constraint so it also permits
  `WORKFLOW_START`.

Migration 006 and the controlled primary intake runtime passed their user-run
integration check on 2026-08-17. No new table was required. The existing
primary-delivery worker continues
to select only `DOWNSTREAM_ACTION`; it must never send a `WORKFLOW_START`
message to the Service Desk Sandbox. The later n8n delivery contract must
define how workflow-start attempts are acknowledged and recorded.

## Verified Implementation Evidence

The user-run consolidated local check passed 10/10 focused groups and proved:

1. both channels call the same domain creation service;
2. invalid authentication or requester authorization writes no rows;
3. a new request commits exactly 1 case, 1 creation event, and 1
   `WORKFLOW_START` message;
4. forced failure of any insert leaves none of those rows;
5. exact replay returns the same case without new rows;
6. conflicting replay returns HTTP `409` without mutation;
7. concurrent duplicate submissions create only 1 case;
8. different IDs with matching fingerprints create separate cases and remain
   review candidates;
9. original request input and workflow-start intent remain immutable; and
10. the primary delivery worker ignores `WORKFLOW_START` messages.

The controlled run committed 4 new fictional intake cases, 4
`CASE_RECEIVED` events, and 4 `WORKFLOW_START` messages. Exact replay created 0
duplicate rows, conflicting replay caused 0 mutations, and the forced rollback
left 0 partial rows. The test process exited with code 0 and removed all 3
containers and its Docker network.

This verifies the controlled local intake boundary. It does not verify an
interactive login that issues the signed session cookie, n8n consumption of
`WORKFLOW_START`, AI analysis, policy retrieval, requester notification, real
users, or production operation.
