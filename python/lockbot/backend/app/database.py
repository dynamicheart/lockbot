"""
Database connection and session management.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from lockbot.backend.app.config import DATABASE_URL
from lockbot.backend.app.sqlite_paths import canonical_sqlite_url

engine = create_engine(canonical_sqlite_url(DATABASE_URL), connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency: yield a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
