# Policy-Retrieval Application Contract

**Status:** controlled local runtime verified; 6/6 groups and cleanup passed

**Contract version:** v1

**Prepared:** 2026-08-19

## Purpose and Scope

This checkpoint turns 1 deterministically accepted `POLICY_QUESTION` into a
grounded policy answer or a safe review outcome. It covers the primary FastAPI
and PostgreSQL boundary only. n8n routing, downstream delivery, requester
notification, human-decision resume, and failure recovery remain deferred.

The checkpoint reuses `policy_documents`, `policy_chunks`, `case_details`, and
append-only `case_events`. It adds no table, migration, or workflow.

## Stable Internal Command

`POST /internal/v1/cases/{case_id}/policy-retrieval` uses the existing primary
Bearer credential and accepts exactly:

```json
{
  "schema_version": "1",
  "case_reference": "CASE-2026-0001",
  "expected_case_version": 2,
  "analysis_run_id": "00000000-0000-4000-8000-000000000001"
}
```

The application loads the immutable subject and message, checked
`policy_question`, requester roles, and accepted analysis evidence. Callers do
not supply query text, visibility, candidate chunks, answer, or citations.

## Deterministic Boundary

1. Require an active requester, `POLICY_QUESTION`, `ANALYZING`, the expected
   version, accepted case details, and a `READY` validation for the supplied
   analysis run.
2. Build 1 bounded query from the original subject, original message, and
   checked policy question. Apply the exact accepted English retrieval
   instruction to the query embedding while leaving policy chunks unmodified.
3. Map requester roles to allowed policy visibility: every active requester may
   see `ALL_EMPLOYEES`; service agents, approvers, and admins additionally see
   only their matching levels; admins may see all 4 levels.
4. Embed the query with the same exact model identifier stored on candidate
   chunks. Reject non-finite, zero, or non-1,024-dimension vectors.
5. Search only active and currently valid allowed documents with exact cosine
   distance and return at most 3 chunks. No approximate index is added.
6. Give the answer model only the query and retrieved chunk IDs and text.
7. Accept only a bounded nonblank answer with 1 or more unique citation IDs,
   all of which were retrieved and visible. The model cannot introduce a policy
   ID or authorize access.
8. A valid grounded answer moves the case to `READY_FOR_ACTION`. Missing allowed
   context, invalid output, invalid citations, or provider failure moves it to
   `NEEDS_REVIEW`. Every outcome increments the case version and appends 1
   terminal event containing bounded evidence.

## Idempotency and Concurrency

The application locks the case only while claiming and finalizing; model calls
occur outside the transaction. A `POLICY_RETRIEVAL_STARTED` event is the durable
claim. A matching terminal event is an exact no-call replay. An unfinalized
matching claim returns `409 POLICY_RETRIEVAL_IN_PROGRESS`; it does not start a
2nd provider call. Abandoned-claim recovery remains the later recovery
workflow's responsibility.

## Evidence Gate

Exactly 1 concise disposable runner must cover authentication and state guards,
visibility filtering, exact top-3 search, grounded citations, safe review,
concurrent/no-call replay, aggregate evidence, and deferred-boundary isolation.
It may include the smallest installed-model smoke, but downloads no model and
calls no hosted or paid service. The complete controlled local gate passed on
2026-08-19.

The 1st user run passed all 6/6 application groups with 6 durable claims, 6
terminal outcomes, 1 exact replay, 0 duplicate provider executions, 3 local
Ollama calls, and 0 hosted or paid calls. Docker cleanup passed. A later
PowerShell native-stderr error occurred while stopping an embedding model that
was already unloaded, so application evidence is valid while final model-state
cleanup was not yet verified by that first run. The corrected cleanup-only run
then verified absent disposable Docker state and both accepted models unloaded.
It passed without repeating an application group or making a model call, which
closed the full policy-retrieval gate.

Passing the complete gate proves controlled fictional local retrieval only. It does not
prove real-company policy quality, production access control, end-to-end n8n
routing, or real-user outcomes.
