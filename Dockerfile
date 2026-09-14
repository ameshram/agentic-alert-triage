FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
COPY eval ./eval

# Install with the API + live extras so the container can serve the real model.
RUN pip install ".[api,live]"

# Generate the synthetic dataset the service loads at startup.
RUN python scripts/generate_synthetic_data.py --out data/synthetic --seed 42

EXPOSE 8000
# Runs with the mock model unless TRIAGE_LIVE=1 and ANTHROPIC_API_KEY are set.
CMD ["uvicorn", "agentic_triage.service:app", "--host", "0.0.0.0", "--port", "8000"]
