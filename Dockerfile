FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir -e .

CMD ["sh", "-c", "alembic upgrade head && uvicorn agentguard.gateway.app:app --host 0.0.0.0 --port 8000"]
