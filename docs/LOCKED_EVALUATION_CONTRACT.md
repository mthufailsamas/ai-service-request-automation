# Locked 50-Case Evaluation Contract

**Status:** completed controlled evaluation; fixed gate CHECK

**Contract version:** v1

## Purpose

This is the single controlled system evaluation used after implementation. It
measures quality and deterministic behavior without rerunning the accepted
component suites. Its fictional corpus is fixed before the first result is
observed and is not training data.

The locked corpus is
`evaluation/locked_system_evaluation_v1.json`, SHA-256
`17e6806295cba62a519353c7db4396eefbc0e2e07a999972520ef009b6477354`.
The runner refuses to start if those bytes change.

The first technical attempt used SHA-256
`8cbdc640b84b40ace84488c5187a6b45dd5d2cd5996ea718de16418940c4b4b6`.
It completed the 40 semantic executions and 8 grounded-retrieval executions but
stopped before workflow controls, metric calculation, or evidence persistence.
Six incident-control fixtures had identical content fingerprints, so the
application correctly isolated the second as a possible duplicate while the
test harness incorrectly attempted a safe downstream action. The correction
changes only those 6 deterministic control inputs. All 40 semantic cases and
all acceptance targets remain unchanged; no model score was available to
influence this correction.

The second technical attempt retained that corrected corpus hash, completed and
persisted all 40 semantic results and all 8 policy-case outcomes, then stopped
at deterministic control 3. The fictional Stage 2 seed had a legacy
message-only fingerprint even though the accepted intake contract calculates a
canonical requester-subject-message fingerprint. The application correctly
found no matching fingerprint; the control had incorrectly assumed the seed
already followed the later intake contract. The 2 seed fingerprints now follow
the accepted formula. This correction changes no locked evaluation case,
acceptance target, model output, or corpus hash.

The same affected-path audit also corrected the control assertion to require
`NEEDS_REVIEW`, which is the accepted deterministic route for a possible
duplicate. `PENDING_APPROVAL` remains the route for a clean access request and
would have been an incorrect expectation for this control.

## Evaluation Population

- 40 new semantic cases: 8 for each accepted request type, balanced across
  English and Indonesian where practical;
- incomplete, ambiguous, unauthorized, and safe automatic routes;
- 8 new policy questions that do not repeat embedding-selection queries; and
- 10 workflow-control cases for intake identity, possible duplicates, analysis
  replay, downstream replay, approval outcomes, bounded retry, permanent
  failure, and expired-claim recovery.

The 40 semantic subject-message pairs are distinct from the model-suitability
benchmark. The runner verifies both exclusions before any model call. Workflow
controls use deterministic fixture proposals because they test software
behavior rather than model quality.

## Fixed Acceptance Targets

- classification macro F1 at least 90%;
- required-field extraction accuracy at least 90%;
- policy retrieval Recall@3 at least 90%;
- citation validity 100%;
- deterministic route and final-state accuracy at least 95%;
- semantic task success at least 90%;
- workflow-control pass rate 100%; and
- explicitly recoverable failure success 100%.

Median and P95 controlled processing latency are measured but have no retrofitted
pass threshold. Policy documents and accepted policy queries are embedded in
batches to avoid repeated cold model loads. Document-indexing time is reported
separately; each policy case receives an equal share of its query-embedding
batch time. This is controlled local evaluation latency, not production or
real-user latency.

## Runtime and Evidence Boundary

The evaluation requires only the already installed
`qwen3:4b-instruct` and `qwen3-embedding:0.6b` models, disposable PostgreSQL and
Service Desk Sandbox services, and the local application image. It downloads no
model and permits no hosted or paid AI call. One stable evidence file is
initialized under `output/locked_evaluation/`, updated after semantic and
retrieval execution, then updated in place with the workflow controls and fixed
summary when the run completes. This preserves the expensive completed boundary
after a later technical failure without accumulating retry files. The launcher
refuses to overwrite a completed result. Disposable Docker state is removed,
and model-unload verification polls for at most 10 seconds to allow the local
runtime to finish releasing model processes.

When a valid `SEMANTIC_AND_RETRIEVAL` partial result exists, the harness checks
its corpus hash, model digests, fixed targets, all 40 ordered case identities,
per-result consistency, counters, and completion boundary. It then runs only
the 10 deterministic controls in fresh disposable databases and makes 0
additional model calls. The final evidence records 2 execution segments rather
than presenting the technical resume as 1 uninterrupted process. The accepted
partial evidence is additionally locked before resume as SHA-256
`a1cd541f9a1abff288fe7877319d8cc5da3a45b7b8921957da1afcaa90968b39`,
which is carried into the completed evidence before the stable file is updated.
Affected-only checks validate the canonical seed precondition, all normalized
control fingerprints before control 1 starts, deterministic-only resume branch,
and the preserved evidence shape without starting Docker or AI.

The first resume launch stopped before image preparation because Docker Desktop
temporarily locked its own Buildx context metadata file. No evaluation code,
database service, or model ran, cleanup passed, and the preserved evidence hash
remained unchanged. Image preparation now retries this exact metadata-lock
signature at most 3 times with 1-second and 2-second waits; unrelated build
failures remain immediate and visible.

The next resume reached deterministic control 10 after controls 1 through 9
completed, then exposed a final harness-only expiry simulation defect. It tried
to move `locked_at` 10 minutes before immutable `created_at`, correctly
triggering `outbox_messages_lock_after_created`. Moving `created_at` would also
violate the immutable delivery-intent trigger. The control now keeps the real
claimed row unchanged, waits a bounded 1.25 seconds, and invokes the accepted
recovery service with its minimum 1-second lease. This follows the actual lease
contract, changes no application or corpus behavior, makes no model call, and
preserves every outbox constraint.

## Completed Result

The corrected user-run evaluation completed on 2026-08-20. Its stable evidence
file is `output/locked_evaluation/locked-system-evaluation-v1.json`, SHA-256
`34cad9e3150cf43b9d043888a717a1527e202b0058f53c08a3c2fe859adc5afe`.
The evidence contains all 40 ordered semantic results and all 10 passing
workflow-control results, preserves the accepted corpus and model identifiers,
records its 2 transparent execution segments, and reports 0 additional model
calls during resume.

- classification macro F1: 94.6% (`PASS` against 90%);
- required-field accuracy: 88.3% (`CHECK` against 90%);
- policy retrieval Recall@3: 0.0% (`CHECK` against 90%);
- citation validity: 0.0% (`CHECK` against 100%);
- route and final-state accuracy: 30.0% (`CHECK` against 95%);
- semantic task success: 20.0% (`CHECK` against 90%);
- workflow controls: 100.0% (`PASS`); and
- explicitly recoverable failures: 100.0% (`PASS`).

All 8 policy cases reached `NEEDS_REVIEW` during deterministic proposal
validation before retrieval, so 0.0% here measures the locked end-to-end route;
it does not replace or invalidate the earlier focused embedding suitability
result. Median controlled processing latency was 10.268 seconds, P95 was 14.984
seconds, cold policy indexing was 3.476 seconds, and the semantic segment made
42 local Ollama calls. Cleanup passed. These are synthetic local measurements,
not production or real-user evidence.

`CHECK` is the accepted honest result. The corpus, completed evidence, prompts,
or thresholds must not be changed or rerun to improve this score.

The stable root launcher was `check.cmd locked-evaluation`. The completed result
must not be rerun. Future improvements require a new development corpus and a
new untouched evaluation contract rather than tuning against these observed
cases.
