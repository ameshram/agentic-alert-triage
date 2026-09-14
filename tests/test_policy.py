"""The policy engine is pure and safety-critical, so it is table-tested.

The invariants that matter: guardrails (injection / budget / watchlist) override
the model's view, and AUTO_CLOSE is reachable only from a LOW-risk,
high-confidence, un-flagged assessment.
"""

import pytest

from agentic_triage.config import Settings
from agentic_triage.policy import PolicyFlags, decide
from agentic_triage.schemas import Decision, RiskAssessment, RiskLevel

CFG = Settings()


def _assess(level: RiskLevel, conf: float) -> RiskAssessment:
    return RiskAssessment(risk_level=level, confidence=conf, narrative="n", key_findings=[])


@pytest.mark.parametrize(
    "level,conf,flags,expected,human",
    [
        # Guardrails override everything, even a LOW/high-confidence assessment.
        (RiskLevel.LOW, 0.99, PolicyFlags(injection=True), Decision.HOLD, True),
        (RiskLevel.LOW, 0.99, PolicyFlags(budget_exceeded=True), Decision.HOLD, True),
        (RiskLevel.LOW, 0.99, PolicyFlags(watchlist_hit=True), Decision.ESCALATE, True),
        # Risk-driven routing.
        (RiskLevel.CRITICAL, 0.9, PolicyFlags(), Decision.ESCALATE, True),
        (RiskLevel.HIGH, 0.9, PolicyFlags(), Decision.ESCALATE, True),
        (RiskLevel.MEDIUM, 0.9, PolicyFlags(), Decision.HOLD, True),
        # LOW risk: auto-close only above the confidence threshold.
        (RiskLevel.LOW, 0.85, PolicyFlags(), Decision.AUTO_CLOSE, False),
        (RiskLevel.LOW, 0.50, PolicyFlags(), Decision.HOLD, True),
    ],
)
def test_decisions(level, conf, flags, expected, human):
    outcome = decide(_assess(level, conf), flags, CFG)
    assert outcome.decision == expected
    assert outcome.requires_human == human
    assert outcome.reason  # every decision is explained


def test_auto_close_threshold_boundary():
    at = decide(_assess(RiskLevel.LOW, CFG.auto_close_min_confidence), PolicyFlags(), CFG)
    below = decide(_assess(RiskLevel.LOW, CFG.auto_close_min_confidence - 0.01), PolicyFlags(), CFG)
    assert at.decision == Decision.AUTO_CLOSE
    assert below.decision == Decision.HOLD
