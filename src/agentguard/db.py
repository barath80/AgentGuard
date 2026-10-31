from collections.abc import AsyncIterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from agentguard.config import settings

if settings.database_url.startswith("sqlite"):
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
else:
    engine = create_engine(
        settings.database_url, pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,  # survive a database restart or failover without serving dead connections
    )

if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _sqlite_wal(dbapi_conn, _record):
        # Dev/test only: write-ahead logging lets console reads proceed while agents' calls write,
        # instead of queueing behind every commit. (Production runs on Postgres.)
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

# expire_on_commit=False: after a commit, reading an object must not silently start a new
# transaction. If it does, the request checks a pooled connection out again and keeps it until
# the session closes in FastAPI's dependency cleanup, which runs on the same thread pool as the
# requests. Under load every thread then waits for a connection only cleanup could release:
# a deadlock until the pool timeout (found by load testing; docs/PERFORMANCE.md).
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncIterator[Session]:
    """One session per request. Async on purpose, though the session is synchronous: FastAPI runs
    a sync generator dependency's cleanup on the request thread pool, so under load the close
    that would return a connection can wait behind requests that are themselves waiting for a
    connection, a deadlock until the pool timeout (found by load testing). Async cleanup runs on
    the event loop instead, and closing an already-committed session does no I/O."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def read_only_engine(database_url: str) -> Engine:
    """An engine whose connections the database itself refuses to write through."""
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def _enforce_read_only(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        if engine.dialect.name == "sqlite":
            cursor.execute("PRAGMA query_only = ON")
        elif engine.dialect.name == "postgresql":
            cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        cursor.close()

    return engine


def read_only_session(database_url: str) -> Session:
    return sessionmaker(bind=read_only_engine(database_url))()


def close_session(session: Session) -> None:
    engine = session.get_bind()
    session.close()
    engine.dispose()
