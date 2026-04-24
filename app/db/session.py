"""DB 엔진 및 세션."""
from app.config import get_settings
from app.db.base import Base
from app.db.pg_connect import PG_CONNECT_KWARGS, normalize_sync_postgresql_url
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_settings = get_settings()
engine = create_engine(
    normalize_sync_postgresql_url(_settings.database_url),
    pool_pre_ping=True,
    echo=False,
    connect_args=PG_CONNECT_KWARGS,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
