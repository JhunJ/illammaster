from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class CommitUpdate(BaseModel):
    """부분 갱신: settings만내면 기존 settings와 얕게 병합(sub_tags는 하위 병합)."""

    settings: dict[str, Any] | None = None


class CommitResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    file_id: int
    original_filename: str | None = None
    parent_commit_id: int | None = None
    version_label: str | None = None
    branch_name: str | None = None
    assignee_name: str | None = None
    assignee_department: str | None = None
    change_notes: str | None = None
    status: str
    created_by: int | None = None
    created_at: datetime | None = None
    settings: dict[str, Any] | None = None
    error_message: str | None = None
    progress_message: str | None = None
    class_pre: str | None = None
    class_major: str | None = None
    class_mid: str | None = None
    class_minor: str | None = None
    class_work_type: str | None = None
