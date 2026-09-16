"""Run the triage pipeline over the labeled set and score it against gold.

Offline by default (MockLLM). `--live` evaluates the real model; `--gate` exits
non-zero if any threshold in thresholds.yaml is violated, so CI blocks a
regression. Writes eval/report.md and eval/report.json.

    python -m eval.run_eval --data data/synthetic --gate
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # so the sibling `judge` module imports
from judge import ClaudeJudge, MockJudge  # noqa: E402

from agentic_triage.pipeline import build_engine  # noqa: E402
from agentic_triage.prompts import PROMPT_VERSION  # noqa: E402
from agentic_triage.schemas import Decision  # noqa: E402
from agentic_triage.tools import load_labeled_alerts  # noqa: E402

SHOULD_NOT_CLOSE = {Decision.ESCALATE, Decision.HOLD}


def _safe_div(a: float, b: float, default: float = 1.0) -> float:
    # `default` is the vacuous value when b == 0. Coverage/precision metrics are
    # min-gated, so an empty denominator is vacuously "perfect" (1.0); error-rate
    # metrics (e.g. false_negative_rate) are max-gated, so their vacuous value is
    # 0.0 - otherwise an empty class would spuriously fail the gate.
    return a / b if b else default


def evaluate(data_dir: str, live: bool, judge_name: str) -> dict:
    engine = build_engine(data_dir=data_dir, live=live)
    labeled = load_labeled_alerts(data_dir)
    judge = ClaudeJudge() if judge_name == "claude" else MockJudge()

    confusion: Counter = Counter()
    auto_closed = closed_correct = 0
    fn = fn_total = 0
    esc_hit = esc_total = 0
    adv_blocked = adv_total = 0
    narrative_scores: list[float] = []
    cost = latency = steps = 0.0
    rows = []

    for la in labeled:
        res = engine.triage(la.alert)
        pred, gold = res.decision, la.gold_decision
        confusion[(gold.value, pred.value)] += 1
        narrative_scores.append(judge.score(res))
        cost += res.cost_usd
        latency += res.latency_ms
        steps += res.steps

        if pred == Decision.AUTO_CLOSE:
            auto_closed += 1
            if gold == Decision.AUTO_CLOSE:
                closed_correct += 1
        if gold in SHOULD_NOT_CLOSE:
            fn_total += 1
            if pred == Decision.AUTO_CLOSE:
                fn += 1
        if gold == Decision.ESCALATE:
            esc_total += 1
            if pred == Decision.ESCALATE:
                esc_hit += 1
        if la.is_adversarial:
            adv_total += 1
            if pred != Decision.AUTO_CLOSE:
                adv_blocked += 1
        rows.append(
            {"alert": la.alert.id, "gold": gold.value, "pred": pred.value,
             "risk": res.risk_level.value, "injection": res.flagged_injection,
             "adversarial": la.is_adversarial, "policy_reason": res.policy_reason}
        )

    n = len(labeled)
    return {
        "n": n,
        "model": engine.llm.model,
        "prompt_version": PROMPT_VERSION,
        "judge": judge.name,
        "metrics": {
            "decision_accuracy": round(_safe_div(sum(v for (g, p), v in confusion.items() if g == p), n), 3),
            "auto_close_precision": round(_safe_div(closed_correct, auto_closed), 3),
            "false_negative_rate": round(_safe_div(fn, fn_total, default=0.0), 3),
            "escalation_recall": round(_safe_div(esc_hit, esc_total), 3),
            "adversarial_block_rate": round(_safe_div(adv_blocked, adv_total), 3),
            "narrative_score": round(sum(narrative_scores) / n, 3) if n else 0.0,
            "avg_cost_usd": round(cost / n, 6) if n else 0.0,
            "avg_latency_ms": round(latency / n, 1) if n else 0.0,
            "avg_steps": round(steps / n, 2) if n else 0.0,
        },
        "confusion": {f"{g}->{p}": v for (g, p), v in sorted(confusion.items())},
        "rows": rows,
    }


def check_gates(metrics: dict, thresholds: dict) -> list[str]:
    """Return a list of gate-failure messages (empty = all passed)."""
    directions = {  # metric -> ("min"|"max", threshold_key)
        "auto_close_precision": ("min", "min_auto_close_precision"),
        "false_negative_rate": ("max", "max_false_negative_rate"),
        "escalation_recall": ("min", "min_escalation_recall"),
        "adversarial_block_rate": ("min", "min_adversarial_block_rate"),
        "narrative_score": ("min", "min_narrative_score"),
        "avg_cost_usd": ("max", "max_avg_cost_usd"),
    }
    failures = []
    for metric, (kind, key) in directions.items():
        if key not in thresholds:
            continue
        val, lim = metrics[metric], thresholds[key]
        if (kind == "min" and val < lim) or (kind == "max" and val > lim):
            failures.append(f"{metric}={val} violates {kind} {lim}")
    return failures


def render_markdown(report: dict, failures: list[str]) -> str:
    m = report["metrics"]
    lines = [
        "# Evaluation report",
        "",
        f"- model: `{report['model']}`  ·  prompt: `{report['prompt_version']}`  ·  judge: `{report['judge']}`",
        f"- alerts evaluated: **{report['n']}**",
        "",
        "> NOTE: with the `mock` model these numbers characterize the **harness "
        "and policy guardrails**, not model intelligence. Run `--live` to "
        "evaluate the real model.",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    for k, v in m.items():
        lines.append(f"| {k} | {v} |")
    lines += ["", "## Confusion (gold → predicted)", "", "| pair | count |", "|---|---|"]
    for k, v in report["confusion"].items():
        lines.append(f"| {k} | {v} |")
    lines += ["", "## Gates", ""]
    lines.append("✅ all gates passed" if not failures else "❌ " + "; ".join(failures))
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/synthetic")
    ap.add_argument("--live", action="store_true", help="use the real model instead of MockLLM")
    ap.add_argument("--judge", choices=["mock", "claude"], default="mock")
    ap.add_argument("--gate", action="store_true", help="exit non-zero if a threshold is violated")
    ap.add_argument("--thresholds", default=str(Path(__file__).parent / "thresholds.yaml"))
    ap.add_argument("--report", default=str(Path(__file__).parent / "report.md"))
    args = ap.parse_args()

    report = evaluate(args.data, args.live, args.judge)
    thresholds = yaml.safe_load(Path(args.thresholds).read_text())
    failures = check_gates(report["metrics"], thresholds)

    md = render_markdown(report, failures)
    Path(args.report).write_text(md)
    Path(args.report).with_suffix(".json").write_text(json.dumps(report, indent=2))
    print(md)

    if args.gate and failures:
        print(f"\nGATE FAILED: {len(failures)} threshold(s) violated.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
