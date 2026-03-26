# ── Stage 1: base ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

WORKDIR /app
ENV PYTHONPATH=/app

# System dependencies for scientific Python + Polygon crypto libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev curl git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ── Stage 2: jupyter (adds JupyterLab) ────────────────────────────────────────
FROM base AS jupyter
RUN pip install --no-cache-dir jupyterlab
EXPOSE 8888
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--no-browser", "--allow-root", \
     "--NotebookApp.token=${JUPYTER_TOKEN}"]

# ── Stage 3: scheduler (APScheduler process) ──────────────────────────────────
FROM base AS scheduler
CMD ["python", "-m", "live.scheduler"]

# ── Stage 4: dashboard (FastAPI health/status UI on port 8080) ────────────────
FROM base AS dashboard
EXPOSE 8080
CMD ["uvicorn", "live.dashboard:app", "--host", "0.0.0.0", "--port", "8080"]

# ── Default target: bot ───────────────────────────────────────────────────────
FROM base AS bot
CMD ["python", "-m", "live.executor", "--mode", "paper"]
