"""End-to-end orchestration: one alert in, one auditable TriageResult out."""

from __future__ import annotations

from pathlib import Path

from .agent import investigate
from .config import Settings
from .llm import AnthropicClient, LLMClient, MockLLM
from .observability import Tracer
from .policy import decide
from .schemas import Alert, TriageResult
from .tools import SyntheticStore, build_tools


class TriageEngine:
    def __init__(self, store: SyntheticStore, llm: LLMClient, cfg: Settings | None = None) -> None:
        self.store = store
        self.llm = llm
        self.cfg = cfg or Settings()
        self.tools = build_tools()

    def triage(self, alert: Alert) -> TriageResult:
        tracer = Tracer()
        assessment, evidence, flags, steps = investigate(
            alert, self.store, self.tools, self.llm, tracer, self.cfg
        )
        outcome = decide(assessment, flags, self.cfg)
        return TriageResult(
            alert_id=alert.id,
            decision=outcome.decision,
            risk_level=assessment.risk_level,
            requires_human=outcome.requires_human,
            confidence=assessment.confidence,
            narrative=assessment.narrative,
            key_findings=assessment.key_findings,
            evidence=evidence,
            policy_reason=outcome.reason,
            steps=steps,
            flagged_injection=flags.injection,
            budget_exceeded=flags.budget_exceeded,
            model=self.llm.model,
            cost_usd=round(tracer.cost_usd, 6),
            latency_ms=round(tracer.latency_ms, 1),
            trace=tracer.events,
        )


def build_engine(
    data_dir: str | Path | None = None, live: bool = False, cfg: Settings | None = None
) -> TriageEngine:
    """Convenience factory used by the CLI, the eval harness, and the service."""
    cfg = cfg or Settings()
    store = SyntheticStore.from_dir(data_dir or cfg.data_dir)
    llm: LLMClient = AnthropicClient(cfg) if live else MockLLM()
    return TriageEngine(store, llm, cfg)
