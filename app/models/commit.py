from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.db.base import Base


class Commit(Base):
    __tablename__ = "commits"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    file_id = Column(Integer, ForeignKey("files.id"), nullable=False)
    parent_commit_id = Column(Integer, ForeignKey("commits.id"), nullable=True)
    version_label = Column(String(255), nullable=True)
    branch_name = Column(String(64), nullable=True)
    assignee_name = Column(String(255), nullable=True)
    assignee_department = Column(String(255), nullable=True)
    change_notes = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="PENDING", index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    settings = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    progress_message = Column(String(255), nullable=True)
    class_pre = Column(String(32), nullable=True)
    class_major = Column(String(64), nullable=True)
    class_mid = Column(String(32), nullable=True)
    class_minor = Column(String(64), nullable=True)
    class_work_type = Column(String(64), nullable=True)

    file_ref = relationship("File", foreign_keys=[file_id], lazy="joined")

    @property
    def original_filename(self) -> str | None:
        return self.file_ref.original_filename if self.file_ref else None
