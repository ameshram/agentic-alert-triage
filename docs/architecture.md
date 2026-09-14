# Architecture

## Components & data flow

```
Alert ─► pipeline.triage()
          └─ agent.investigate()            # tool-use loop, budgeted
               ├─ llm (AnthropicClient|MockLLM)   # reasons, requests tools
               ├─ tools (over SyntheticStore)     # transactions/entity/watchlist/RAG
               │    └─ security.scan/wrap         # untrusted text handled here
               └─ Tracer                          # tokens, latency, cost per call
          └─ policy.decide(assessment, flags)     # deterministic action + human?
     ─► TriageResult (decision, rationale, evidence, trace, cost, latency)
```

One alert in, one auditable `TriageResult` out. The result carries the decision,
the model's narrative and cited findings, the raw evidence, the policy reason,
and the full per-call trace.

## Key decisions (ADR-style)

### 1. The model reasons; a deterministic policy decides the action
**Decision:** the LLM outputs a `RiskAssessment` (recommendation); `policy.py`
converts it to `auto_close | escalate | hold`.
**Why:** irreversible/consequential actions must be governed by auditable rules,
not free-form model output. It lets us encode the *asymmetric* cost of errors
(never auto-close on thin evidence) and unit-test every decision path.
**Alternative rejected:** let the model emit the action directly (or via a
forced tool call). Simpler, but unauditable and unsafe — a single prompt
injection or a miscalibrated confidence could close a real case.

### 2. A swappable brain with a deterministic mock
**Decision:** `LLMClient` is an interface with `AnthropicClient` and `MockLLM`.
**Why:** the whole pipeline + eval run offline, deterministically, in CI with no
key and no cost. It also isolates *harness* correctness from *model* quality —
the mock lets us prove the guardrails and metrics work independent of any model.
**Alternative rejected:** mocking the SDK at the HTTP layer — brittle and doesn't
give a deterministic reasoning policy to test the pipeline against.

### 3. Retrieval isolated behind a tiny interface
**Decision:** `retrieval.VectorStore` with a dependency-free hashing embedding is
the default; production swaps in real embeddings + pgvector.
**Why:** keeps the repo runnable with zero services while making the production
path a one-file change. Prior-case evidence is **weighted by relevance** so
low-similarity hits don't leak risk into unrelated alerts.

### 4. Manual tool-use loop, not the SDK tool-runner
**Decision:** own the loop in `agent.py`.
**Why:** the loop is where the budget guardrail, the untrusted-text handling, and
the flag collection live — all per-turn concerns that are clearest when the loop
is explicit. The SDK tool-runner is a fine choice when those hooks aren't needed.

## Failure modes & how they're handled

| Failure | Handling |
|---|---|
| Model never submits an assessment | Loop ends → conservative MEDIUM placeholder → `hold` (human). |
| Step / cost / latency budget exceeded | `budget_exceeded` flag → `hold` (human). |
| Tool raises | Caught; returned to the model as an `is_error` tool result (data, not a crash). |
| Prompt injection in untrusted text | Scanned + wrapped; any flag forces `hold`. |
| Watchlist hit | Guardrail forces at least `escalate`, regardless of model view. |
| Malformed model output | `submit_assessment` is `strict`; args are schema-validated. |

## At scale (production considerations)

- **Throughput:** the pipeline is stateless per alert → scale horizontally.
  Use async + a cheaper first-pass model (`claude-sonnet-5`), reserving Opus for
  escalation-worthy cases; batch offline backfills.
- **Cost/latency:** tracked per case; budgets cap worst cases. The auto-close
  path (LOW/high-confidence) short-circuits investigation depth.
- **State:** cases + dispositions in Postgres; prior-case corpus in pgvector.
- **Prompt/version management:** prompts are versioned (`prompts.PROMPT_VERSION`)
  and recorded in every eval report so behavior changes are attributable.
- **Human-in-the-loop:** `hold`/`escalate` populate an analyst queue; their
  dispositions feed back as new labeled eval data.
