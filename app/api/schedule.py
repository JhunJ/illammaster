from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from geoalchemy2.shape import to_shape
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Commit, Entity, ScheduleRun
from app.schemas.schedule import ScheduleExtractRequest, ScheduleRunResponse
from app.services.schedule_extraction import (
    SCHEDULE_CATEGORIES,
    TEXT_ENTITY_TYPES_FOR_EXTRACT,
    extract_schedule,
    load_near_geometry,
)

router = APIRouter(tags=["schedule"])


def _first_text_xy(db: Session, commit_id: int) -> tuple[float, float]:
    ent = (
        db.query(Entity)
        .filter(
            Entity.commit_id == commit_id,
            Entity.entity_type.in_(TEXT_ENTITY_TYPES_FOR_EXTRACT),
        )
        .first()
    )
    if not ent or ent.geom is None:
        return 0.0, 0.0
    try:
        shp = to_shape(ent.geom)
        if shp.geom_type == "Point":
            return float(shp.x), float(shp.y)
        c = shp.centroid
        return float(c.x), float(c.y)
    except Exception:
        return 0.0, 0.0


@router.post("/commits/{commit_id}/extract", response_model=ScheduleRunResponse)
def run_extract(
    commit_id: int,
    body: ScheduleExtractRequest,
    db: Session = Depends(get_db),
):
    c = db.query(Commit).filter(Commit.id == commit_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Commit not found")
    if c.status != "READY":
        raise HTTPException(
            status_code=400,
            detail=f"Commit not ready (status={c.status}). Wait for parsing to finish.",
        )
    cat = body.category.strip().lower()
    if cat not in SCHEDULE_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of {list(SCHEDULE_CATEGORIES)}",
        )

    cfg = body.config or {}
    rows, validation = extract_schedule(db, commit_id, cat, cfg)

    geom_hints: dict[str, Any] | None = None
    if body.include_geometry_hints:
        center = _first_text_xy(db, commit_id)
        hints_list = load_near_geometry(db, commit_id, center, body.geometry_radius)
        geom_hints = {"sample_center": list(center), "near": hints_list[:50]}

    rules_ver = (cfg.get("rules_version") if isinstance(cfg, dict) else None) or "1.0"
    run = ScheduleRun(
        commit_id=commit_id,
        category=cat,
        rules_version=str(rules_ver),
        config_snapshot=cfg,
        rows=rows,
        validation=validation,
        geometry_hints=geom_hints,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


@router.get("/commits/{commit_id}/runs", response_model=list[ScheduleRunResponse])
def list_runs(commit_id: int, db: Session = Depends(get_db)):
    return (
        db.query(ScheduleRun)
        .filter(ScheduleRun.commit_id == commit_id)
        .order_by(ScheduleRun.id.desc())
        .all()
    )


@router.get("/commits/{commit_id}/runs/{run_id}", response_model=ScheduleRunResponse)
def get_run(commit_id: int, run_id: int, db: Session = Depends(get_db)):
    r = (
        db.query(ScheduleRun)
        .filter(ScheduleRun.commit_id == commit_id, ScheduleRun.id == run_id)
        .first()
    )
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    return r
