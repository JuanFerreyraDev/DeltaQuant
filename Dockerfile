# ── DeltaQuant — multi-stage Dockerfile (stub) ───────────────────────────────
# Fully implemented in Phase 5 (chore/f5-dockerfile-multistage).
# This stub keeps the directory structure consistent with §4 of the tech plan.

FROM python:3.11-slim AS base

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
