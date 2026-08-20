# Human-Resume Consumers Contract

**Status:** verified by the controlled 6-group local runtime check

**Contract version:** v1

## Purpose

This combined checkpoint completes the 2 consumers that remained after the
verified human-resume handoff: requester-information re-analysis and terminal
rejection notification. It also routes service-agent confirmation or correction
deterministically from already accepted details. It does not repeat the verified
n8n handoff or approved downstream-action check.

## Durable Authority

Every effect starts from 1 append-only
`HUMAN_DECISION_RESUME_ACKNOWLEDGED` event. The consumer rechecks its exact human
decision, route, case, state, version, action, and 64-character outbox identity.
No volatile post-response branch is the only trigger.

Requester information is combined with the immutable original message without
overwriting it. The resulting exact input receives a new SHA-256 analysis
identity, retains the 8,000-character guard and 2-attempt limit, and records the
`HDRESUME-*` trigger in the terminal analysis event. Concurrent invocation and
exact replay may call the fixture provider only once.

Confirmed or corrected service-agent details do not call a model again. Current
role, system, requester, and approver permissions are rechecked before routing
to approval, policy retrieval, or downstream action. One
`HUMAN_REVIEW_REANALYZED` event records the exact next route.

## Terminal Notification

Only `SERVICE_AGENT_REJECTED` and `APPROVAL_REJECTED` acknowledgements may create
the exact `REQUESTER_NOTIFICATION`. Its payload contains the fictional requester
reference, case reference and version, subject, rejection reason, and both human
references. The immutable intent uses the existing bounded outbox and append-only
delivery-attempt tables.

The v1 adapter is an explicit zero-cost local requester inbox, not email, SMS,
or a hosted provider. Successful or terminal failed delivery produces one
same-state audit event without changing the already rejected case.

## Evidence Gate

Exactly 1 concise disposable runner covers 6 groups:

1. acknowledgement, decision, route, state, and version guards;
2. requester information and one bounded fixture re-analysis;
3. concurrent invocation and exact one-call replay;
4. deterministic service-agent confirmation and correction routes;
5. both terminal rejection notification paths and exact replay; and
6. aggregate evidence, unfinished-work checks, and boundary isolation.

The runner uses 5 fictional cases, disposable PostgreSQL, the current application
image, 1 deterministic fixture call, 0 Ollama calls, and no hosted or paid
service. The user-run check passed all 6/6 groups on 2026-08-19 with 5 durable
resume acknowledgements, 2 deterministic agent routes, 2 delivered terminal
notifications, 0 duplicate effects, and 0 unfinished work. Cleanup also passed.

## Evidence Boundary

Interactive local login and case views, scheduled failure recovery, full
end-to-end integration, and the locked 50-case evaluation were verified through
their separate contracts. Deployment, real users, and production behavior
remain outside the local v1 evidence.
