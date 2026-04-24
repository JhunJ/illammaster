from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ProjectUpdate(BaseModel):
    """부분 갱신: name·settings 키는 보낸 값만 반영(settings는 얕은 병합)."""

    name: str | None = None
    settings: dict[str, Any] | None = None


class ProjectCreate(BaseModel):
    name: str
    code: str | None = None
    created_by: int | None = None


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    code: str
    created_by: int | None = None
    created_at: datetime | None = None
    settings: dict[str, Any] | None = None
