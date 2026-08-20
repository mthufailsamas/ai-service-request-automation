# Portfolio Website Handoff

This copy is ready for the separate Portfolio Website repository. It preserves
the verified local evidence and the fixed `CHECK` quality result.

## Role Family and Core Stack

**AI Automation Engineering · FastAPI · n8n · PostgreSQL/pgvector · Ollama · Docker**

## Project Title

**AI Service Request Automation**

## Card Summary

A local, auditable service-request workflow that combines AI-assisted request
understanding with deterministic rules, human review, approval, downstream API
delivery, and scheduled recovery. Controlled evaluation verified the workflow
controls and exposed the remaining local-model quality gap before unattended
use.

## Problem

Service teams repeatedly interpret incoming requests, verify required details,
consult policies, decide routes, create downstream records, and communicate
outcomes. Manual handling makes consistent decisions, duplicate protection,
failure recovery, and auditability difficult to maintain.

## My Contribution

Designed and implemented 1 end-to-end automation for 5 request types across
REST and web intake, AI-assisted analysis, policy retrieval, deterministic
validation, role-based human decisions, durable workflow orchestration,
downstream delivery, notification, and operational recovery. Built strict
idempotency, permission, state-transition, retry, and evidence boundaries around
probabilistic model output.

## Technical Details

- FastAPI owns authentication, permissions, state transitions, validation,
  approvals, idempotency, delivery evidence, and the local portal.
- n8n coordinates request start, human-decision resume, and scheduled recovery
  through authenticated, bounded workflow entries.
- PostgreSQL with pgvector stores authoritative case, audit, approval, outbox,
  policy, retrieval, and attempt evidence.
- Ollama runs `qwen3:4b-instruct` and `qwen3-embedding:0.6b` locally under the
  IDR 0 cost boundary.
- A separate Service Desk Sandbox reproduces successful delivery, replay,
  transient failure, permanent failure, and recovery behavior.

## Headline Evidence

- **50/50** locked evaluation cases completed
- **100.0%** workflow-control pass rate
- **100.0%** recoverable-failure pass rate
- **94.6%** request-type classification macro F1

## Project Output

Delivered a reproducible local application with role-based case handling,
durable orchestration, human approval and exception paths, downstream API
integration, scheduled recovery, controlled test runners, and checksum-locked
evaluation evidence. The combined lifecycle passed 7/7 integration groups with
0 unfinished workflow work and 0 duplicate terminal effects.

## Evidence & Scope

The locked 50-case evaluation used fictional bilingual requests and fixed
pre-observation targets. Classification reached 94.6% macro F1, while
required-field accuracy reached 88.3%, route accuracy 30.0%, and semantic task
success 20.0%. The fixed quality gate is `CHECK`: deterministic workflow and
recovery safeguards passed, while the selected local analysis pipeline still
requires human review for reliable use. Evidence is controlled local testing,
not production, real-user, or business-impact validation.

## Recommended Links

- Repository README: `README.md`
- Evaluation summary: `docs/EVALUATION_RESULTS.md`
- Architecture: `docs/ARCHITECTURE.md`
- Full lifecycle contract: `docs/END_TO_END_CONTRACT.md`

## Visual Brief for the Website Checkpoint

Create 1 native 1920x1080 visual with a centered **AI Service Request
Automation** title. Show a left-to-right path from intake to FastAPI and n8n,
then AI plus deterministic validation, human review or approval, Service Desk,
and durable evidence. Use only these visible evidence labels: **50 cases**,
**100% workflow controls**, **100% recoverable failures**, and **94.6% macro
F1**. Include a compact **Quality gate: CHECK** badge so the visual cannot imply
that every semantic target passed.
