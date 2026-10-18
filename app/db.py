from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import database_url

_engine = None
_SessionLocal = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(database_url(), future=True)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_session() -> Session:
    get_engine()
    return _SessionLocal()


def get_db():
    """FastAPI dependency: one session per request, always closed."""
    s = get_session()
    try:
        yield s
    finally:
        s.close()
