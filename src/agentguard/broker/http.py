"""One pooled HTTP client per downstream service, per process.

Building an httpx client is expensive (a TLS context costs hundreds of milliseconds of CPU on
some platforms), so it's done once and the client is shared by every thread; httpx clients
are thread-safe. Creation is locked: with an unlocked cache, a burst of concurrent first
requests all miss at once and each builds its own client, serialised on the GIL. Load testing
measured 64 threads stuck there for ~15 s, long enough to trip broker deadlines and set off a
reconciliation storm (docs/PERFORMANCE.md). Credentials are never set on a shared client;
callers send them per request.
"""

import threading

import httpx

_clients: dict[str, httpx.Client] = {}
_lock = threading.Lock()


def pooled_client(base_url: str, **kwargs) -> httpx.Client:
    client = _clients.get(base_url)
    if client is None:
        with _lock:
            client = _clients.get(base_url)
            if client is None:
                client = _clients[base_url] = httpx.Client(base_url=base_url, **kwargs)
    return client
