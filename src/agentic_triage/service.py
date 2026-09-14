"""Optional FastAPI surface. Import is lazy so the core package (and the tests
and eval harness) do not require FastAPI to be installed.

Run:  uvicorn agentic_triage.service:app --reload
(honours TRIAGE_* env vars; set TRIAGE_LIVE=1 to use the real model.)
"""

from __future__ import annotations

import os


def create_app():  # noqa: ANN201 - FastAPI type only available when installed
    from fastapi import FastAPI

    from .pipeline import build_engine
    from .schemas import Alert, TriageResult

    engine = build_engine(live=os.environ.get("TRIAGE_LIVE") == "1")
    app = FastAPI(title="agentic-alert-triage", version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "model": engine.llm.model}

    @app.post("/triage", response_model=TriageResult)
    def triage(alert: Alert) -> TriageResult:
        return engine.triage(alert)

    return app


# Uvicorn entrypoint. Guarded so `import agentic_triage.service` never fails
# just because FastAPI is absent (e.g. in the test environment).
try:  # pragma: no cover
    app = create_app()
except Exception:  # pragma: no cover
    app = None
