# Guided Local Demo

This demo teaches the accepted service-request boundaries through disposable
fictional data. It binds the portal only to `127.0.0.1`, generates temporary
runtime secrets, downloads no model, and removes its database state during
cleanup.

## 1. Start the portal

Start Docker Desktop, open PowerShell in the project root, and run:

```powershell
.\demo.cmd start
```

Open `http://127.0.0.1:8000/login`. Every fictional account uses this local
demo password:

```text
Demo-Local-Only-2026!
```

| Role | Employee reference | Learning task |
| --- | --- | --- |
| Requester | `EMP-201` | Supply missing information and create a new request |
| Service agent | `AGT-301` | Inspect and confirm or correct an AI proposal |
| Approver | `MGR-104` | Approve or reject the assigned data change |
| Administrator | `ADM-001` | Inspect aggregate operational evidence |

## 2. Requester lesson

1. Log in as `EMP-201`.
2. Open **Demo: missing incident information**.
3. Read the state, AI summary, and audit history.
4. Supply `Urgency is high for the warehouse team.`
5. Submit the form and observe the new state and audit event.
6. Use **New request** to see the accepted web-intake form. A newly submitted
   request remains `RECEIVED` until an orchestration handoff processes it.

Leave **Guided Ollama incident** untouched until lesson 5. Startup reserves
that durable fictional request as the first safe workflow claim but makes 0 AI
calls.

This lesson shows that the requester can see only owned cases and can supply
information only from `NEEDS_INFORMATION`.

## 3. Service-agent lesson

1. Log out and sign in as `AGT-301`.
2. Open **Demo: service-agent review**.
3. Read the proposed summary and audit history.
4. Confirm the proposal with a short fictional review note.
5. Observe that the case leaves the review queue after the durable decision.

The correction form is also available when the proposed type, summary, or
structured fields need human repair.

## 4. Approver lesson

1. Log out and sign in as `MGR-104`.
2. Open **Demo: assigned approval**.
3. Approve or reject the fictional data change.
4. Reopen the case and inspect the state and append-only event.

The assigned approver can act only on their own pending approval. This focused
portal demo does not run the later Service Desk delivery worker.

## 5. Ollama lesson

Return to PowerShell and run:

```powershell
.\demo.cmd ollama
```

The first execution sends exactly 1 fictional Indonesian incident through the
accepted local `qwen3:4b-instruct` adapter. It uses the real intake,
analysis-start, persistence, deterministic-validation, and AI-analysis
contracts. It does not invoke n8n in this focused lesson. An exact replay reuses
the stored result and makes no additional model call. The intake request itself
is an exact replay of the reserved startup identity, preventing an unrelated
manual request from being claimed accidentally.

Log in again as `EMP-201`, open **Guided Ollama incident**, and inspect its
request type, state, AI summary, and audit history. `NEEDS_REVIEW` is a valid
safe outcome when deterministic validation rejects part of the model proposal.

## 6. Operations lesson

Log in as `ADM-001` and open **Operations**. The counts come from durable
PostgreSQL records and require no AI call.

## 7. Stop and clean up

```powershell
.\demo.cmd stop
```

Cleanup removes the disposable database, containers, network, temporary
secrets, and any loaded accepted language model. The installed Ollama model
files remain available on the laptop.

## Demo boundary

This is a learning surface, not a production deployment. It focuses on the
portal, role isolation, durable human actions, and 1 real local-model proposal.
The repository's controlled end-to-end runners remain the evidence for n8n,
policy retrieval, downstream Service Desk delivery, retry, and recovery.
