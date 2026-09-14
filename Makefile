.PHONY: setup data test lint eval eval-live run clean quickstart

setup:  ## install the package with dev extras
	pip install -e ".[dev]"

data:  ## generate the synthetic labeled dataset
	python scripts/generate_synthetic_data.py --out data/synthetic --seed 42

test:  ## run unit tests (offline, no API key)
	pytest

lint:  ## static checks
	ruff check .

eval: data  ## run the offline eval with regression gates
	python -m eval.run_eval --data data/synthetic --gate

eval-live: data  ## run the eval against the real model (needs ANTHROPIC_API_KEY and .[live])
	python -m eval.run_eval --data data/synthetic --live --judge claude

run:  ## start the HTTP service (needs .[api])
	uvicorn agentic_triage.service:app --reload

quickstart: setup data test eval  ## everything a reviewer needs, in one command

clean:
	rm -rf data/synthetic eval/report.md eval/report.json .pytest_cache
