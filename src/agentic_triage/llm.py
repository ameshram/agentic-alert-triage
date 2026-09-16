"""The reasoning "brain", behind one interface with two implementations.

    AnthropicClient - real reasoning via Claude tool use (needs an API key).
    MockLLM         - a deterministic, rule-based stand-in.

Why a mock brain? So the entire pipeline and the evaluation harness run offline,
with no API key and no cost, in CI and on any reviewer's laptop. The mock is a
transparent rule engine over the same evidence the real model sees; it is
injection-immune by construction (it does not follow text), which lets the eval
isolate the *harness and policy* behavior from model quality. Run `--live` to
evaluate the real model.

Both clients are stateful for the duration of one alert: `start()` opens a
fresh investigation and `provide_tool_results()` continues it. The agent loop
in agent.py drives them without knowing which one it holds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from .config import HIGH_RISK_COUNTRIES, Settings
from .schemas import Alert, AlertType, RiskAssessment, RiskLevel


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Turn:
    stop: str  # "tool" | "final"
    tool_calls: list[ToolCall] = field(default_factory=list)
    assessment: RiskAssessment | None = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


class LLMClient(Protocol):
    model: str

    def start(self, system: str, user_text: str, tool_specs: list[dict], alert: Alert) -> Turn: ...
    def provide_tool_results(self, results: list[dict]) -> Turn: ...


# --------------------------------------------------------------------------
# Deterministic offline brain
# --------------------------------------------------------------------------
class MockLLM:
    """Rule-based stand-in. Investigates in two rounds, then scores risk."""

    model = "mock"

    def __init__(self) -> None:
        self._alert: Alert | None = None
        self._round = 0
        self._pending: dict[str, str] = {}
        self._ev: dict[str, dict] = {}

    def start(self, system: str, user_text: str, tool_specs: list[dict], alert: Alert) -> Turn:
        self._alert = alert
        self._round = 0
        self._ev = {}
        return self._emit(
            [
                ("get_transaction_history", {"account_id": alert.account_id, "lookback_days": 90}),
                ("get_entity_profile", {"entity_id": alert.subject_entity_id}),
            ]
        )

    def provide_tool_results(self, results: list[dict]) -> Turn:
        for r in results:
            name = self._pending.get(r["tool_use_id"], "?")
            try:
                self._ev[name] = json.loads(r["content"])
            except (json.JSONDecodeError, TypeError):
                self._ev[name] = {}
        self._round += 1
        assert self._alert is not None
        if self._round == 1:
            profile = self._ev.get("get_entity_profile", {})
            return self._emit(
                [
                    ("check_watchlist", {"entity_id": self._alert.subject_entity_id, "name": profile.get("name", "")}),
                    ("search_prior_cases", {"query": self._alert.alert_type.value + " " + self._alert.reason, "k": 3}),
                ]
            )
        return Turn(stop="final", assessment=self._assess(), model=self.model)

    # -- helpers --
    def _emit(self, calls: list[tuple[str, dict]]) -> Turn:
        self._pending = {}
        tool_calls = []
        for name, args in calls:
            cid = f"mock-{self._round}-{name}"
            self._pending[cid] = name
            tool_calls.append(ToolCall(id=cid, name=name, input=args))
        return Turn(stop="tool", tool_calls=tool_calls, model=self.model)

    def _assess(self) -> RiskAssessment:
        assert self._alert is not None
        txns = self._ev.get("get_transaction_history", {})
        profile = self._ev.get("get_entity_profile", {})
        watch = self._ev.get("check_watchlist", {})
        priors = self._ev.get("search_prior_cases", {})

        points = 0
        findings: list[str] = []

        if watch.get("hit"):
            points += 4
            findings.append("Subject matched a watchlist entry.")
        max_amt = txns.get("max_amount", 0.0)
        if max_amt >= 9000:
            points += 2 if self._alert.alert_type == AlertType.STRUCTURING else 1
            findings.append(f"Largest transaction ${max_amt:,.0f} near reporting threshold.")
        risky_geo = set(txns.get("countries", [])) & HIGH_RISK_COUNTRIES
        if risky_geo:
            points += 2
            findings.append(f"Activity involving high-risk geographies: {sorted(risky_geo)}.")
        rflags = profile.get("risk_flags", [])
        if rflags:
            points += min(2, len(rflags))
            findings.append(f"Subject carries standing risk flags: {rflags}.")
        # Only count prior-case precedent when the match is actually relevant -
        # a low-similarity hit is noise, not evidence.
        strong_priors = [
            p
            for p in priors.get("results", [])
            if p.get("disposition") == "confirmed_suspicious" and p.get("score", 0.0) >= 0.25
        ]
        if strong_priors:
            points += 2
            findings.append("Highly similar prior cases were confirmed suspicious.")
        if txns.get("count", 0) >= 20:
            points += 1
            findings.append(f"High transaction velocity: {txns['count']} in lookback window.")
        if self._alert.model_score >= 0.8:
            points += 1

        if points >= 6:
            level, conf = RiskLevel.CRITICAL, 0.90
        elif points >= 4:
            level, conf = RiskLevel.HIGH, 0.85
        elif points >= 2:
            level, conf = RiskLevel.MEDIUM, 0.75
        elif points == 1:
            level, conf = RiskLevel.LOW, 0.70  # mildly odd → below auto-close bar
        else:
            level, conf = RiskLevel.LOW, 0.90
            findings.append("No corroborating risk signals found across evidence.")

        narrative = (
            f"Alert {self._alert.id} ({self._alert.alert_type.value}): assessed {level.value} "
            f"risk from {points} corroborating signal(s) across transactions, KYC profile, "
            f"watchlist, and prior-case precedent."
        )
        return RiskAssessment(
            risk_level=level, confidence=conf, narrative=narrative, key_findings=findings
        )


# --------------------------------------------------------------------------
# Real brain
# --------------------------------------------------------------------------
class AnthropicClient:
    """Drives Claude through a manual tool-use loop (one turn at a time)."""

    def __init__(self, cfg: Settings) -> None:
        import anthropic  # lazy: only needed for the live path

        self._anthropic = anthropic
        self._client = anthropic.Anthropic()
        self.model = cfg.model
        self._cfg = cfg
        self._system = ""
        self._tool_specs: list[dict] = []
        self._messages: list[dict] = []

    def start(self, system: str, user_text: str, tool_specs: list[dict], alert: Alert) -> Turn:
        self._system = system
        self._tool_specs = tool_specs
        self._messages = [{"role": "user", "content": user_text}]
        return self._call()

    def provide_tool_results(self, results: list[dict]) -> Turn:
        content = []
        for r in results:
            block = {
                "type": "tool_result",
                "tool_use_id": r["tool_use_id"],
                "content": r["content"],
            }
            if r.get("is_error"):
                block["is_error"] = True
            content.append(block)
        self._messages.append({"role": "user", "content": content})
        return self._call()

    def _call(self) -> Turn:
        import time

        while True:  # loop only to resume a paused server turn (rare here)
            t0 = time.perf_counter()
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self._cfg.max_tokens,
                system=self._system,
                tools=self._tool_specs,
                messages=self._messages,
                thinking={"type": "adaptive"},
                output_config={"effort": self._cfg.effort},
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0
            usage = resp.usage
            self._messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "pause_turn":
                continue

            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            submit = next((b for b in tool_uses if b.name == "submit_assessment"), None)
            if submit is not None:
                # strict=True guarantees these fields validate.
                assessment = RiskAssessment(**dict(submit.input))
                return Turn(
                    stop="final",
                    assessment=assessment,
                    model=self.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    latency_ms=latency_ms,
                )
            if tool_uses:
                return Turn(
                    stop="tool",
                    tool_calls=[ToolCall(id=b.id, name=b.name, input=dict(b.input)) for b in tool_uses],
                    model=self.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    latency_ms=latency_ms,
                )
            # Model ended the turn without submitting an assessment.
            return Turn(
                stop="final",
                assessment=None,
                model=self.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=latency_ms,
            )
