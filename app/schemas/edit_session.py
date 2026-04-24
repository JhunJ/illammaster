from datetime import datetime

from pydantic import BaseModel, ConfigDict


class EditSessionCreate(BaseModel):
    project_id: int
    commit_id: int
    editor_user_id: int | None = None
    notes: str | None = None


class EditSessionEnd(BaseModel):
    notes: str | None = None


class EditSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    commit_id: int
    editor_user_id: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    notes: str | None = None
