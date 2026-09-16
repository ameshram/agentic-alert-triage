"""Configuration: model, budgets, policy thresholds, and pricing.

Everything that a reviewer might want to tune lives here, not scattered through
the code. Budgets and thresholds are first-class because at staff level the
interesting questions are "what does it cost?" and "when do we stop trusting
the model and call a human?" - both answered here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Illustrative per-1M-token prices (USD), current as of the Anthropic pricing
# table cached 2026-06. VERIFY against current pricing before quoting costs:
# https://www.anthropic.com/pricing
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    # MockLLM has no token cost.
    "mock": {"input": 0.0, "output": 0.0},
}


def _default_data_dir() -> Path:
    return Path(os.environ.get("TRIAGE_DATA_DIR", "data/synthetic"))


@dataclass
class Settings:
    # Which model runs the investigation. Opus 5 is the default for reasoning
    # quality; for high-volume triage, sonnet-5 is the documented cost fallback.
    model: str = os.environ.get("TRIAGE_MODEL", "claude-opus-5")
    effort: str = os.environ.get("TRIAGE_EFFORT", "high")  # low|medium|high|xhigh|max

    # Guardrails: the loop stops and routes to a human if either is exceeded.
    max_steps: int = int(os.environ.get("TRIAGE_MAX_STEPS", "6"))
    max_cost_usd: float = float(os.environ.get("TRIAGE_MAX_COST_USD", "0.50"))
    max_latency_ms: float = float(os.environ.get("TRIAGE_MAX_LATENCY_MS", "60000"))
    max_tokens: int = int(os.environ.get("TRIAGE_MAX_TOKENS", "4000"))

    # Policy: the minimum model confidence required to auto-close a LOW-risk
    # alert. Everything below this threshold goes to a human. Deliberately high
    # - a false auto-close (missing real crime) is the costliest error.
    auto_close_min_confidence: float = float(
        os.environ.get("TRIAGE_AUTO_CLOSE_MIN_CONFIDENCE", "0.80")
    )

    data_dir: Path = field(default_factory=_default_data_dir)
    seed: int = int(os.environ.get("TRIAGE_SEED", "42"))

    def price_of(self, model: str) -> dict[str, float]:
        return PRICING.get(model, PRICING["mock"])


# High-risk jurisdictions used by the synthetic generator and the mock brain.
# Illustrative only - a real system would source this from a maintained list.
HIGH_RISK_COUNTRIES: set[str] = {"IR", "KP", "SY", "RU", "MM"}
