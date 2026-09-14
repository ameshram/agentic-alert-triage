"""Narrative-quality scoring (LLM-as-judge, with an offline stand-in).

Decision metrics (precision/recall/FNR) check *what* the system decided. They
don't check whether the rationale is any good. The judge scores narrative
quality on a 0..1 rubric: is it grounded in evidence, specific, and calibrated?

MockJudge is a deterministic heuristic so the eval runs offline. ClaudeJudge
uses the model with a structured rubric; enable it with `--judge claude`.
"""

from __future__ import annotations

from agentic_triage.schemas import TriageResult


class MockJudge:
    name = "mock"

    def score(self, result: TriageResult) -> float:
        s = 0.0
        if len(result.narrative) >= 60:
            s += 0.4
        s += min(0.4, 0.1 * len(result.key_findings))
        if result.evidence:  # rationale rests on gathered evidence
            s += 0.2
        return round(min(1.0, s), 3)


class ClaudeJudge:
    name = "claude"

    def __init__(self, model: str = "claude-sonnet-5") -> None:
        import anthropic  # lazy

        self._client = anthropic.Anthropic()
        self._model = model

    def score(self, result: TriageResult) -> float:
        rubric = (
            "Score this AML triage rationale from 0 to 1 on: (a) grounded in the "
            "cited evidence, (b) specific rather than generic, (c) confidence "
            "calibrated to the evidence. Return only the number.\n\n"
            f"risk={result.risk_level.value} confidence={result.confidence}\n"
            f"narrative={result.narrative}\n"
            f"findings={result.key_findings}"
        )
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=16,
            messages=[{"role": "user", "content": rubric}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "0")
        try:
            return max(0.0, min(1.0, float(text.strip().split()[0])))
        except (ValueError, IndexError):
            return 0.0
