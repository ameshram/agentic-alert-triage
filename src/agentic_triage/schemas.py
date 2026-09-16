"""Domain model for alert triage.

These types are the contract between every component: the synthetic data,
the tools, the agent loop, the policy engine, and the evaluation harness all
speak in terms of these objects. Keeping the model explicit (rather than
passing dicts around) is what makes the pipeline testable and auditable.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class AlertType(str, Enum):
    STRUCTURING = "structuring"
    VELOCITY = "velocity"
    HIGH_RISK_GEO = "high_risk_geo"
    WATCHLIST_MATCH = "watchlist_match"
    LARGE_CASH = "large_cash"
    DORMANT_REACTIVATION = "dormant_reactivation"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Decision(str, Enum):
    """The three terminal actions the system can take on an alert.

    AUTO_CLOSE is the only action taken without a human. Everything the model
    is unsure about, or that trips a guardrail, routes to a human.
    """

    AUTO_CLOSE = "auto_close"
    ESCALATE = "escalate"
    HOLD = "hold"


class Entity(BaseModel):
    id: str
    name: str
    kind: str = "individual"  # "individual" | "business"
    country: str = "US"
    risk_flags: list[str] = Field(default_factory=list)


class Transaction(BaseModel):
    id: str
    account_id: str
    ts: datetime
    amount: float
    currency: str = "USD"
    direction: str = "debit"  # "debit" | "credit"
    channel: str = "ach"  # ach | wire | card | p2p | cash
    counterparty_id: str | None = None
    counterparty_name: str = ""
    country: str = "US"
    memo: str = ""  # UNTRUSTED free text - see security.py


class Alert(BaseModel):
    id: str
    account_id: str
    subject_entity_id: str
    alert_type: AlertType
    created_at: datetime
    model_score: float = 0.0  # upstream detection score, 0..1
    reason: str = ""


class Evidence(BaseModel):
    source: str  # tool name that produced it
    summary: str
    data: dict = Field(default_factory=dict)


class RiskAssessment(BaseModel):
    """The reasoning artifact the LLM produces. It is a *recommendation*,
    not an action - policy.py converts it into a Decision."""

    risk_level: RiskLevel
    confidence: float = Field(ge=0.0, le=1.0)
    narrative: str
    key_findings: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    """The full, auditable outcome for one alert."""

    alert_id: str
    decision: Decision
    risk_level: RiskLevel
    requires_human: bool
    confidence: float
    narrative: str
    key_findings: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    policy_reason: str = ""
    steps: int = 0
    flagged_injection: bool = False
    budget_exceeded: bool = False
    model: str = ""
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    trace: list[dict] = Field(default_factory=list)


class LabeledAlert(BaseModel):
    """An alert plus its ground-truth label, used only by the eval harness."""

    alert: Alert
    gold_decision: Decision
    gold_risk: RiskLevel
    is_adversarial: bool = False
    note: str = ""
