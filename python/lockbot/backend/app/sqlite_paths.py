"""One SQLite file interpretation for engine, locks, inventory and backup."""

from pathlib import Path

from sqlalchemy.engine import make_url


def sqlite_database_file(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise ValueError("This deployment requires SQLite database ownership")
    if url.query.get("uri") or (url.database and url.database.startswith("file:")):
        raise ValueError("SQLite URI database URLs are not supported; use a normal sqlite:/// file URL")
    if not url.database or url.database == ":memory:":
        return None
    # SQLite/SQLAlchemy do not expand '~' in database filenames.
    return Path(url.database).resolve()


def canonical_sqlite_url(database_url: str) -> str:
    database = sqlite_database_file(database_url)
    if database is None:
        return database_url
    return make_url(database_url).set(database=str(database)).render_as_string(hide_password=False)
