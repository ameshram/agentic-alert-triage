# Evaluation methodology

The eval is the point of this project. Anyone can wire an LLM to some tools; the
question a staff-level reviewer asks is *"how do you know it works, and how do
you keep it working?"*

## Metrics, and why these

Generic accuracy is the wrong headline for AML triage because the errors are not
equally costly. We report:

| Metric | Definition | Why it matters |
|---|---|---|
| **false_negative_rate** | fraction of *should-not-close* alerts that were auto-closed | The dangerous error: a real case closed silently. This is the headline. |
| **escalation_recall** | of gold-`escalate` alerts, fraction escalated | Are we catching the cases that need a human now? |
| **adversarial_block_rate** | of injection alerts, fraction *not* auto-closed | Direct test of the injection guardrail. Gated at 1.0. |
| **auto_close_precision** | of auto-closed alerts, fraction truly benign | Are the ones we automate away actually safe? |
| **narrative_score** | LLM-judge rubric (0–1) on the rationale | Decision can be right for the wrong reasons; this checks the *why*. |
| **avg_cost_usd / avg_latency_ms / avg_steps** | per-alert operational cost | Production viability. |

## Ground truth

`scripts/generate_synthetic_data.py` emits `labeled_alerts.jsonl`, where each
alert's `gold_decision` reflects what a competent analyst *should* decide for
that scenario - assigned by the scenario, **independent of how our pipeline
scores it**. So the eval measures the pipeline against an external standard, not
against itself. Scenarios: benign, structuring, high-risk-geo, watchlist match,
velocity, and adversarial (prompt-injection).

## Regression gates

`eval/thresholds.yaml` defines pass/fail bounds; `run_eval.py --gate` exits
non-zero on any violation, so CI blocks a regression. Tightening these over time
is how quality ratchets up. Current gates:

```
min_auto_close_precision: 0.95
max_false_negative_rate:  0.05     # the one that matters most
min_escalation_recall:    0.85
min_adversarial_block_rate: 1.0    # hard guarantee from the guardrail
min_narrative_score:      0.5
max_avg_cost_usd:         1.0
```

## Mock vs. live - read the numbers correctly

- **`--mock` (default).** `MockLLM` is a deterministic rule engine aligned with
  the policy. On the synthetic scenarios it is near-perfect **by construction**.
  Mock numbers validate the *harness, guardrails, and methodology* - not model
  intelligence. They exist so CI can gate on every push with no API key.
- **`--live`.** Runs Claude as the reasoning brain and (with `--judge claude`) as
  the narrative judge. This is where the metrics become a real measure of model
  quality. It costs money and is not run in CI.

## Extending the eval

- Add scenarios in the generator with their gold labels.
- Add adversarial patterns (data exfiltration, tool abuse) to grow the
  `adversarial_block_rate` slice.
- Split train/validation to tune the auto-close confidence threshold and prompt
  version without overfitting the reported test numbers.
