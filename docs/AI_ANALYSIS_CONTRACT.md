# AI-Analysis Contract

**Status:** controlled persistence, fixture runtime, local adapter, and continuation verified

**Contract version:** v1

**Prepared:** 2026-08-17

**Accepted:** 2026-08-17

## Purpose

This contract defines how 1 case already in `ANALYZING` receives a bounded
local-AI proposal, deterministic validation, durable evidence, and a safe next
decision. It translates the verified `qwen3:4b-instruct` suitability result
into an application boundary without making the model authoritative.

The checkpoint covers request classification, summary, field extraction,
source evidence, deterministic validation, and analysis persistence. It does
not retrieve policy chunks, answer policy questions, approve consequential
actions, deliver downstream records, notify requesters, or claim production
reliability.

## Beginner Mental Model

| Step | Input | Action | Output | Authority |
| --- | --- | --- | --- | --- |
| Read | Immutable case text | Check state, version, and input budget | Analysis input | Primary application |
| Propose | Subject and original message | Return structured JSON | Untrusted proposal | Local AI or test fixture |
| Validate | Proposal, source text, and reference data | Apply exact deterministic rules | Validation decision | Primary application |
| Commit | Checked result | Store evidence and apply 1 allowed transition | Durable analysis outcome | PostgreSQL and primary application |
| Continue | Accepted analysis | Retrieve, approve, or deliver in later checkpoints | Next workflow step | Later deterministic boundary or person |

The simple rule is: the model proposes; application code checks; PostgreSQL
records; people authorize consequential work.

## Entry Boundary

Analysis may start only when all of these facts are true:

- the case exists and is in `ANALYZING`;
- the case has the matching `ANALYSIS_STARTED` event and workflow-start
  reference;
- the expected case version still matches;
- the original subject and message still produce the expected input hash; and
- no completed analysis for the same input, prompt contract, and model already
  exists.

The accepted n8n continuation calls the authenticated primary endpoint only
after the workflow-start webhook acknowledgement has already been returned.
The workflow-start dispatcher must never wait for the local model. The stable
continuation version is `2`, matching the original `ANALYSIS_STARTED` event;
it is not replaced by a later case version returned on workflow-start replay.

The planned internal request contains only stable references:

```json
{
  "schema_version": "1",
  "case_reference": "CASE-2026-0001",
  "expected_case_version": 2,
  "workflow_start_reference": "WFSTART-42"
}
```

The existing n8n-to-primary bearer credential protects this internal call.
n8n never receives the original request text and never writes the primary
database directly. The primary application loads the immutable text itself.
n8n makes at most 1 analysis call per workflow execution, uses a 200-second
HTTP timeout, and performs no inline retry. FastAPI and PostgreSQL own exact
replay, the maximum 2 provider attempts, and deterministic finalization.

## Canonical Analysis Input

The provider receives only:

- `subject`, stored exactly as accepted by intake;
- `original_message`, stored exactly as accepted by intake; and
- the prompt and JSON schema fixed by this contract.

Requester permissions, internal UUIDs, password data, tokens, cookies,
attachment paths, and database credentials are never sent to the model.
Attachment metadata and file content are outside v1 analysis.

`input_sha256` is the lowercase SHA-256 of canonical UTF-8 JSON containing only
`subject` and `original_message`, with sorted keys, no insignificant spaces,
and non-ASCII characters preserved. The original text is never normalized or
replaced before hashing.

### Input budget

Intake safely stores messages up to 20,000 characters, but that does not imply
that every accepted message fits the selected 4,096-token model context. The
initial AI-analysis input is limited to 8,000 combined characters across the
subject and original message.

An oversized case is not truncated, summarized, split, or silently sent to the
model. It records a `SKIPPED` analysis outcome with 0 model tokens and routes
to `NEEDS_REVIEW`. The original request remains unchanged for a service agent.

The 8,000-character limit is a conservative v1 device guard for the accepted
English and Indonesian scope. A later measured tokenizer or chunking design
may replace it only through a new contract version.

## Provider Interface

The application exposes 1 small provider operation:

```text
analyze(subject, original_message) -> provider result
```

The provider result contains:

- `model_name`;
- `model_identifier`;
- the structured proposal;
- wall time in milliseconds;
- input-token count; and
- output-token count.

There are exactly 2 v1 implementations:

1. `OllamaAnalysisProvider` for the real local model; and
2. `FixtureAnalysisProvider` for deterministic automated tests.

The fixture provider returns only explicitly configured fictional proposals.
It never calls Ollama and never appears in the UI, documentation, or metrics as
a real AI result.

## Ollama Runtime Contract

- Model: `qwen3:4b-instruct` already installed by the user.
- Model selection: no automatic download and no paid fallback.
- Prompt contract: `analysis-v1`, derived from benchmark contract v2.
- Output: strict JSON schema.
- Temperature: `0`.
- Thinking: disabled.
- Context: 4,096 tokens.
- Maximum generated tokens: 512.
- Model-call concurrency: 1.
- Per-call timeout: 180 seconds.
- Warm retention during a controlled run: 10 minutes.

The application never starts a second simultaneous model call merely to reduce
latency. If Ollama is unavailable, times out, or returns a retryable local
server failure, deterministic retry rules apply.

## Exact Proposal Schema

The provider proposal contains exactly these top-level fields:

```json
{
  "request_type": "access_request",
  "summary": "Read-only dashboard access for weekly reporting.",
  "fields": {
    "policy_topic": null,
    "question": null,
    "affected_service": null,
    "incident_description": null,
    "impact": null,
    "urgency": null,
    "target_system": "Sales Dashboard",
    "requested_access_level": "read-only",
    "business_reason": "prepare the weekly regional report",
    "approver_id": "MGR-104",
    "record_reference": null,
    "requested_changes": null,
    "case_reference": null
  },
  "evidence": [
    {
      "field": "approver_id",
      "quote": "MGR-104"
    }
  ]
}
```

`request_type` is exactly 1 of:

- `policy_question`;
- `incident_report`;
- `access_request`;
- `data_change_request`; or
- `status_request`.

The application maps those values to the existing uppercase database enums.
All 13 field names are always present. A missing or unrelated value is JSON
`null`, never an invented default or an omitted key.

### Output bounds

| Value | Maximum |
| --- | ---: |
| Serialized proposal | 32 KiB UTF-8 |
| Summary | 500 characters |
| Policy topic, affected service, or target system | 200 characters |
| Question, incident description, or business reason | 2,000 characters |
| Requested changes | 4,000 characters |
| Requested access level | 80 characters |
| Approver ID | 50 characters |
| Record reference | 100 characters |
| Case reference | 32 characters |
| One evidence quote | 500 characters |
| Evidence items | 13, at most 1 per field |

`impact` and `urgency` are either null or one of `low`, `medium`, `high`, and
`critical`, compared case-insensitively and stored as the uppercase database
enum after validation.

The model does not return `missing_fields`, a route, a workflow state, an
approval decision, a priority, or a downstream payload. Application code
derives those results. A separate `policy_search_query` is not added in v1;
later retrieval uses the original request plus the checked policy question so
a shortened AI field cannot replace the source.

## Evidence Rules

Every evidence item contains exactly `field` and `quote`.

- `field` must name 1 of the 13 proposal fields.
- `quote` must be a non-blank, exact, contiguous, case-sensitive span from the
  stored subject or original message.
- An evidence field must have a non-null proposed value.
- Duplicate evidence fields are invalid.
- Every non-null field used by deterministic routing must have valid evidence.
- Evidence for an unrelated field is invalid.
- Summary text is not accepted as evidence.

Missing evidence does not cause the application to invent support. It produces
a deterministic review result.

The older Stage 3 database fixture uses `field_name` only to test JSONB storage.
It predates this runtime provider contract and is not a valid runtime proposal.
The canonical runtime key is `field`, matching the verified model benchmark.

## Deterministic Validation

The primary application performs these checks in order:

1. verify the exact top-level and nested JSON shape and all size bounds;
2. map the request type to the accepted uppercase enum;
3. require unrelated fields to remain null;
4. validate every evidence quote against the immutable source;
5. derive required missing fields from the request type;
6. resolve systems only through an exact active code, exact active name, or 1
   unique active alias;
7. require consequential identifiers to appear exactly in the source;
8. resolve an approver to exactly 1 active approver with the required system
   permission;
9. resolve a referenced case and recheck the requester's view permission;
10. preserve a distinct request with a matching content fingerprint as a
    possible duplicate that requires review; and
11. recheck that the requester remains active and authorized before routing.

An identifier is never repaired by prefix matching or best effort. For
example, `MGR-10` cannot resolve when the source says `MGR-104`. No match,
multiple matches, truncation, evidence mismatch, unrelated populated fields,
or permission mismatch produces `NEEDS_REVIEW` or `REJECTED` according to the
accepted deterministic rule.

`missing_fields` is derived only after the proposal shape and request type are
valid. Null, blank, or whitespace-only required values are missing. The model
cannot choose this list.

Each validation rule is stored with the existing exact fields:

- `rule_code`;
- `outcome`: `PASS`, `REVIEW`, or `REJECT`;
- `field_name`;
- `proposed_value`;
- `resolved_value`; and
- `reason`.

## Decision and State Mapping

| Condition | Validation result | Case result |
| --- | --- | --- |
| First retryable provider failure | No validation proposal yet | Remain `ANALYZING`; allow 1 retry |
| Second retryable provider failure | No valid proposal | Move to `FAILED` |
| Oversized input | `NEEDS_REVIEW` | Move to `NEEDS_REVIEW` without a model call |
| Invalid JSON, schema, bounds, or evidence | `NEEDS_REVIEW` | Move to `NEEDS_REVIEW` |
| Required values missing | `NEEDS_INFORMATION` | Move to `NEEDS_INFORMATION` |
| Possible duplicate or ambiguous reference | `NEEDS_REVIEW` | Move to `NEEDS_REVIEW` |
| Deterministic authorization rejection | `REJECTED` | Move to `REJECTED` |
| Complete access or data-change request | `READY` | Store checked details, create `PENDING` approval, move to `PENDING_APPROVAL` |
| Complete incident or authorized status request | `READY` | Store checked details and move to `READY_FOR_ACTION` |
| Complete policy question | `READY` for retrieval | Store checked details and remain `ANALYZING` until retrieval decides the route |

`READY` means the analysis proposal passed its own deterministic rules. It does
not mean a policy answer is grounded, an approval was granted, or a downstream
action is authorized.

## Attempts, Replay, and Recovery

- One analysis invocation performs at most 1 provider call.
- A case receives at most 2 provider attempts for the same input hash, prompt
  contract, and model identifier.
- Only transport failure, timeout, HTTP `429`, or HTTP `5xx` is retryable.
- Invalid JSON or a contract-invalid proposal is not retried automatically; it
  requires review.
- An exact replay after a completed commit returns the existing analysis and
  does not call the provider again.
- Concurrent invocations serialize on the case row. Only 1 may create the next
  attempt.
- A `PROCESSING` attempt older than 240 seconds is an abandoned attempt. It is
  finalized honestly as `FAILED` with an unknown provider outcome before any
  bounded retry.

The idempotency identity is the tuple of case, exact input hash, prompt contract
version, model identifier, and attempt number. A retry is a new durable attempt,
not an overwrite of previous evidence.

## Transaction Boundary

Provider waiting never occurs inside a database transaction.

1. In a short transaction, lock the case, validate state and version, return an
   existing completed replay when present, recover an expired attempt when
   needed, and insert 1 `PROCESSING` attempt.
2. Release the transaction and call the provider.
3. In a new short transaction, lock the case again, confirm the version and
   immutable input hash, finalize the attempt, append validation evidence when
   available, write only checked `case_details`, apply at most 1 allowed state
   transition, increment the case version when the state changes, and append 1
   case event.

Any failure in the final transaction rolls back the analysis finalization,
validation, accepted details, approval, state change, and event together. The
original request and all earlier attempts remain unchanged.

## Required Schema and State Alignment

The verified Stage 3 tables remain valid evidence for their original database
checkpoint, but the runtime implementation needs 1 additive migration:

- add positive `attempt_number` to `ai_analysis_runs`;
- add nullable `completed_at`;
- allow `PROCESSING` and `SKIPPED` in addition to `COMPLETED`,
  `INVALID_OUTPUT`, and `FAILED`;
- enforce status and completion-time consistency;
- enforce uniqueness across case, input hash, prompt contract, model
  identifier, and attempt number; and
- keep `proposal` as an object and `evidence` as an array, using a bounded error
  object and an empty evidence array when no valid proposal exists.

The accepted business-process table also needs the missing
`ANALYZING -> FAILED` transition for terminal analysis failure. Its existing
`FAILED -> ANALYZING` retry row otherwise has no reachable analysis-failure
state. The user accepted these alignments with this contract on 2026-08-17.
Migration `007_ai_analysis_runtime.sql` now prepares the additive database
alignment while preserving the verified Stage 3 migration unchanged.

## Security and Logging

- Secrets and authorization headers never enter prompts, analysis rows, audit
  payloads, or logs.
- Normal logs contain stable case references, attempt numbers, outcome codes,
  durations, and token counts, not original request text or full proposals.
- Invalid raw model output is represented by a bounded SHA-256 and error code;
  it is not copied wholesale into logs.
- The real provider is local Ollama only; no request leaves the laptop through
  a paid or hosted model API.
- The fixture provider is marked explicitly in durable evidence.

## Required Controlled Evidence

The later implementation must pass 1 concise deterministic integration check
using fictional cases and the fixture provider:

1. strict entry authentication, state, version, and workflow-reference guards;
2. all 5 request-type mappings and unrelated-null enforcement;
3. deterministic missing-field derivation;
4. exact evidence, enum, identifier, system, approver, permission, ownership,
   and possible-duplicate checks;
5. safe state mapping for review, information, rejection, approval, retrieval,
   and ready-for-action paths;
6. exact replay and 2 concurrent invocations without a duplicate provider call;
7. bounded transient retry, invalid-output handling, and abandoned-attempt
   recovery;
8. atomic rollback without partial accepted details, approval, state, or event;
9. append-only attempt and validation evidence with consistent aggregate
   counts; and
10. no policy retrieval, approval decision, downstream delivery, or requester
    notification before its own boundary is satisfied.

After that deterministic gate passes, 1 small real-provider smoke check uses 2
existing benchmark cases, 1 English and 1 Indonesian. It verifies only the
Ollama adapter, persistence, and bounded output. It does not rerun model
selection or create a new quality claim; the accepted 10-case benchmark remains
the model-suitability evidence.

## n8n Analysis-Continuation Gate

`docs/WORKFLOW_START_CONTRACT.md` defines the accepted post-acknowledgement
transport and response classification. Its later focused integration check
uses the deterministic fixture provider and 0 Ollama calls. It must prove:

1. the external workflow-start acknowledgement returns before delayed fixture
   analysis completes;
2. exactly 1 authenticated stable-reference command reaches this endpoint;
3. duplicate continuation is in progress or an exact no-call replay, never a
   duplicate provider execution;
4. retryable and malformed responses remain post-acknowledgement operational
   failures and do not trigger inline provider retry; and
5. primary analysis evidence remains isolated from policy retrieval, approval
   decisions, downstream delivery, and requester notification.

This gate does not repeat the 10-group domain integration or the 2-case Ollama
smoke. It verifies only the new n8n-to-primary continuation boundary.

## Truth Boundary

Contract acceptance authorizes only the bounded implementation and its focused
checks. The user-run additive persistence check passed on 2026-08-17, including
lifecycle constraints, bounded attempts, the `SKIPPED` outcome, and terminal
`ANALYZING -> FAILED` evidence. The user-run fixture integration also passed all
10/10 contract groups on 2026-08-17: 23 fictional cases produced 26 durable
attempts, 23 validation records, 10 accepted structured-detail rows, 2 pending
human approvals, 0 duplicate attempt identities, 0 unfinished attempts, and 0
external AI calls; cleanup passed. This verifies only the deterministic
application boundary with the fixture provider. `OllamaAnalysisProvider` and
its exact 2-case disposable smoke runner are prepared. The 1st user run reached
the real provider and durable validation, then exposed an overstrict smoke
assertion that conflated deterministic proposal acceptance with adapter output.
The runtime validator remains exact and unchanged; the corrected smoke now
checks structured bounded output, persistence, validation evidence, and no-call
replay while reporting the deterministic outcome separately. The corrected
user-run smoke passed 2/2 existing bilingual cases on 2026-08-17 with exactly 2
local model calls, 2 durable structured attempts, 2 no-call replays, 0 unfinished
attempts, and cleanup `PASS`. Both proposals were safely routed to
`NEEDS_REVIEW`, so this verifies the adapter boundary but creates no new
model-quality or automatic-routing claim. The later user-run n8n analysis
continuation passed all 5/5 fixture groups on 2026-08-19 with 2 durable analysis
attempts, 0 duplicate identities, 0 unfinished attempts, 0 external AI calls,
and cleanup `PASS`. This verifies the orchestration-to-analysis boundary, not
policy retrieval or end-to-end behavior.

The policy-retrieval application boundary is recorded in
`docs/POLICY_RETRIEVAL_CONTRACT.md`. It consumes only a `READY` policy analysis.
Its 6/6 application groups and corrected cleanup-only verification are
verified; this does not change the verified AI-analysis evidence above.

The verified human-decision application boundary is defined in
`docs/HUMAN_DECISION_CONTRACT.md`. It returns requester information and accepted
service-agent details to `ANALYZING`. Its user-run check passed 6/6 groups with
0 external AI calls. The separate human-resume handoff and route consumers are
also verified in `docs/HUMAN_RESUME_CONTRACT.md` and
`docs/HUMAN_RESUME_CONSUMERS_CONTRACT.md`.

The combined controlled lifecycle is verified separately. The locked quality
evaluation finished with `CHECK`, so this adapter evidence does not establish
unattended model quality, real-user performance, deployment, or production
reliability.
