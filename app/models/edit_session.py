from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Text

from app.db.base import Base


class EditSession(Base):
    """버전(커밋) 편집·검토 세션 — CAD Manage 스타일 추적."""

    __tablename__ = "edit_sessions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    commit_id = Column(Integer, ForeignKey("commits.id"), nullable=False, index=True)
    editor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)
