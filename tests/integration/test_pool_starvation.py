"""Under more concurrent requests than threads or pooled connections, the gateway must
queue and serve them all, not deadlock.

FastAPI runs synchronous dependencies and endpoints on a fixed thread pool. A request that
holds a pooled database connection while it waits for a thread (between the identity
dependency and the endpoint, or for a sync dependency's cleanup) can deadlock against requests
holding threads while waiting for connections, until the pool's 30 s timeout. Load testing
found three such paths (docs/PERFORMANCE.md); this pins them shut with a deliberately tiny pool
and thread budget. Postgres only: SQLite doesn't use the bounded pool.
"""

import os
import socket
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg
import pytest

ADMIN_URL = os.environ.get("TEST_POSTGRES_ADMIN_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="TEST_POSTGRES_ADMIN_URL not set")
AGENT = {"x-workload-identity": "mock-support-agent@dev-workload-pool"}


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def gateway():
    db = f"ag_starve_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(f"create database {db}")
    base = ADMIN_URL.rsplit("/", 1)[0].replace("postgresql://", "postgresql+psycopg://")
    port = _port()
    env = {**os.environ, "DATABASE_URL": f"{base}/{db}", "TARGET_BACKEND": "inprocess", "BROKER_URL": "",
           "DB_POOL_SIZE": "1", "DB_MAX_OVERFLOW": "0", "REQUEST_THREADS": "3"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "agentguard.gateway.app:app", "--port", str(port),
                             "--log-level", "warning"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(url + "/readyz", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        admin.execute(f"drop database if exists {db} with (force)")
        admin.close()


def test_more_requests_than_threads_and_connections_all_complete(gateway):
    def call(_):
        return httpx.post(gateway + "/v1/tool-calls", headers=AGENT, timeout=25, json={
            "tool_name": "read-ticket", "environment": "dev", "resource_tenant_id": "tenant-a",
            "target_resource": "ticket:T-1001", "args": {"ticket_id": "T-1001"}, "idempotency_key": str(uuid.uuid4()),
        }).status_code

    started = time.monotonic()
    with ThreadPoolExecutor(12) as pool:
        statuses = list(pool.map(call, range(36)))
    elapsed = time.monotonic() - started
    assert statuses == [200] * 36
    assert elapsed < 20  # a deadlock only clears at the 30 s pool timeout, and then with errors
