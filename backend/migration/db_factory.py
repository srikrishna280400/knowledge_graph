#db_factory.py

from __future__ import annotations
import os
import re
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker, declarative_base
from sqlalchemy.pool import NullPool


try:
    from .config import PRIMARY, get_database_url, get_db_path
except ImportError:
    from config import PRIMARY, get_database_url, get_db_path


def normalize_pg_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://") and not url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url

def get_primary_mirror_database_url() -> str:
    return (
        os.getenv("KG_PRIMARY_MIRROR_DATABASE_URL", "").strip()
        or os.getenv("KG_PRIMARY_DATABASE_URL", "").strip()
        or os.getenv("DATABASE_URL", "").strip()
    )

def get_primary_mirror_engine(
    *,
    future: bool = True,
    echo: bool = False,
) -> Engine | None:
    database_url = normalize_pg_url(get_primary_mirror_database_url())
    if not database_url:
        return None

    cache_key = f"primary_mirror|future={int(future)}|echo={int(echo)}"
    engine = _ENGINE_CACHE.get(cache_key)
    if engine is not None:
        return engine

    engine = create_engine(
        database_url,
        future=future,
        echo=echo,
        pool_pre_ping=True,
        poolclass=NullPool,
        connect_args={"prepare_threshold": None},
        )
    _ENGINE_CACHE[cache_key] = engine
    return engine


def get_primary_mirror_sessionmaker(
    *,
    autoflush: bool = False,
    autocommit: bool = False,
    future: bool = True,
    echo: bool = False,
) -> sessionmaker | None:
    engine = get_primary_mirror_engine(future=future, echo=echo)
    if engine is None:
        return None

    cache_key = (
        f"primary_mirror|future={int(future)}|echo={int(echo)}|"
        f"autoflush={int(autoflush)}|autocommit={int(autocommit)}"
    )
    maker = _SESSIONMAKER_CACHE.get(cache_key)
    if maker is not None:
        return maker

    maker = sessionmaker(
        bind=engine,
        autoflush=autoflush,
        autocommit=autocommit,
        future=future,
    )
    _SESSIONMAKER_CACHE[cache_key] = maker
    return maker

@contextmanager
def primary_mirror_session_scope(
    *,
    autoflush: bool = False,
    autocommit: bool = False,
    future: bool = True,
    echo: bool = False,
):
    maker = get_primary_mirror_sessionmaker(
        autoflush=autoflush,
        autocommit=autocommit,
        future=future,
        echo=echo,
    )
    if maker is None:
        yield None
        return

    session = maker()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


_ENGINE_CACHE: dict[str, Engine] = {}
_SESSIONMAKER_CACHE: dict[str, sessionmaker] = {}
_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def build_sqlite_url(db_path: str | Path) -> str:
    p = Path(db_path).expanduser().resolve()
    return f"sqlite:///{p}"


def is_sqlite_url(database_url: str) -> bool:
    return database_url.startswith("sqlite")


def get_engine(
    target: str = PRIMARY,
    *,
    future: bool = True,
    echo: bool = False,
) -> Engine:
    cache_key = f"{target}|future={int(future)}|echo={int(echo)}"
    engine = _ENGINE_CACHE.get(cache_key)
    if engine is not None:
        return engine

    database_url = get_database_url(target)

    if is_sqlite_url(database_url):
        db_path = get_db_path(target)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            database_url,
            future=future,
            echo=echo,
            pool_pre_ping=True,
        )
    else:
        engine = create_engine(
            database_url,
            future=future,
            echo=echo,
            pool_pre_ping=True,
            poolclass=NullPool,
            connect_args={"prepare_threshold": None},
        )

    _ENGINE_CACHE[cache_key] = engine
    return engine


def get_sessionmaker(
    target: str = PRIMARY,
    *,
    autoflush: bool = False,
    autocommit: bool = False,
    future: bool = True,
    echo: bool = False,
) -> sessionmaker:
    cache_key = (
        f"{target}|future={int(future)}|echo={int(echo)}|"
        f"autoflush={int(autoflush)}|autocommit={int(autocommit)}"
    )

    maker = _SESSIONMAKER_CACHE.get(cache_key)
    if maker is not None:
        return maker

    engine = get_engine(target, future=future, echo=echo)
    maker = sessionmaker(
        bind=engine,
        autoflush=autoflush,
        autocommit=autocommit,
        future=future,
    )
    _SESSIONMAKER_CACHE[cache_key] = maker
    return maker

Base = declarative_base()
engine = get_engine(PRIMARY)
SessionLocal = get_sessionmaker(PRIMARY, autoflush=False, autocommit=False, future=True)

def get_session_factory(
    target: str = PRIMARY,
    *,
    autoflush: bool = False,
    autocommit: bool = False,
    future: bool = True,
    echo: bool = False,
) -> sessionmaker:
    return get_sessionmaker(
        target=target,
        autoflush=autoflush,
        autocommit=autocommit,
        future=future,
        echo=echo,
    )


def new_session(
    target: str = PRIMARY,
    *,
    autoflush: bool = False,
    autocommit: bool = False,
    future: bool = True,
    echo: bool = False,
) -> Session:
    maker = get_sessionmaker(
        target,
        autoflush=autoflush,
        autocommit=autocommit,
        future=future,
        echo=echo,
    )
    return maker()


def newsession(
    target: str = PRIMARY,
    *,
    autoflush: bool = False,
    autocommit: bool = False,
    future: bool = True,
    echo: bool = False,
) -> Session:
    return new_session(
        target=target,
        autoflush=autoflush,
        autocommit=autocommit,
        future=future,
        echo=echo,
    )


def get_db_session(
    target: str = PRIMARY,
    *,
    autoflush: bool = False,
    autocommit: bool = False,
    future: bool = True,
    echo: bool = False,
) -> Session:
    return new_session(
        target=target,
        autoflush=autoflush,
        autocommit=autocommit,
        future=future,
        echo=echo,
    )


@contextmanager
def session_scope(
    target: str = PRIMARY,
    *,
    autoflush: bool = False,
    autocommit: bool = False,
    future: bool = True,
    echo: bool = False,
):
    session = get_db_session(
        target=target,
        autoflush=autoflush,
        autocommit=autocommit,
        future=future,
        echo=echo,
    )
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db_path_str(target: str = PRIMARY) -> str:
    return str(get_db_path(target))


def attach_database(session: Session, target: str, alias: str) -> None:
    if not _ALIAS_RE.match(alias):
        raise ValueError(
            f"Invalid SQLite ATTACH alias '{alias}'. "
            "Use letters, numbers, and underscores only, and do not start with a number."
        )

    database_url = get_database_url(target)
    if not is_sqlite_url(database_url):
        raise RuntimeError("attach_database() is only supported for SQLite targets")

    db_path = get_db_path(target)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    safe_path = str(db_path).replace("'", "''")
    session.execute(text(f"ATTACH DATABASE '{safe_path}' AS {alias}"))


def test_connection(target: str = PRIMARY) -> None:
    engine = get_engine(target)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def dispose_all_engines() -> None:
    for engine in _ENGINE_CACHE.values():
        engine.dispose()
    _ENGINE_CACHE.clear()
    _SESSIONMAKER_CACHE.clear()

