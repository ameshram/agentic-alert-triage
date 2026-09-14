"""Investigation tools and the synthetic data store they read from.

Each tool returns a JSON-serializable dict. Any free text that originates from
a third party (transaction memos, counterparty names, prior-case summaries) is
passed through security.wrap_untrusted before it reaches the model, and any
injection flags are surfaced under the private "_flags" key so the agent loop
can force human review.

The `submit_assessment` tool is the terminal action: when the model calls it,
the investigation is complete and the loop parses its input as a RiskAssessment.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .retrieval import VectorStore
from .schemas import Entity, LabeledAlert, RiskLevel, Transaction
from .security import scan_untrusted, wrap_untrusted

_MAX_TXNS_RETURNED = 25


class SyntheticStore:
    """Holds the synthetic world: entities, transactions, watchlist, cases."""

    def __init__(
        self,
        entities: dict[str, Entity],
        transactions: list[Transaction],
        watchlist: list[dict],
        prior_cases: list[dict],
    ) -> None:
        self.entities = entities
        self._txns_by_account: dict[str, list[Transaction]] = {}
        for t in transactions:
            self._txns_by_account.setdefault(t.account_id, []).append(t)
        self.watchlist = watchlist
        self._watch_names = {w["name"].lower() for w in watchlist}
        self._watch_entity_ids = {w.get("entity_id") for w in watchlist if w.get("entity_id")}
        self.prior_cases = VectorStore()
        for c in prior_cases:
            self.prior_cases.add(c["id"], c["summary"], {"disposition": c.get("disposition", "unknown")})

    # ---- constructors -------------------------------------------------
    @classmethod
    def from_dir(cls, path: str | Path) -> SyntheticStore:
        p = Path(path)
        entities = {
            e["id"]: Entity(**e) for e in _load_json(p / "entities.json")
        }
        transactions = [Transaction(**t) for t in _load_json(p / "transactions.json")]
        watchlist = _load_json(p / "watchlist.json")
        prior_cases = _load_json(p / "prior_cases.json")
        return cls(entities, transactions, watchlist, prior_cases)

    # ---- queries used by tools ---------------------------------------
    def transactions_for(self, account_id: str, lookback_days: int) -> list[Transaction]:
        txns = self._txns_by_account.get(account_id, [])
        if not txns:
            return []
        cutoff = max(t.ts for t in txns) - timedelta(days=lookback_days)
        return sorted(
            [t for t in txns if t.ts >= cutoff], key=lambda t: t.ts, reverse=True
        )

    def watchlist_hit(self, name: str = "", entity_id: str = "") -> list[dict]:
        hits = []
        for w in self.watchlist:
            if entity_id and w.get("entity_id") == entity_id:
                hits.append(w)
            elif name and w["name"].lower() == name.lower():
                hits.append(w)
        return hits


def _load_json(path: Path) -> list:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return json.loads(path.read_text())


def load_labeled_alerts(path: str | Path) -> list[LabeledAlert]:
    rows = _load_json(Path(path) / "labeled_alerts.jsonl")
    return [LabeledAlert(**r) for r in rows]


# --------------------------------------------------------------------------
# Tool implementations. Signature: fn(store, **input) -> dict
# --------------------------------------------------------------------------
def _tool_get_transaction_history(store: SyntheticStore, account_id: str, lookback_days: int = 90) -> dict:
    txns = store.transactions_for(account_id, lookback_days)
    flags: list[str] = []
    rows = []
    for t in txns[:_MAX_TXNS_RETURNED]:
        _, memo_flags = scan_untrusted(t.memo)
        _, name_flags = scan_untrusted(t.counterparty_name)
        flags.extend(memo_flags + name_flags)
        rows.append(
            {
                "ts": t.ts.isoformat(),
                "amount": t.amount,
                "direction": t.direction,
                "channel": t.channel,
                "country": t.country,
                "counterparty": wrap_untrusted("counterparty_name", t.counterparty_name),
                "memo": wrap_untrusted("transaction_memo", t.memo),
            }
        )
    amounts = [t.amount for t in txns]
    return {
        "count": len(txns),
        "max_amount": max(amounts) if amounts else 0.0,
        "total_debits": round(sum(t.amount for t in txns if t.direction == "debit"), 2),
        "total_credits": round(sum(t.amount for t in txns if t.direction == "credit"), 2),
        "countries": sorted({t.country for t in txns}),
        "channels": sorted({t.channel for t in txns}),
        "transactions": rows,
        "_flags": sorted(set(flags)),
    }


def _tool_get_entity_profile(store: SyntheticStore, entity_id: str) -> dict:
    e = store.entities.get(entity_id)
    if e is None:
        return {"found": False, "entity_id": entity_id}
    return {
        "found": True,
        "id": e.id,
        "name": e.name,
        "kind": e.kind,
        "country": e.country,
        "risk_flags": e.risk_flags,
    }


def _tool_check_watchlist(store: SyntheticStore, name: str = "", entity_id: str = "") -> dict:
    hits = store.watchlist_hit(name=name, entity_id=entity_id)
    return {
        "hit": bool(hits),
        "matches": [{"list": h.get("list_name", "sanctions"), "name": h["name"]} for h in hits],
    }


def _tool_search_prior_cases(store: SyntheticStore, query: str, k: int = 3) -> dict:
    results = store.prior_cases.search(query, k=k)
    flags: list[str] = []
    out = []
    for score, rec in results:
        _, f = scan_untrusted(rec.text)
        flags.extend(f)
        out.append(
            {
                "score": round(score, 3),
                "disposition": rec.metadata.get("disposition", "unknown"),
                "summary": wrap_untrusted("prior_case", rec.text),
            }
        )
    return {"results": out, "_flags": sorted(set(flags))}


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., dict]
    strict: bool = False


def build_tools() -> dict[str, Tool]:
    return {
        "get_transaction_history": Tool(
            name="get_transaction_history",
            description=(
                "Return recent transactions for an account with aggregates "
                "(counts, totals, countries, channels). Memos and counterparty "
                "names are third-party data wrapped in <untrusted> tags."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "lookback_days": {"type": "integer", "description": "How far back to look (default 90)."},
                },
                "required": ["account_id"],
            },
            fn=_tool_get_transaction_history,
        ),
        "get_entity_profile": Tool(
            name="get_entity_profile",
            description="Return the KYC profile for the subject entity (kind, country, and any standing risk flags).",
            input_schema={
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
            fn=_tool_get_entity_profile,
        ),
        "check_watchlist": Tool(
            name="check_watchlist",
            description="Check the subject against sanctions / PEP watchlists by entity_id and/or name.",
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": [],
            },
            fn=_tool_check_watchlist,
        ),
        "search_prior_cases": Tool(
            name="search_prior_cases",
            description=(
                "Semantic search over previously investigated cases and their "
                "dispositions. Use it to find precedent for the current pattern."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer"},
                },
                "required": ["query"],
            },
            fn=_tool_search_prior_cases,
        ),
    }


# Terminal tool: the model calls this to submit its final assessment. strict=True
# guarantees the arguments validate against the RiskAssessment shape.
SUBMIT_ASSESSMENT = Tool(
    name="submit_assessment",
    description="Submit the final risk assessment for this alert. Call this exactly once, after investigating.",
    input_schema={
        "type": "object",
        "properties": {
            "risk_level": {"type": "string", "enum": [r.value for r in RiskLevel]},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "narrative": {"type": "string"},
            "key_findings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["risk_level", "confidence", "narrative", "key_findings"],
        "additionalProperties": False,
    },
    fn=lambda **_: {},  # never executed; it is the terminal signal
    strict=True,
)


def anthropic_tool_specs(tools: dict[str, Tool]) -> list[dict]:
    """Render tools (plus submit_assessment) as Anthropic tool definitions."""
    specs = []
    for t in list(tools.values()) + [SUBMIT_ASSESSMENT]:
        spec: dict = {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        if t.strict:
            spec["strict"] = True
        specs.append(spec)
    return specs
