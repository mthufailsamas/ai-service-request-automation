# Business Process Contract

**Status:** accepted v1 design; locked evaluation completed with CHECK

**Accepted:** 2026-08-17

## Purpose

The project automates the operational work between receiving a service request
and recording its outcome. AI helps understand natural-language requests and
retrieve useful context. Deterministic rules control completeness, duplicate
handling, permissions, routing, and risk. People authorize consequential
actions.

The 5 request types below are branches of 1 service-request lifecycle. They are
not separate applications.

## Actors

- **Requester:** submits a question, report, or action request and supplies
  missing information when asked.
- **Service agent:** checks incomplete, ambiguous, or exceptional requests.
- **Approver:** authorizes or rejects access and data changes.
- **Automation system:** coordinates intake, AI assistance, rules, records,
  notifications, retries, and audit history.
- **Downstream service:** stores the final ticket, response, or approved action
  in a local service-management sandbox.

## Supported Request Types

| Request type | What the system understands | Intended outcome |
| --- | --- | --- |
| Policy question | Topic, question, and requester context | Retrieve policy evidence and prepare a grounded answer |
| Incident report | Affected service, description, impact, and urgency | Create and route a service ticket |
| Access request | Requested system, access level, reason, requester, and approver | Wait for approval before recording the action |
| Data-change request | Target record, requested change, reason, requester, and approver | Wait for approval before recording the action |
| Status request | Existing case reference and requester identity | Return the authorized case status |

## Intake Contract

The v1 design supports 2 intake experiences over the same request contract:

1. a web form for non-technical users; and
2. a REST webhook for system-to-system submission.

Every incoming request contains:

- `source_channel`;
- `external_request_id` supplied by the intake source;
- requester identity supplied by the authenticated web session or an exact
  webhook requester reference, then resolved to internal `requester_id`;
- `subject`;
- `message` written in natural language;
- optional attachment names and metadata; and
- `received_at`.

The system creates:

- `case_id` as the internal reference;
- `idempotency_key` from the source and external request ID;
- `content_fingerprint` for possible duplicate detection;
- current workflow state;
- created and updated timestamps; and
- a version number for safe updates.

The accepted transport, authentication, exact replay, conflict, fingerprint,
atomic creation, and response rules are defined in `docs/INTAKE_CONTRACT.md`.

## AI Proposal Contract

AI receives the natural-language request and may propose:

- 1 of the 5 supported request types;
- a short operational summary;
- request-type-specific fields;
- evidence about the source text used for each extracted value.

Policy retrieval later uses the original request plus the checked policy
question. AI does not replace that source with a separate search query in v1.

AI output must follow a structured schema. Its confidence or explanation may
support review, but it never authorizes an action or chooses a final workflow
state by itself. Deterministic application rules derive missing required fields
and validate consequential identifiers against original text and reference
data.

## Required Fields by Request Type

The requester may write these values naturally in the message. AI proposes the
structured values and deterministic validation checks whether they are usable.

| Request type | Required structured fields |
| --- | --- |
| Policy question | `policy_topic`, `question` |
| Incident report | `affected_service`, `incident_description`, `impact`, `urgency` |
| Access request | `target_system`, `requested_access_level`, `business_reason`, `approver_id` |
| Data-change request | `target_system`, `record_reference`, `requested_changes`, `business_reason`, `approver_id` |
| Status request | `case_reference` |

All request types also require a valid `requester_id`, `subject`, and `message`.

## End-to-End Workflow

1. Receive the request from the web form or REST webhook.
2. Authenticate the requester and create a traceable case.
3. Reject an invalid identity or return an existing case for an exact
   idempotent replay.
4. Ask AI for a category, summary, extracted details, and source evidence.
5. Retrieve policy or procedure context when the request needs it.
6. Apply deterministic completeness, duplicate, permission, risk, and routing
   rules.
7. Ask the requester for missing information or send an ambiguous case to a
   service agent.
8. Ask an approver to authorize access or data changes.
9. Record the allowed outcome in the downstream service sandbox.
10. Notify the requester and preserve the audit history.

## Workflow States

- `RECEIVED`
- `ANALYZING`
- `NEEDS_INFORMATION`
- `NEEDS_REVIEW`
- `PENDING_APPROVAL`
- `READY_FOR_ACTION`
- `COMPLETED`
- `REJECTED`
- `FAILED`

## Allowed State Transitions

| Current state | Event or decision | Next state |
| --- | --- | --- |
| `RECEIVED` | Intake accepted | `ANALYZING` |
| `ANALYZING` | Required information is missing | `NEEDS_INFORMATION` |
| `ANALYZING` | Category or extracted values remain ambiguous | `NEEDS_REVIEW` |
| `ANALYZING` | Complete access or data-change request | `PENDING_APPROVAL` |
| `ANALYZING` | Complete safe request | `READY_FOR_ACTION` |
| `ANALYZING` | Requester is unauthorized or request is forbidden | `REJECTED` |
| `ANALYZING` | Both bounded analysis attempts fail | `FAILED` |
| `NEEDS_INFORMATION` | Requester supplies information | `ANALYZING` |
| `NEEDS_REVIEW` | Agent corrects or confirms the case | `ANALYZING` |
| `NEEDS_REVIEW` | Agent rejects the case | `REJECTED` |
| `PENDING_APPROVAL` | Approver authorizes the action | `READY_FOR_ACTION` |
| `PENDING_APPROVAL` | Approver rejects the action | `REJECTED` |
| `READY_FOR_ACTION` | Downstream action succeeds | `COMPLETED` |
| `READY_FOR_ACTION` | Downstream action fails | `FAILED` |
| `FAILED` | Retry is allowed after a transient action failure | `READY_FOR_ACTION` |
| `FAILED` | Retry is allowed after an analysis failure | `ANALYZING` |

Every transition records the previous state, next state, event, actor, time,
and supporting reason in the audit history.

## Deterministic Decision Rules

1. **Identity:** an inactive or unknown requester is rejected. A missing
   requester identity requires more information.
2. **Idempotency:** the same `source_channel` and `external_request_id` return
   the existing case and do not repeat downstream work.
3. **Possible duplicate:** a matching content fingerprint from a different
   external request ID is sent to review instead of being silently discarded.
4. **Completeness:** missing request-type fields lead to
   `NEEDS_INFORMATION`.
5. **Ambiguity:** unsupported categories, conflicting values, or unusable AI
   output lead to `NEEDS_REVIEW`.
6. **Policy answer:** an answer can proceed automatically only when it cites
   retrieved policy evidence that is available to the requester. Otherwise it
   needs review.
7. **Incident:** a complete incident creates a ticket automatically. Impact
   and urgency determine priority through a fixed rule table. Critical cases
   also alert a service agent but do not wait for approval to create the
   ticket.
8. **Access and data changes:** every complete request requires an approver.
   AI cannot approve it.
9. **Status:** the requester can see only a case they own or are explicitly
   allowed to view.
10. **Downstream delivery:** an idempotency key prevents a retry from creating
    the same downstream record twice.

## Human Decision Boundary

- The requester supplies missing information.
- The service agent corrects ambiguous extraction, classification, or policy
  context and handles possible duplicates or exceptions.
- The approver authorizes or rejects access and data changes.
- The automation may complete only explicitly safe outcomes: grounded policy
  responses, incident ticket creation, and authorized status responses.

## Controlled Evaluation Data

The project uses fictional data created for this repository:

- policy and procedure documents;
- requester, approver, service, and permission reference records;
- labeled natural-language requests for every supported type;
- expected extracted fields, routes, states, and downstream effects; and
- incomplete, ambiguous, duplicate, unauthorized, approval, integration
  failure, and retry cases.

The locked evaluation corpus contains 50 cases:

- 40 semantic cases: 8 for each supported request type, covering clear,
  paraphrased, informal, incomplete, ambiguous, duplicate, and authorization
  conditions; and
- 10 workflow-control cases covering exact idempotent replay, possible
  duplicates, approval outcomes, transient downstream failures, permitted
  retries, and permanent failures.

The evaluation data is used to test the completed system. It is not model
training data and contains no private company or personal information.

## Evaluation Measures

The completed evaluation calculates:

- request-type classification macro F1;
- required-field extraction accuracy;
- policy retrieval Recall@3 and citation validity;
- deterministic route and final-state accuracy;
- end-to-end task success;
- exact duplicate and idempotency protection;
- downstream delivery success;
- controlled failure-recovery success; and
- median and 95th-percentile processing latency.

## v1 Acceptance Targets

Targets are fixed before implementation so they cannot be changed merely to
match the observed result:

- 100% pass rate for deterministic permission, approval, idempotency, and
  duplicate-control tests;
- at least 90% classification macro F1;
- at least 90% required-field extraction accuracy;
- at least 90% policy retrieval Recall@3 with valid citations;
- at least 95% route and final-state accuracy;
- at least 90% end-to-end task success; and
- 100% recovery in the explicitly recoverable failure fixtures.

Only results produced by reproducible project runs can become verified or
public claims. The completed 50-case result, fixed targets, evidence hash, and
quality interpretation are recorded in `docs/EVALUATION_RESULTS.md`. Its fixed
quality gate is `CHECK`; the result is preserved rather than tuned after
observation.
