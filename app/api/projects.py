import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Project
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectUpdate
from app.services.project_delete import delete_project_cascade

router = APIRouter(tags=["projects"])


def _generate_project_code(db: Session) -> str:
    """CADManage 스타일: 날짜 + 짧은 무작위 접미사로 고유 코드 생성."""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    for _ in range(64):
        suf = secrets.token_hex(3).upper()
        code = f"ILLAM-{day}-{suf}"
        if not db.query(Project).filter(Project.code == code).first():
            return code
    return f"ILLAM-{day}-{secrets.token_hex(4).upper()}"


@router.post("/projects", response_model=ProjectResponse)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    raw = (payload.code or "").strip()
    code = raw if raw else _generate_project_code(db)
    if db.query(Project).filter(Project.code == code).first():
        raise HTTPException(status_code=400, detail="Project code already exists")
    p = Project(name=payload.name.strip(), code=code, created_by=payload.created_by)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@router.get("/projects", response_model=list[ProjectResponse])
def list_projects(db: Session = Depends(get_db)):
    return db.query(Project).order_by(Project.created_at.desc(), Project.id.desc()).all()


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: int, db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return p


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
def update_project(project_id: int, payload: ProjectUpdate, db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Project name cannot be empty")
        p.name = name
    if payload.settings is not None:
        cur = dict(p.settings) if isinstance(p.settings, dict) else {}
        for k, v in payload.settings.items():
            if k == "sub_tags" and isinstance(v, dict) and isinstance(cur.get("sub_tags"), dict):
                merged = dict(cur["sub_tags"])
                merged.update(v)
                cur["sub_tags"] = merged
            else:
                cur[k] = v
        p.settings = cur
    db.commit()
    db.refresh(p)
    return p


@router.delete("/projects/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db)):
    ok = delete_project_cascade(db, project_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Project not found")
    db.commit()
    return {"ok": True}
