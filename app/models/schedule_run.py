from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class ScheduleRun(Base):
    """일람 추출 실행 결과 (커밋·카테고리별)."""

    __tablename__ = "schedule_runs"

    id = Column(Integer, primary_key=True, index=True)
    commit_id = Column(Integer, ForeignKey("commits.id"), nullable=False, index=True)
    category = Column(String(32), nullable=False, index=True)
    rules_version = Column(String(32), nullable=True)
    config_snapshot = Column(JSONB, nullable=True)
    rows = Column(JSONB, nullable=False)
    validation = Column(JSONB, nullable=True)
    geometry_hints = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
