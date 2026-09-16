"""Per-case tracing and cost accounting.

Every alert produces a trace: each tool call and each model call, with latency
and (for model calls) token counts and dollar cost. This is what makes the
system operable - you can answer "what did this decision cost?" and "where did
the latency go?" per case, and aggregate across a run in the eval harness.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import PRICING


def token_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price = PRICING.get(model, PRICING["mock"])
    return (
        input_tokens / 1_000_000 * price["input"]
        + output_tokens / 1_000_000 * price["output"]
    )


@dataclass
class Tracer:
    """Collects spans for one alert. Cheap, in-memory, JSON-serializable."""

    events: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    _t0: float = field(default_factory=time.perf_counter)

    def record_llm(
        self, model: str, input_tokens: int, output_tokens: int, latency_ms: float
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        cost = token_cost(model, input_tokens, output_tokens)
        self.cost_usd += cost
        self.events.append(
            {
                "kind": "llm",
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": round(latency_ms, 1),
                "cost_usd": round(cost, 6),
            }
        )

    def record_tool(self, name: str, latency_ms: float, ok: bool = True) -> None:
        self.events.append(
            {
                "kind": "tool",
                "name": name,
                "latency_ms": round(latency_ms, 1),
                "ok": ok,
            }
        )

    @property
    def latency_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000.0


class span:
    """Context manager that returns elapsed milliseconds via ``.ms``."""

    def __init__(self) -> None:
        self.ms: float = 0.0
        self._t0 = 0.0

    def __enter__(self) -> span:
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = (time.perf_counter() - self._t0) * 1000.0
