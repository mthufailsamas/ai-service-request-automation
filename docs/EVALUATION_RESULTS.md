# Locked Evaluation Results

**Status:** completed controlled evaluation; fixed gate `CHECK`

**Evaluation date:** 2026-08-20

## Integrity

- Contract: `v1`
- Population: 40 semantic cases and 10 workflow-control cases
- Corpus SHA-256:
  `17e6806295cba62a519353c7db4396eefbc0e2e07a999972520ef009b6477354`
- Evidence: [locked-system-evaluation-v1.json](../output/locked_evaluation/locked-system-evaluation-v1.json)
- Evidence SHA-256:
  `34cad9e3150cf43b9d043888a717a1527e202b0058f53c08a3c2fe859adc5afe`
- Analysis model: `qwen3:4b-instruct`
- Embedding model: `qwen3-embedding:0.6b`
- Execution segments: 2, with 0 additional model calls during the deterministic
  resume segment

## Fixed Gates

| Measure | Fixed target | Result | Gate |
| --- | ---: | ---: | --- |
| Classification macro F1 | at least 90% | 94.6% | `PASS` |
| Required-field accuracy | at least 90% | 88.3% | `CHECK` |
| Policy retrieval Recall@3 | at least 90% | 0.0% | `CHECK` |
| Citation validity | 100% | 0.0% | `CHECK` |
| Route and final-state accuracy | at least 95% | 30.0% | `CHECK` |
| Semantic task success | at least 90% | 20.0% | `CHECK` |
| Workflow controls | 100% | 100.0% | `PASS` |
| Recoverable failures | 100% | 100.0% | `PASS` |

## Runtime Evidence

- Median controlled processing latency: 10.268 seconds
- P95 controlled processing latency: 14.984 seconds
- Cold policy indexing: 3.476 seconds
- Local Ollama calls: 42
- Hosted or paid AI calls: 0
- Unfinished outbox messages: 0
- Duplicate terminal effects: 0
- Cleanup: `PASS`

## Interpretation

The classification target, all 10 deterministic workflow controls, and both
recoverable-failure controls passed. The required-field, routing, retrieval,
citation, and task-success targets did not pass, so the overall quality gate is
honestly `CHECK`.

All 8 locked policy cases reached `NEEDS_REVIEW` during deterministic proposal
validation before retrieval. Their 0.0% retrieval and citation scores describe
the combined analyzer-to-retrieval route, not the embedding model in isolation.
The earlier focused embedding-suitability benchmark remains separate evidence.

The operational conclusion is bounded: deterministic orchestration,
idempotency, recovery, and human-safe fallback behaved correctly in the
fictional local evaluation, while the selected analysis pipeline is not ready
for unattended semantic processing. No production, real-user, or business
impact claim is supported.

The accepted corpus, prompts, thresholds, and completed evidence remain frozen.
Future model improvements require a separate development corpus and a new
untouched evaluation contract.
