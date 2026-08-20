# Workflow-Start Delivery Contract

**Status:** controlled local runtime verified; 8/8 groups and cleanup passed

**Contract version:** v1

**Prepared:** 2026-08-17

**Accepted:** 2026-08-17

**Controlled runtime verified:** 2026-08-17, 8/8 groups passed

**Analysis-continuation runtime verified:** 2026-08-19, 5/5 groups passed

## Purpose

This contract defines the first durable handoff from the primary PostgreSQL
outbox to the local n8n request-intake workflow. It covers only delivery of a
committed `WORKFLOW_START` intent and the safe transition from `RECEIVED` to
`ANALYZING`.

Contract acceptance by itself did not verify runtime behavior. The later
controlled local check passed all 8 groups; it still does not verify AI
analysis, policy retrieval, interactive login, requester notification, real
users, or production operation.

The accepted and verified analysis-continuation addendum below defines the
post-acknowledgement n8n boundary. It does not change the verified dispatcher
acknowledgement, claim, retry, or durable-start contract.

## Authority Boundary

- PostgreSQL and the primary FastAPI application remain authoritative for the
  case, workflow state, version, audit event, outbox, and delivery attempts.
- n8n orchestrates the process but never connects directly to the primary
  database and never writes workflow state by itself.
- A primary dispatcher sends only `WORKFLOW_START` messages whose destination
  is exactly `N8N_REQUEST_INTAKE`.
- The existing Service Desk delivery worker continues to send only
  `DOWNSTREAM_ACTION` messages to `service-desk-sandbox`.
- The original subject and message remain in the primary application. The
  workflow-start payload carries only stable references.

This separation prevents the orchestration tool from bypassing validation,
version checks, audit history, or the accepted state machine.

## Authentication and Secret Handling

The dispatcher calls this local n8n production webhook:

```text
POST /webhook/service-request-intake-v1
```

The request contains:

```text
Authorization: Bearer <N8N_WORKFLOW_START_TOKEN>
Idempotency-Key: <outbox_messages.idempotency_key>
Content-Type: application/json
```

n8n protects the webhook with credential-backed header authentication. Its
first primary-application call uses a different service credential:

```text
Authorization: Bearer <PRIMARY_WORKFLOW_TOKEN>
Idempotency-Key: <the same workflow-start key>
```

The 2 tokens are distinct, injected through local environment or n8n
credential configuration, never committed, returned to clients, or written to
application and audit payloads. The n8n service and primary application share
only the private local Compose network during controlled checks.

## Dispatcher Request

The dispatcher sends the immutable v1 outbox payload without adding request
text or trusted business results:

```json
{
  "schema_version": "1",
  "case_id": "00000000-0000-0000-0000-000000000000",
  "case_reference": "CASE-2026-0001",
  "case_version": 1,
  "trigger_event": "CASE_RECEIVED"
}
```

The dispatcher takes the `Idempotency-Key` from the outbox column, not from
caller-controlled JSON. A missing or malformed required value is a permanent
local contract failure and must not be sent.

## Claim Boundary

One dispatcher invocation handles at most 1 workflow-start message.

1. In a short database transaction, select the oldest eligible message with
   `FOR UPDATE SKIP LOCKED`.
2. Require `status = PENDING`, `message_type = WORKFLOW_START`, destination
   `N8N_REQUEST_INTAKE`, `available_at <= now()`, and attempts below the fixed
   limit.
3. Set the message to `PROCESSING`, increment `attempt_count`, set `locked_at`,
   and commit before making the network request.
4. Never hold a PostgreSQL transaction open while waiting for n8n.
5. In a second transaction, append the attempt outcome and update the outbox
   state together.

This reuses the accepted Stage 5 outbox pattern while keeping the n8n and
Service Desk destinations isolated.

## Durable Start Guard

After webhook authentication and shape validation, n8n calls:

```text
POST /internal/v1/cases/{case_id}/analysis-start
```

The body contains `schema_version`, `case_reference`, `expected_case_version`,
and `trigger_event`; the forwarded header contains the authoritative
workflow-start idempotency key.

The primary application performs these steps in 1 PostgreSQL transaction:

1. Lock the matching case row.
2. Confirm the case ID, case reference, `CASE_RECEIVED` trigger, v1 input, and
   the matching immutable `WORKFLOW_START` outbox intent.
3. Look for an existing `ANALYSIS_STARTED` event with the same workflow-start
   idempotency key before treating a later case version as a conflict.
4. If the same event exists, return its original stable reference without a
   state change, version increment, or new event.
5. Otherwise require `current_state = RECEIVED` and `version = 1`.
6. Update the case to `ANALYZING`, increment its version to `2`, and append
   exactly 1 `ANALYSIS_STARTED` event with `actor_type = INTEGRATION`.
7. Store only the workflow-start key and trigger metadata in the event payload,
   then commit both writes together.

The stable acknowledgement reference is derived from the immutable event ID:

```text
WFSTART-<event_id>
```

If the case is in another state and no matching start event exists, the primary
application returns a permanent conflict without mutation. A replay after the
case has progressed beyond `ANALYZING` still succeeds only when that exact
start event exists; it never moves the case backward.

## n8n Acknowledgement

n8n returns HTTP `200` only after the primary application has committed the
fresh transition or confirmed its exact replay. The response is:

```json
{
  "schema_version": "1",
  "status": "ACCEPTED",
  "workflow_start_reference": "WFSTART-2",
  "case_reference": "CASE-2026-0001",
  "accepted_transition": "RECEIVED->ANALYZING",
  "current_state": "ANALYZING",
  "case_version": 2,
  "idempotent_replay": false
}
```

On exact replay, `idempotent_replay` is `true` and the same
`workflow_start_reference` is returned. `current_state` and `case_version` may
reflect legitimate later progress.

The dispatcher accepts success only when HTTP is exactly `200`, the response
is a valid JSON object, `schema_version` and `status` match, the case reference
matches the request, and the workflow-start reference is non-blank. It stores
that stable reference in `delivery_attempts.downstream_reference`.

An n8n execution record is useful operational evidence, but it is not the
business acknowledgement. This avoids depending on edition-specific execution
metadata and keeps the durable result in the primary application.

## Post-Acknowledgement Analysis Continuation

AI analysis begins only after `Return Workflow Response` has sent the stable
workflow-start acknowledgement. The dispatcher response path never waits for
the model, deterministic validation, or analysis persistence.

The accepted implementation sequence is:

1. Keep the verified Webhook-to-Respond path and its external response schema
   unchanged.
2. On a valid primary HTTP `200` start acknowledgement only, preserve an
   internal continuation object beside `response_body_json`. It contains only
   `case_id`, `case_reference`, `expected_case_version`, and
   `workflow_start_reference`; the webhook response still exposes only the
   accepted acknowledgement body.
3. Set `expected_case_version` to `2`, derived from the accepted transition
   from start version `1`. Never substitute a later `case_version` returned by
   a workflow-start replay because the stable `ANALYSIS_STARTED` event remains
   sequence `2`.
4. Follow the normal input-data output of `Return Workflow Response` into the
   post-response continuation. No continuation node is connected to invalid
   input or rejected start branches.
5. Call the authenticated primary analysis endpoint exactly once for that n8n
   execution. Do not enable the HTTP node's automatic retry.
6. Classify only the bounded primary response for n8n operational evidence.
   PostgreSQL and FastAPI remain authoritative for the analysis attempt,
   validation result, case state, and retry limit.

The internal call is:

```text
POST /internal/v1/cases/{case_id}/analysis
Authorization: Bearer <PRIMARY_WORKFLOW_TOKEN>
Content-Type: application/json
```

```json
{
  "schema_version": "1",
  "case_reference": "CASE-2026-0001",
  "expected_case_version": 2,
  "workflow_start_reference": "WFSTART-2"
}
```

The call reuses the existing n8n-to-primary bearer credential because both
internal endpoints belong to the same primary service boundary. It sends no
subject, original message, requester data, model prompt, token, or database
identifier. The HTTP timeout is `200` seconds, longer than the provider's fixed
`180`-second timeout. The request uses native JSON with full-response capture
and `Never Error` so the next deterministic node can classify it.

### Continuation response classification

| Primary result | n8n operational classification | Required behavior |
| --- | --- | --- |
| Valid HTTP `200` analysis acknowledgement | `FINALIZED` | Accept the primary result even when deterministic validation chose review, information, or rejection |
| HTTP `503` with `outcome = RETRYABLE_FAILURE` | `RETRYABLE_FAILURE_RECORDED` | Make no inline 2nd call; retain the primary attempt as the authority for later recovery |
| HTTP `409` with `ANALYSIS_IN_PROGRESS` and `retryable = true` | `IN_PROGRESS` | Stop this duplicate continuation without another provider call |
| Other contract-valid retryable error | `RETRYABLE_CONTINUATION_FAILURE` | Record bounded operational failure only; later recovery owns resumption |
| Contract-valid permanent error | `PERMANENT_CONTINUATION_FAILURE` | Record bounded operational failure and do not retry automatically |
| Invalid HTTP `2xx` or malformed response | `INVALID_PRIMARY_RESPONSE` | Treat as an operational failure; never invent an analysis result |

An HTTP `200` analysis acknowledgement is successful orchestration when the
primary application finalized the provider output safely. `INVALID_OUTPUT` or
`NEEDS_REVIEW` is therefore not an n8n transport failure. n8n does not change,
repair, or reinterpret the primary validation decision.

The workflow-start outbox remains `SENT` after its valid acknowledgement even
when post-response analysis later fails. A duplicate or replayed workflow-start
execution may call the same continuation again; the primary analysis identity,
case lock, attempt limit, and exact replay rules prevent an extra completed
analysis or unbounded model calls.

This addendum does not yet guarantee automatic recovery if n8n stops after the
acknowledgement but before the analysis call reaches FastAPI. The durable
`ANALYZING` state and `ANALYSIS_STARTED` event make that gap detectable, but
the separate failure-recovery entry workflow remains responsible for resuming
it in a later checkpoint.

## Bounded Failure Handling

The existing outbox limit remains `max_attempts = 3`. Runtime retries use a
fixed 30-second delay; a controlled disposable check may set the delay to zero
without changing the production default.

| Result | Classification | Outbox result |
| --- | --- | --- |
| Valid HTTP `200` acknowledgement | `SUCCESS` | `SENT` |
| Timeout, connection failure, HTTP `408`, `429`, or any `5xx` | `TRANSIENT_FAILURE` | `PENDING`, or `FAILED` at attempt 3 |
| Any `2xx` response other than the valid HTTP `200` acknowledgement | `TRANSIENT_FAILURE` | `PENDING`, or `FAILED` at attempt 3 |
| HTTP `401`, `403`, `404`, `409`, or `422` | `PERMANENT_FAILURE` | `FAILED` |
| Other HTTP result | `PERMANENT_FAILURE` | `FAILED` |

Every finalized claim appends exactly 1 `delivery_attempts` row. Success has a
non-blank downstream reference and no error. Failure has no downstream
reference and includes a bounded response payload, error code, and readable
error message. Authentication headers and tokens are never stored.

The critical ambiguous case is a lost response after the primary transition
commits. The next bounded attempt sends the same key. The primary start guard
returns the original event reference, n8n forwards it, and the dispatcher can
mark the outbox `SENT` without a duplicate transition.

An invalid `2xx` acknowledgement is transient because the primary transition
may already have committed. If all 3 attempts remain ambiguous, the outbox is
honestly `FAILED` for reconciliation; the dispatcher does not invent a success
or move an already-started case backward.

## Abandoned Claim Recovery

A retry limit alone does not recover a worker that crashes after claiming a
message. Before normal claiming, the dispatcher may recover at most 1
`PROCESSING` workflow-start message whose `locked_at` is older than 60 seconds.

- Lock that row with `FOR UPDATE SKIP LOCKED`.
- Append a `TRANSIENT_FAILURE` attempt with HTTP status null, error code
  `DISPATCH_LEASE_EXPIRED`, and a message stating that the transport outcome is
  unknown.
- Return it to `PENDING` with the normal delay when attempts remain, or move it
  to `FAILED` when the limit has been reached.
- End that invocation after recovery; the next invocation performs any retry.

The unknown outcome is not reported as a confirmed network failure. A possible
late or repeated send remains safe because the primary start guard is
idempotent.

## Minimal n8n Workflow Shape

The later implementation contains only the nodes needed for this boundary:

1. authenticated production Webhook;
2. strict v1 payload validation;
3. authenticated HTTP call to the primary analysis-start endpoint;
4. deterministic mapping of primary errors; and
5. Respond to Webhook with the stable primary acknowledgement.

AI inference, retrieval, validation, approval, downstream delivery, and
notification are not added to this workflow checkpoint. They begin only after
the durable start integration has passed its own controlled local check.

The accepted continuation implementation later adds only 3 post-response
responsibilities to this same workflow:

1. preserve the stable internal continuation references while keeping the
   external response unchanged;
2. make 1 authenticated native-JSON call to the primary analysis endpoint; and
3. classify the bounded primary result for n8n execution evidence.

It does not add policy retrieval, a 2nd AI provider, inline retry, approval,
delivery, notification, or database access.

## Prepared Implementation

The implementation prepared on 2026-08-17 adds:

- `application/workflow_start.py` for strict local payload validation, atomic
  claim and finalization, bounded HTTP outcomes, stable acknowledgement checks,
  and expired-lease recovery;
- the authenticated FastAPI analysis-start endpoint in `application/main.py`;
- a 6-node n8n v1 workflow containing authenticated intake, strict validation,
  branching, the authenticated primary call with a native JSON request body,
  contract-aware response normalization into a new plain JSON object, and 1
  explicit non-streaming response;
- temporary credential rendering and encrypted n8n credential import without a
  committed token; and
- 1 disposable Compose runner with 8 focused integration groups.

The runner pins n8n `2.33.2`, keeps its SQLite state in a disposable named
volume, exposes no n8n host port, disables diagnostics and template traffic,
and removes the volume after the check. No primary migration or application
table was added. Static preparation and controlled runtime verification are
complete; the final user-run evidence is recorded in `PROJECT.md`.

## Verified Controlled Evidence

The consolidated disposable integration check proved:

1. invalid dispatcher authentication creates no start event or case transition
   and is recorded as a permanent outbox delivery failure;
2. 2 concurrent dispatchers claim a workflow-start message only once;
3. a fresh message creates exactly 1 `ANALYSIS_STARTED` event, moves the case
   to `ANALYZING`, and records 1 successful delivery attempt;
4. a lost-response retry returns the same start reference with no duplicate
   event or version increment;
5. transient failures retry only within the 3-attempt limit;
6. permanent rejection creates no state change, while an invalid-success
   acknowledgement retries only within the bounded limit;
7. an expired claim is recorded honestly and recovered within the limit; and
8. the Service Desk worker still ignores `WORKFLOW_START`.

The check used 8 fictional workflow cases and no Ollama call. It recorded 6
analysis-start events, 13 append-only attempts, 0 duplicate analysis-start
events, and 0 unfinished workflow messages. This verifies only the controlled
local workflow-start boundary, not the later analysis workflow or production
reliability.

## Verified Continuation Evidence

The implementation uses 1 concise disposable check and the verified fixture
provider. It does not rerun the 8-group workflow-start suite or call Ollama.
Its 5 focused groups proved:

1. the dispatcher receives and durably records the valid workflow-start
   acknowledgement before a deliberately delayed fixture analysis completes;
2. only a valid accepted start sends the exact authenticated 4-field analysis
   command and creates 1 durable analysis attempt plus validation record;
3. a repeated or concurrent continuation produces an in-progress response or
   exact replay without a duplicate provider call or analysis identity;
4. a retryable analysis result and malformed primary response are classified
   after the acknowledgement without changing the already-sent workflow-start
   outbox or causing an inline 2nd provider call; and
5. aggregate evidence contains no unfinished continuation-owned work and no
   policy retrieval, approval decision, downstream delivery, or requester
   notification.

The check uses fictional data, temporary local credentials, disposable
PostgreSQL and n8n state, 0 Ollama calls, and concise failure-only diagnostics.
Focused static checks passed on 2026-08-19 without starting a container or
calling a model. The later user run passed all 5/5 groups with 2 fictional
continuation cases, 2 durable analysis attempts, 1 deterministic validation
record, 0 duplicate analysis identities, 0 unfinished analysis attempts, 0
external AI calls, and cleanup `PASS`. This verifies only the controlled local
fixture boundary.

## Official n8n References

- n8n security audits include detection of unprotected webhooks:
  <https://docs.n8n.io/hosting/securing/security-audit/>
- n8n execution history and retry behavior are operational facilities; the
  primary application remains the business-state authority:
  <https://docs.n8n.io/workflows/executions/all-executions/>
- the Respond to Webhook node's normal output passes its input data onward,
  which permits post-response continuation without changing the response body:
  <https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.respondtowebhook/>
- n8n v2 CLI uses explicit workflow publishing after import:
  <https://docs.n8n.io/hosting/cli-commands/>
