from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ScheduleExtractRequest(BaseModel):
    category: str = Field(..., description="wall | beam | column | slab")
    config: dict[str, Any] | None = None
    include_geometry_hints: bool = False
    geometry_radius: float = 800.0


class ScheduleRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    commit_id: int
    category: str
    rules_version: str | None = None
    config_snapshot: dict[str, Any] | None = None
    rows: list[dict[str, Any]]
    validation: dict[str, Any] | None = None
    geometry_hints: dict[str, Any] | None = None
    created_at: datetime | None = None
