FROM python:3.12-slim

# One image, several roles (gateway, broker, helpdesk, migrations, model training);
# the command picks the role. Runs as an unprivileged user with a read-only-friendly layout.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY policies ./policies

RUN pip install ".[ml]" \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agentguard

USER 10001

# Readiness, not just liveness: compose holds the console back until the database is reachable.
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/readyz')"

# Compose runs migrations on start for convenience; Kubernetes runs them in a pre-upgrade Job instead.
CMD ["sh", "-c", "alembic upgrade head && uvicorn agentguard.gateway.app:app --host 0.0.0.0 --port 8000"]
