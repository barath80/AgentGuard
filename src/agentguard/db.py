from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from agentguard.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)

if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _sqlite_wal(dbapi_conn, _record):
        # Dev/test only: write-ahead logging lets console reads proceed while agents' calls write,
        # instead of queueing behind every commit. (Production runs on Postgres.)
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_session() -> Iterator[Session]:
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
