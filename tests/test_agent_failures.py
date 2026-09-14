"""Failure-path coverage for the agent loop (agent.investigate, via TriageEngine).

These drive the loop with tiny scripted LLM clients — no MockLLM rules, no API
key — so the defensive branches actually run: budget exhaustion, a model that
never submits an assessment, an unknown tool, and a tool that raises. In every
case the system must fail closed (route to a human / never crash), which is the
property that matters for a triage system.
"""

from datetime import datetime

from agentic_triage.config import Settings
from agentic_triage.llm import ToolCall, Turn
from agentic_triage.pipeline import TriageEngine
from agentic_triage.schemas import (
    Alert,
    AlertType,
    Decision,
    Entity,
    RiskAssessment,
    RiskLevel,
)
from agentic_triage.tools import SyntheticStore

TS = datetime(2026, 1, 1)


def _alert() -> Alert:
    return Alert(id="F1", account_id="a1", subject_entity_id="e1",
                 alert_type=AlertType.VELOCITY, created_at=TS, model_score=0.3, reason="x")


def _store() -> SyntheticStore:
    return SyntheticStore({"e1": Entity(id="e1", name="Test Subject")}, [], watchlist=[], prior_cases=[])


class _AlwaysToolLLM:
    """Never finishes: every turn requests a valid tool, forcing the budget guard."""

    model = "fake-loop"

    def start(self, system, user_text, tool_specs, alert):
        return self._tool()

    def provide_tool_results(self, results):
        return self._tool()

    def _tool(self) -> Turn:
        return Turn(stop="tool", model=self.model,
                    tool_calls=[ToolCall(id="t", name="get_entity_profile", input={"entity_id": "e1"})])


class _NoAssessmentLLM:
    """Ends the turn immediately without submitting an assessment."""

    model = "fake-empty"

    def start(self, system, user_text, tool_specs, alert):
        return Turn(stop="final", assessment=None, model=self.model)

    def provide_tool_results(self, results):
        return Turn(stop="final", assessment=None, model=self.model)


class _ScriptedThenAssessLLM:
    """Calls one named tool once, then finishes with a low-risk assessment."""

    model = "fake-scripted"

    def __init__(self, tool_name: str, tool_input: dict | None = None):
        self._tool_name = tool_name
        self._tool_input = tool_input or {}

    def start(self, system, user_text, tool_specs, alert):
        return Turn(stop="tool", model=self.model,
                    tool_calls=[ToolCall(id="t", name=self._tool_name, input=self._tool_input)])

    def provide_tool_results(self, results):
        return Turn(stop="final", model=self.model, assessment=RiskAssessment(
            risk_level=RiskLevel.LOW, confidence=0.9, narrative="clean", key_findings=["none"]))


def test_budget_exhaustion_routes_to_human():
    res = TriageEngine(_store(), _AlwaysToolLLM(), Settings()).triage(_alert())
    assert res.budget_exceeded is True
    assert res.requires_human is True
    assert res.decision != Decision.AUTO_CLOSE
    assert res.confidence == 0.30  # conservative fallback assessment


def test_model_without_assessment_routes_to_human():
    res = TriageEngine(_store(), _NoAssessmentLLM(), Settings()).triage(_alert())
    assert res.budget_exceeded is True
    assert res.requires_human is True
    assert res.decision != Decision.AUTO_CLOSE


def test_unknown_tool_is_recorded_not_crashed():
    res = TriageEngine(_store(), _ScriptedThenAssessLLM("does_not_exist"), Settings()).triage(_alert())
    assert res.alert_id == "F1"  # investigation still concluded
    assert any("error" in (ev.data or {}) for ev in res.evidence)


def test_tool_exception_is_captured_as_evidence():
    eng = TriageEngine(_store(), _ScriptedThenAssessLLM("get_entity_profile", {"entity_id": "e1"}), Settings())

    def _boom(store, **kwargs):
        raise RuntimeError("tool exploded")

    eng.tools["get_entity_profile"].fn = _boom
    res = eng.triage(_alert())
    assert res.alert_id == "F1"  # exception was swallowed as data, not raised
    assert any("tool exploded" in str(ev.data) for ev in res.evidence)
