import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agentguard import models  # noqa: F401 - registers tables on Base.metadata
from agentguard.db import Base
from agentguard.policy.seed import seed


@pytest.fixture()
def session(tmp_path) -> Session:
    """A fresh SQLite-backed, seeded session per test — fully isolated
    from the dev `agentguard.db` file and from other tests.
    """
    db_path = tmp_path / f"test-{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    db_session = session_factory()
    seed(db_session)
    try:
        yield db_session
    finally:
        db_session.close()
        engine.dispose()
