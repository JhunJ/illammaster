from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Commit, EditSession, File, Project, User
from app.schemas.user import UserCreate, UserResponse

router = APIRouter(tags=["users"])


@router.post("/users", response_model=UserResponse)
def create_user(payload: UserCreate, db: Session = Depends(get_db)):
    exists = db.query(User).filter(User.email == payload.email.strip()).first()
    if exists:
        raise HTTPException(status_code=400, detail="Email already exists")
    role = (payload.role or "").strip() or None
    u = User(name=payload.name.strip(), email=payload.email.strip(), role=role)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@router.get("/users", response_model=list[UserResponse])
def list_users(db: Session = Depends(get_db)):
    return db.query(User).order_by(User.id).all()


@router.delete("/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    db.query(Project).filter(Project.created_by == user_id).update({Project.created_by: None}, synchronize_session=False)
    db.query(Commit).filter(Commit.created_by == user_id).update({Commit.created_by: None}, synchronize_session=False)
    db.query(File).filter(File.uploaded_by == user_id).update({File.uploaded_by: None}, synchronize_session=False)
    db.query(EditSession).filter(EditSession.editor_user_id == user_id).delete(synchronize_session=False)
    db.delete(u)
    db.commit()
    return {"ok": True}
