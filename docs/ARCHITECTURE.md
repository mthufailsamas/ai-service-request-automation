# Implementation Architecture

**Status:** implemented local v1; controlled integration verified; locked quality gate CHECK

## Design Goal

Use the smallest zero-cost architecture that can execute the accepted service
request process and demonstrate transferable AI Automation responsibilities.
Every component has 1 clear purpose. The design avoids duplicate platforms and
paid services.

## Component Responsibilities

| Component | Responsibility | Why it exists |
| --- | --- | --- |
| Web interface | Submit requests, view cases, supply missing information, review cases, approve actions, and view operational results | Gives requesters, service agents, and approvers a non-technical interface |
| FastAPI application | Validate API contracts, apply business rules, manage roles and permissions, persist state, call local AI and retrieval, and expose review actions | Keeps consequential rules in readable, testable Python |
| n8n | Coordinate event-driven steps, branches, human pauses, notifications, downstream calls, and controlled retries | Demonstrates real workflow orchestration instead of hiding the process inside 1 Python function |
| PostgreSQL with pgvector | Store cases, states, audit events, approvals, idempotency records, delivery attempts, reference data, policy chunks, and embeddings | Provides 1 durable source of truth for workflow and retrieval data |
| Ollama | Run the language and embedding models locally | Provides zero-cost AI inference without sending project data to a paid API |
| Service Desk Sandbox | Behave like an external ticket and action API, including controlled success, duplicate, transient failure, and permanent failure responses | Makes API integration, idempotency, retry, and recovery behavior reproducible |
| Evaluation runner | Execute the labeled evaluation corpus and calculate the accepted measures | Separates repeatable evidence from manual demonstrations |

## User Interface Approach

The web interface uses server-rendered HTML and small inline CSS served by
FastAPI. A large frontend framework is not required for the accepted workflow.

The interface provides role-appropriate views:

- requester intake and case status;
- service-agent review queue;
- approver decision queue; and
- operations dashboard for volume, routes, failures, retries, and latency.

Technical identifiers and error details remain available in a dedicated
support section instead of dominating the normal user experience.

The verified local portal in `docs/LOCAL_PORTAL_CONTRACT.md` verifies passwords
against the existing PostgreSQL hash, issues the existing signed session, and
uses CSRF-protected login, logout, and human-action forms. One role-filtered
query boundary protects both lists and details. Its 6-group controlled runtime
passed on 2026-08-19. The separate recovery-and-operations gate later verified
the administrator operations dashboard.

## Python Boundary

FastAPI owns behavior that must be deterministic and testable:

- request and response schemas;
- authentication and role checks;
- case creation and state transitions;
- required-field validation;
- duplicate and idempotency rules;
- risk, routing, and approval rules;
- policy-access checks;
- downstream delivery records;
- audit history; and
- workflow metrics.

AI output is validated through Pydantic schemas before any business rule uses
it. Invalid structured output becomes a reviewable failure; it never becomes a
fabricated default request.

## n8n Boundary

The project has 1 business process with 3 workflow entry points:

1. **Request intake:** starts from a committed `WORKFLOW_START` outbox message
   after the web or webhook adapter has atomically stored the case and its 1st
   event, then coordinates analysis, retrieval, rule evaluation, routing,
   delivery, and notification.
2. **Human decision resume:** starts when a requester, service agent, or
   approver supplies the information or decision needed to continue a case.
3. **Failure recovery:** periodically repairs only expired accepted outbox claims,
   appends an honest unknown-outcome attempt, and returns eligible work to its
   existing bounded worker within the configured retry limit.

These are not separate portfolio projects. They exist because intake, human
decisions, and scheduled recovery happen at different times and through
different events.

n8n does not duplicate the Python business rules. It calls explicit FastAPI
operations and branches on their returned decisions.

## Primary Intake Transaction Boundary

The web form and REST webhook authenticate through different adapters and map
to 1 deterministic case-creation service. A new request commits exactly 1 case,
its `CASE_RECEIVED` event, and a distinct `WORKFLOW_START` outbox message in 1
transaction. The HTTP request does not wait for n8n or call it directly.

The workflow-start payload carries only stable case references. n8n later
retrieves authoritative request data through FastAPI. Requester receipts use a
separate `REQUESTER_NOTIFICATION` message so orchestration and communication
have independent destinations, retry histories, and outcomes. The accepted
transport, authentication, replay, fingerprint, response, and rollback rules
are in `docs/INTAKE_CONTRACT.md`.

The accepted durable handoff is defined in
`docs/WORKFLOW_START_CONTRACT.md`. A primary dispatcher calls an authenticated
local n8n webhook, and n8n calls an authenticated FastAPI analysis-start
endpoint. FastAPI, not n8n, atomically owns the idempotent `RECEIVED` to
`ANALYZING` transition and its `ANALYSIS_STARTED` event. n8n never connects
directly to the primary database.

The controlled runner gives n8n its own disposable SQLite volume.
Only its encrypted workflow and credential state use that volume; primary
business state stays in PostgreSQL. The runner exposes no n8n port to the host
and removes the n8n volume during cleanup. Its user-run integration passed all
8/8 groups and cleanup on 2026-08-17.

The next authenticated internal operation is
`POST /internal/v1/cases/{case_id}/analysis`. It accepts only the case
reference, expected version, and verified workflow-start reference. The
primary application loads the immutable subject and message, claims 1 bounded
attempt in PostgreSQL, releases the transaction before provider work, and
atomically finalizes the proposal, deterministic validation, accepted details,
approval intent when required, state, and audit event.

`FixtureAnalysisProvider` supplies only explicitly configured fictional
results for the controlled integration check. It performs no network call.
The user-run 10-group fixture integration passed on 2026-08-17. It covered the
exact proposal schema, all 5 request types, evidence and authorization rules,
state routing, replay, concurrency, retry, abandoned-attempt recovery, rollback,
aggregates, and deferred-boundary isolation. Its 23 fictional cases produced 26
durable attempts with 0 duplicate identities, 0 unfinished attempts, and 0
external AI calls. This verifies the deterministic fixture boundary only; the
Ollama adapter then passed its corrected 2-case user-run smoke with exactly 2
local model calls, 2 durable structured attempts, 2 no-call replays, and 0
unfinished attempts. Both proposals were safely routed to `NEEDS_REVIEW`; this
is adapter evidence, not a new quality or automatic-routing result. The runtime
validator was not weakened.

The accepted n8n analysis continuation begins from the normal output of the
verified `Return Workflow Response` node, after the dispatcher has received its
stable acknowledgement. It carries only the case ID, case reference, stable
expected version `2`, and workflow-start reference to the authenticated primary
analysis endpoint. The primary application loads the immutable request text;
n8n never receives it or writes PostgreSQL directly.

One n8n execution makes at most 1 native-JSON analysis call with no inline
retry. A 200-second n8n timeout sits outside the primary provider's 180-second
limit. FastAPI and PostgreSQL own the 2-attempt maximum, exact replay,
deterministic validation, and state transition. n8n records only a bounded
operational classification and never changes a review or rejection decision.

This post-response design keeps model latency and failure outside the verified
workflow-start acknowledgement. It does not yet guarantee automatic recovery
if n8n stops between acknowledgement and analysis delivery; the durable
`ANALYZING` state makes that gap detectable for the later failure-recovery
workflow. The user-run 5-group fixture-only continuation integration passed on
2026-08-19 with 2 durable analysis attempts, 0 duplicate identities, 0
unfinished attempts, 0 external AI calls, and cleanup `PASS`. This verifies the
post-response orchestration boundary only.

## Primary Delivery Transaction Boundary

The verified primary delivery slice processes at most 1 ready downstream
outbox message per worker invocation:

1. claim 1 eligible message with `FOR UPDATE SKIP LOCKED`, increment its attempt
   count, and commit the claim;
2. call the authenticated Service Desk Sandbox API without holding a database
   transaction open; and
3. append the delivery attempt and update the outbox state together in 1 new
   transaction.

HTTP `200` and `201` are successful only when their response body matches the
sandbox contract. HTTP `503` is retryable only when the body explicitly marks
it retryable. Transport failures are retryable within the outbox attempt limit.
Every other HTTP result or invalid success body is a permanent failure.

This boundary keeps network waiting outside database transactions while
preserving 1 atomic local result for every completed call. Its controlled local
integration check passed 6/6 groups on 2026-08-17. n8n did not participate in
that check.

## Local AI Runtime

### Selected language model

- Runtime: Ollama on Windows.
- Selected v1 model: `qwen3:4b-instruct`.
- Intended use: request classification, concise summary, structured field
  extraction, and source-evidence proposals.
- Output mode: JSON schema with temperature `0`.
- Initial context limit: 4,096 tokens to keep memory use controlled.

The Ollama library lists the quantized model at approximately 2.5 GB with an
Apache 2.0 license. Benchmark contract v2 passed its controlled 10-request
quality gate with 100.0% schema validity, 100.0% request-type classification,
93.8% field matching, 100.0% deterministic missing-field handling, 96.2%
evidence validity, 83.9% evidence coverage, 0 fabricated defaults, and
11.53-second median warm latency. This selects the model for proposal work; it
does not make model output authoritative.

### Embedding model selection

- Selected v1 model: `qwen3-embedding:0.6b`.
- Intended use: policy indexing and semantic retrieval.
- Retrieval store: pgvector in the project PostgreSQL database.

The Ollama library currently lists this model at approximately 639 MB. The same
embedding model must be used for both indexing and querying.

The fixed local suitability benchmark passed on 10/10 bilingual queries with
valid 1,024-dimension vectors, 100.0% Recall@3, 100.0% top-1 accuracy, a
0.220-second median warm-query latency, and a 0.259-second nearest-rank p95
warm-query latency. This selects the model for the v1 retrieval implementation;
it does not verify pgvector behavior, access filtering, answer grounding, or
real-world policy retrieval.

### Provider behavior

The application exposes 1 small AI-provider interface with 2 implementations:

- `OllamaAnalysisProvider` for the accepted local runtime; and
- a deterministic fixture provider used only by automated tests.

The Ollama implementation permits only the local HTTP endpoint on port `11434`,
disables proxy routing, requires the accepted model name and digest, serializes
model calls, and applies the fixed prompt, JSON schema, timeout, context, output,
temperature, thinking, and warm-retention contract. Its runtime behavior remains
verified by the corrected 2-case local smoke. The smoke reports deterministic
outcomes separately because an adapter can return a schema-valid bounded
proposal that the application safely routes to review.

The fixture provider never appears as a real AI result in the user interface or
evaluation. It exists so rules, routing, retries, and integrations can be tested
independently of model variability.

If Ollama is unavailable or returns invalid output, the case moves to a clear
failure or review state. The application must not invent fallback values.

Ollama proposes the request type, summary, extracted values, and source
evidence. Application code derives missing required fields from the accepted
request contract. Completeness is therefore reproducible and is not delegated
to probabilistic model output.

Identifiers that affect routing or downstream records must match the original
request or accepted reference data exactly. An identifier that is truncated,
altered, absent, duplicated, or unsupported by its evidence cannot proceed
automatically. The application preserves the original text and routes that case
to review instead of trusting the proposal.

## Retrieval Approach

1. Split the fictional policy documents into traceable chunks.
2. Generate local embeddings once during controlled indexing.
3. Store chunk text, policy identity, access level, and vector in PostgreSQL.
4. Embed the request query with the same model.
5. Retrieve the top 3 allowed chunks by cosine similarity.
6. Ask the language model to answer only from those chunks and cite their IDs.
7. Validate that every cited ID was retrieved and visible to the requester.

A policy answer without valid allowed evidence goes to service-agent review.

The prepared v1 application boundary is specified in
`docs/POLICY_RETRIEVAL_CONTRACT.md`. It uses an append-only
`POLICY_RETRIEVAL_STARTED` event as the durable claim, performs local provider
work outside the transaction, and stores bounded terminal evidence in the case
event rather than adding a retrieval table. Its 6/6 application groups and
corrected cleanup-only verification passed the controlled user runs. It does
not yet connect the verified n8n continuation to retrieval.

## Human-Decision Application Boundary

The prepared boundary in `docs/HUMAN_DECISION_CONTRACT.md` reuses signed user
sessions, `users`, `user_roles`, `system_permissions`, `cases`, `case_details`,
`case_events`, and `approvals`. Requester information, service-agent review,
and assigned-approver decisions use 1 strict HTTP command and 1 case-row lock.
Every accepted effect increments the case version and appends exactly 1
user-attributed event in the same transaction; exact command replay returns the
original acknowledgement.

Requester and service-agent resume actions follow the accepted transition back
to `ANALYZING`. The human-decision application checkpoint adds no table,
migration, AI call, downstream action, notification, or interactive login. Its
controlled runtime passed 6/6 focused groups with 0 duplicate command effects,
0 external AI calls, and cleanup `PASS` on 2026-08-19.

The separately verified orchestration boundary in
`docs/HUMAN_RESUME_CONTRACT.md` derives 1 immutable
`HUMAN_DECISION_RESUME` outbox intent from each committed user event. Local n8n
maps the 6 actions into `ANALYSIS_CONTINUATION`, `DOWNSTREAM_ACTION`, or
`TERMINAL_NOTIFICATION`, then receives an authenticated same-state primary
acknowledgement. This proves only handoff and route classification. The corrected
user run passed all 6/6 groups with 0 duplicate acknowledgements, 0 unfinished
intents, 0 external AI calls, and cleanup `PASS`. Downstream action and the
other 2 consumer families were verified through their separate focused gates.

## Approved Downstream-Action Boundary

The verified boundary in `docs/APPROVED_ACTION_CONTRACT.md` consumes only the
durable human-resume acknowledgement for `APPROVE_REQUEST`. A PostgreSQL-backed
materializer rechecks current requester and approver authority, approval and
case state, accepted details, and target system before creating 1 immutable
`DOWNSTREAM_ACTION` payload. This avoids making a volatile n8n post-response
branch the only trigger for a consequential action.

The existing delivery worker and Service Desk Sandbox perform the external
handoff. A terminal reconciler moves `READY_FOR_ACTION` to `COMPLETED` only
from a successful append-only attempt or to `FAILED` only from compatible
terminal failure evidence. The boundary adds no primary table, provider, or AI
call. Its controlled runtime passed all 6/6 groups with 0 duplicate terminal
transitions, 0 unfinished actions, 0 external AI calls, and cleanup `PASS`.

## Remaining Human-Resume Consumers

The combined boundary in `docs/HUMAN_RESUME_CONSUMERS_CONTRACT.md` consumes only
the verified append-only acknowledgement. Requester information is added to the
model input without mutating the original request, receives a distinct analysis
identity, and retains the existing input, attempt, validation, and replay
guards. Service-agent accepted details are routed deterministically after current
role and system permissions are rechecked, so they are not sent through a model
again.

Terminal agent or approver rejection creates 1 immutable
`REQUESTER_NOTIFICATION` for a zero-cost local requester inbox. It reuses the
bounded outbox and append-only delivery evidence and records a same-state sent or
failed audit event. The user-run combined check passed all 6/6 groups with 0
duplicate effects, 0 unfinished work, 0 hosted or paid AI calls, and cleanup
`PASS`.

## Deterministic Safe-Action Boundary

Complete incidents, grounded visible policy answers, and requester-owned status
responses now use `application/safe_action.py` to materialize the three matching
Service Desk Sandbox contracts. The materializer binds the immutable outbox key
to the exact analysis, retrieval, or service-agent routing event and rechecks
current deterministic authority. Its reconciler rebuilds the payload from
durable evidence before any terminal case transition. The combined end-to-end
runtime passed all 7/7 controlled lifecycle groups on 2026-08-20.

## Data Contract

The primary database contains focused tables for:

- users and their roles;
- services and permission reference data;
- cases and their structured request data;
- case state-transition and audit events;
- approvals;
- policy documents and chunks;
- downstream delivery attempts; and
- requester notifications.

The Service Desk Sandbox stores its own downstream records so the integration
boundary can be tested independently.

Exact columns, constraints, authority boundaries, identifier guards, and staged
implementation order are defined in the accepted `docs/DATA_CONTRACT.md`.
Stages 1 through 5 and the isolated Service Desk Sandbox schema have passed
their user-run database checks. Those checks predate the accepted
`WORKFLOW_START` outbox type. The later user-run primary intake integration
check verified migration 006 and the controlled web and webhook intake boundary
in 10/10 focused groups on 2026-08-17. The later controlled workflow-start
integration passed 8/8 groups, including concurrent claiming, idempotent
lost-response recovery, bounded retry and rejection, expired-claim recovery,
and isolation from the Service Desk worker.

## Local Packaging

Docker Compose will eventually start:

- FastAPI application;
- n8n;
- PostgreSQL with pgvector; and
- Service Desk Sandbox.

Ollama will initially run natively on Windows so the project can use the NVIDIA
GPU without adding GPU-container setup to the beginner path. Containers will
reach it through the host interface. The project will provide 1 start command,
1 stop command, health checks, and clear terminal output after the core services
exist.

## Reliability and Evidence

The implementation will add these capabilities only when their stage is
reached:

- database-enforced idempotency and allowed state transitions;
- bounded retry with transient and permanent failure separation;
- delivery outbox so committed cases are not lost between services;
- structured application and workflow logs;
- operational metrics derived from audit events;
- unit, integration, workflow-contract, and browser tests;
- controlled AI and retrieval evaluation; and
- Docker and continuous-integration verification.

A Playwright browser adapter may be added after the API delivery path is
verified, but only as 1 real fallback for the same downstream process. It is not
part of the initial implementation checkpoint.

## Verified Device Snapshot

Read-only inspection on 2026-08-17 found:

- AMD Ryzen 5 4600H with 6 cores and 12 logical processors;
- 31.4 GB system RAM;
- NVIDIA GeForce GTX 1650 with 4,096 MiB VRAM and driver 610.74;
- 282.8 GB free space on drive C;
- WSL 2.7.11;
- Docker 29.6.2 and Docker Compose 5.3.1 installed;
- Docker Engine not reachable during inspection; and
- Ollama 0.32.14 installed and its local API reachable;
- `qwen3:4b-instruct` downloaded and evaluated locally; and
- `qwen3-embedding:0.6b` downloaded and evaluated locally.

The 4B quantized model passed the controlled v2 suitability gate with 67% GPU
placement and 11.53-second median warm latency. A 7B or larger model is not
selected for v1 because the accepted proposal model already passes its gate and
the added memory and latency risk is unnecessary.

## Device Optimization Policy

The implementation will target the verified 32 GB installed-memory class,
31.4 GB available system memory, and 4,096 MiB NVIDIA GPU. Model selection will
use a quality gate followed by a conditional fallback:

1. benchmark `qwen3:4b-instruct` on the small fixed request set;
2. reject a configuration that produces invalid schemas, fabricated defaults,
   unsafe routes, or unacceptable extraction quality;
3. accept the 4B configuration and stop testing when it passes the quality gate
   with usable warm latency and stable memory placement; and
4. test `qwen3:1.7b` only when the 4B configuration is too slow or unstable.
   The smaller model is a performance fallback, not an expected quality
   improvement.

The initial runtime configuration will:

- limit context to 4,096 tokens unless evaluation proves that more context is
  required;
- combine classification, summary, and field extraction into 1 structured
  model call per request where possible;
- keep model-call concurrency at 1 so parallel requests cannot exhaust VRAM;
- queue additional AI work while non-AI API operations remain responsive;
- precompute policy-document embeddings and batch only the indexing work;
- run query embedding and response generation sequentially instead of keeping
  multiple models under GPU load at the same time;
- keep the selected language model warm during an active demonstration and
  release it after an idle period;
- cap generated output to the fields and explanation required by the schema;
  and
- record cold-start latency, warm latency, RAM use, VRAM use, and GPU offload
  during the suitability benchmark.

The project will not increase model size merely to maximize hardware use. Spare
RAM and GPU capacity are reserved for PostgreSQL, n8n, FastAPI, the downstream
sandbox, the browser, Docker overhead, and operating-system stability.

## Zero-Cost Boundary

No cloud deployment, paid API, subscription, credit purchase, or billing setup
is part of this architecture. Installation and model downloads require internet
access and local disk space but do not authorize a chargeable service.

## Source References

- [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
- [Ollama embeddings](https://docs.ollama.com/capabilities/embeddings)
- [Ollama hardware support](https://docs.ollama.com/gpu)
- [Qwen3 4B Instruct in the Ollama library](https://ollama.com/library/qwen3%3A4b-instruct)
- [Qwen3 Embedding in the Ollama library](https://ollama.com/library/qwen3-embedding)
