from sqlalchemy.orm import declarative_base

try:
    from migration.config import PRIMARY, get_db_path
    from migration.db_factory import get_engine, get_sessionmaker
except ImportError:
    try:
        from migration.config import PRIMARY, get_db_path
        from migration.db_factory import get_engine, get_sessionmaker
    except ImportError:
        from migration.config import PRIMARY, get_db_path
        from migration.db_factory import get_engine, get_sessionmaker

DB_PATH = get_db_path(PRIMARY)
engine = get_engine(PRIMARY)
SessionLocal = get_sessionmaker(PRIMARY, autoflush=False, autocommit=False, future=True)

Base = declarative_base()