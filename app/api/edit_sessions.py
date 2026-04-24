from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Commit, EditSession, Project
from app.schemas.edit_session import EditSessionCreate, EditSessionEnd, EditSessionResponse

router = APIRouter(tags=["edit_sessions"])


@router.get("/edit-sessions", response_model=list[EditSessionResponse])
def list_edit_sessions(
    project_id: int | None = None,
    active_only: bool = False,
    db: Session = Depends(get_db),
):
    q = db.query(EditSession).order_by(EditSession.started_at.desc(), EditSession.id.desc())
    if project_id is not None:
        q = q.filter(EditSession.project_id == project_id)
    if active_only:
        q = q.filter(EditSession.ended_at.is_(None))
    return q.all()


@router.post("/edit-sessions", response_model=EditSessionResponse)
def start_edit_session(payload: EditSessionCreate, db: Session = Depends(get_db)):
    proj = db.query(Project).filter(Project.id == payload.project_id).first()
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    c = db.query(Commit).filter(Commit.id == payload.commit_id).first()
    if not c or c.project_id != payload.project_id:
        raise HTTPException(status_code=400, detail="Commit does not belong to project")
    row = EditSession(
        project_id=payload.project_id,
        commit_id=payload.commit_id,
        editor_user_id=payload.editor_user_id,
        notes=(payload.notes or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch("/edit-sessions/{session_id}/end", response_model=EditSessionResponse)
def end_edit_session(session_id: int, payload: EditSessionEnd | None = None, db: Session = Depends(get_db)):
    row = db.query(EditSession).filter(EditSession.id == session_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    if row.ended_at is not None:
        raise HTTPException(status_code=400, detail="Session already ended")
    row.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if payload and payload.notes:
        extra = payload.notes.strip()
        if extra:
            row.notes = (row.notes or "") + ("\n" if row.notes else "") + extra
    db.commit()
    db.refresh(row)
    return row


@router.delete("/edit-sessions/{session_id}")
def delete_edit_session(session_id: int, db: Session = Depends(get_db)):
    row = db.query(EditSession).filter(EditSession.id == session_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    db.delete(row)
    db.commit()
    return {"ok": True}
