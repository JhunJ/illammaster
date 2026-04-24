from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.db.session import get_db
from app.models import Commit, Entity, Project
from app.schemas.commit import CommitResponse, CommitUpdate

router = APIRouter(tags=["commits"])


@router.get("/commits/classification-summary")
def commits_classification_summary(db: Session = Depends(get_db)):
    """분류 트리용: 모든 커밋 + 프로젝트 식별 정보(태그는 commit.settings.sub_tags)."""
    rows = (
        db.query(Commit, Project.name, Project.code)
        .join(Project, Commit.project_id == Project.id)
        .order_by(Project.id.asc(), Commit.id.asc())
        .all()
    )
    return [
        {
            "commit_id": c.id,
            "project_id": c.project_id,
            "project_name": pname,
            "project_code": pcode,
            "version_label": c.version_label,
            "settings": c.settings,
        }
        for c, pname, pcode in rows
    ]


@router.patch("/commits/{commit_id}", response_model=CommitResponse)
def update_commit(commit_id: int, payload: CommitUpdate, db: Session = Depends(get_db)):
    c = db.query(Commit).filter(Commit.id == commit_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Commit not found")
    if payload.settings is not None:
        cur = dict(c.settings) if isinstance(c.settings, dict) else {}
        for k, v in payload.settings.items():
            if k == "sub_tags" and isinstance(v, dict) and isinstance(cur.get("sub_tags"), dict):
                merged = dict(cur["sub_tags"])
                merged.update(v)
                cur["sub_tags"] = merged
            else:
                cur[k] = v
        c.settings = cur
    db.commit()
    db.refresh(c)
    return c


@router.get("/commits/{commit_id}", response_model=CommitResponse)
def get_commit(commit_id: int, db: Session = Depends(get_db)):
    c = db.query(Commit).filter(Commit.id == commit_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Commit not found")
    return c


@router.get("/commits/{commit_id}/diagnostics")
def commit_diagnostics(commit_id: int, db: Session = Depends(get_db)):
    """업로드·파싱 상태와 DB 엔티티 개수(뷰어 미표시 원인 확인용)."""
    c = (
        db.query(Commit)
        .options(joinedload(Commit.file_ref))
        .filter(Commit.id == commit_id)
        .first()
    )
    if not c:
        raise HTTPException(status_code=404, detail="Commit not found")

    entity_total = (
        db.query(func.count(Entity.id)).filter(Entity.commit_id == commit_id).scalar() or 0
    )
    entity_with_geom = (
        db.query(func.count(Entity.id))
        .filter(Entity.commit_id == commit_id, Entity.geom.isnot(None))
        .scalar()
        or 0
    )
    rows = (
        db.query(Entity.entity_type, func.count(Entity.id))
        .filter(Entity.commit_id == commit_id)
        .group_by(Entity.entity_type)
        .all()
    )
    by_type = {str(t): int(n) for t, n in rows}
    fname = None
    if c.file_ref:
        fname = c.file_ref.original_filename

    return {
        "commit_id": commit_id,
        "status": c.status,
        "progress_message": c.progress_message,
        "error_message": c.error_message,
        "version_label": c.version_label,
        "original_filename": fname,
        "entity_total": entity_total,
        "entity_with_geom": entity_with_geom,
        "entity_by_type": by_type,
    }


@router.get("/projects/{project_id}/commits", response_model=list[CommitResponse])
def list_commits(project_id: int, db: Session = Depends(get_db)):
    return (
        db.query(Commit)
        .options(joinedload(Commit.file_ref))
        .filter(Commit.project_id == project_id)
        .order_by(Commit.created_at.asc(), Commit.id.asc())
        .all()
    )
