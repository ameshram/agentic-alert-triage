"""The decision policy: turn a model risk assessment into an action.

This is the core guardrail of the system. The LLM produces a RiskAssessment
(a recommendation); it never fires an irreversible action directly. This
deterministic, auditable function decides auto_close / escalate / hold and
whether a human is required. Because it is pure and rule-based, it is fully
unit-tested (tests/test_policy.py) and every decision carries a reason string.

Design principle: a false auto-close (closing a genuinely suspicious alert) is
the most expensive error, so the rules are asymmetric — only a LOW-risk,
high-confidence, un-flagged alert is ever auto-closed. Everything else, and
anything that trips a guardrail, goes to a human.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .schemas import Decision, RiskAssessment, RiskLevel


@dataclass
class PolicyFlags:
    injection: bool = False
    watchlist_hit: bool = False
    budget_exceeded: bool = False


@dataclass
class PolicyOutcome:
    decision: Decision
    requires_human: bool
    reason: str


def decide(
    assessment: RiskAssessment, flags: PolicyFlags, cfg: Settings
) -> PolicyOutcome:
    # 1. Safety guardrails first — these override the model's view entirely.
    if flags.injection:
        return PolicyOutcome(
            Decision.HOLD,
            requires_human=True,
            reason="Untrusted content tripped an injection heuristic; manual review required.",
        )
    if flags.budget_exceeded:
        return PolicyOutcome(
            Decision.HOLD,
            requires_human=True,
            reason="Investigation exceeded its step/cost/latency budget before concluding.",
        )
    if flags.watchlist_hit:
        return PolicyOutcome(
            Decision.ESCALATE,
            requires_human=True,
            reason="Subject matched a sanctions/PEP watchlist.",
        )

    # 2. Risk-driven routing.
    if assessment.risk_level == RiskLevel.CRITICAL:
        return PolicyOutcome(Decision.ESCALATE, True, "Critical risk assessed.")
    if assessment.risk_level == RiskLevel.HIGH:
        return PolicyOutcome(Decision.ESCALATE, True, "High risk assessed.")
    if assessment.risk_level == RiskLevel.MEDIUM:
        return PolicyOutcome(
            Decision.HOLD, True, "Medium risk; hold for analyst review."
        )

    # 3. LOW risk — the only path to auto-close, gated on confidence.
    if assessment.confidence >= cfg.auto_close_min_confidence:
        return PolicyOutcome(
            Decision.AUTO_CLOSE,
            requires_human=False,
            reason=(
                f"Low risk with confidence {assessment.confidence:.2f} "
                f">= threshold {cfg.auto_close_min_confidence:.2f}."
            ),
        )
    return PolicyOutcome(
        Decision.HOLD,
        requires_human=True,
        reason=(
            f"Low risk but confidence {assessment.confidence:.2f} "
            f"below auto-close threshold {cfg.auto_close_min_confidence:.2f}."
        ),
    )
