"""The agent loop: reason → call tools → observe → assess, under a budget.

This owns the investigation. It drives whichever LLMClient it is given, executes
the tool calls the model requests, sanitizes and feeds back results, and enforces
hard budgets (max steps / cost / latency). Two things never leave this loop:
the private "_flags" from tools (injection signals) and the watchlist-hit signal
— both are collected here and handed to the policy engine, not the model.
"""

from __future__ import annotations

import json

from .config import Settings
from .llm import LLMClient, Turn
from .observability import Tracer, span
from .policy import PolicyFlags
from .prompts import SYSTEM_PROMPT, initial_user_message
from .schemas import Alert, Evidence, RiskAssessment, RiskLevel
from .tools import SyntheticStore, Tool, anthropic_tool_specs


def _summarize(name: str, out: dict) -> str:
    if name == "get_transaction_history":
        return (
            f"{out.get('count', 0)} txns; max ${out.get('max_amount', 0):,.0f}; "
            f"countries={out.get('countries', [])}; channels={out.get('channels', [])}"
        )
    if name == "get_entity_profile":
        if not out.get("found"):
            return "entity not found"
        return f"{out.get('name')} ({out.get('kind')}, {out.get('country')}); flags={out.get('risk_flags', [])}"
    if name == "check_watchlist":
        return "watchlist HIT" if out.get("hit") else "no watchlist match"
    if name == "search_prior_cases":
        return f"{len(out.get('results', []))} prior case(s) retrieved"
    return "ok"


def investigate(
    alert: Alert,
    store: SyntheticStore,
    tools: dict[str, Tool],
    llm: LLMClient,
    tracer: Tracer,
    cfg: Settings,
) -> tuple[RiskAssessment, list[Evidence], PolicyFlags, int]:
    tool_specs = anthropic_tool_specs(tools)
    flags = PolicyFlags()
    evidence: list[Evidence] = []

    turn: Turn = llm.start(SYSTEM_PROMPT, initial_user_message(alert), tool_specs, alert)
    tracer.record_llm(turn.model, turn.input_tokens, turn.output_tokens, turn.latency_ms)

    steps = 0
    while turn.stop == "tool":
        steps += 1
        # Budget guardrail: stop before doing more work if we've spent too much.
        if (
            steps > cfg.max_steps
            or tracer.cost_usd > cfg.max_cost_usd
            or tracer.latency_ms > cfg.max_latency_ms
        ):
            flags.budget_exceeded = True
            break

        results = []
        for tc in turn.tool_calls:
            tool = tools.get(tc.name)
            ok = True
            with span() as sp:
                if tool is None:
                    out: dict = {"error": f"unknown tool {tc.name}"}
                    ok = False
                else:
                    try:
                        out = tool.fn(store, **tc.input)
                    except Exception as exc:  # tool failures are data, not crashes
                        out = {"error": str(exc)}
                        ok = False
            tracer.record_tool(tc.name, sp.ms, ok)

            # Collect (and hide from the model) the injection + watchlist signals.
            raw_flags = out.pop("_flags", []) if isinstance(out, dict) else []
            if raw_flags:
                flags.injection = True
            if tc.name == "check_watchlist" and out.get("hit"):
                flags.watchlist_hit = True

            evidence.append(Evidence(source=tc.name, summary=_summarize(tc.name, out), data=out))
            results.append(
                {"tool_use_id": tc.id, "content": json.dumps(out), "is_error": not ok}
            )

        turn = llm.provide_tool_results(results)
        tracer.record_llm(turn.model, turn.input_tokens, turn.output_tokens, turn.latency_ms)

    # Resolve the assessment. If the loop was cut short by the budget, or the
    # model ended without submitting one, fall back to a conservative placeholder
    # and let the policy engine route it to a human.
    if turn.stop == "final" and turn.assessment is not None and not flags.budget_exceeded:
        assessment = turn.assessment
    else:
        if turn.assessment is None:
            flags.budget_exceeded = True
        assessment = RiskAssessment(
            risk_level=RiskLevel.MEDIUM,
            confidence=0.30,
            narrative="Investigation did not conclude within budget; routing to human review.",
            key_findings=[],
        )

    return assessment, evidence, flags, steps
