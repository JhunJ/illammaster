"""프로젝트 삭제 시 연관 행 정리 (커밋·도면·엔티티·블록·추출기록·세션)."""

from sqlalchemy.orm import Session

from app.models import (
    BlockAttr,
    BlockDef,
    BlockInsert,
    Commit,
    EditSession,
    Entity,
    File,
    Project,
    ScheduleRun,
)


def delete_project_cascade(db: Session, project_id: int) -> bool:
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        return False

    db.query(EditSession).filter(EditSession.project_id == project_id).delete(synchronize_session=False)

    commit_ids = [
        row[0] for row in db.query(Commit.id).filter(Commit.project_id == project_id).all()
    ]
    if commit_ids:
        db.query(Entity).filter(Entity.commit_id.in_(commit_ids)).delete(synchronize_session=False)
        insert_ids = [
            row[0]
            for row in db.query(BlockInsert.id).filter(BlockInsert.commit_id.in_(commit_ids)).all()
        ]
        if insert_ids:
            db.query(BlockAttr).filter(BlockAttr.insert_id.in_(insert_ids)).delete(synchronize_session=False)
        db.query(BlockInsert).filter(BlockInsert.commit_id.in_(commit_ids)).delete(synchronize_session=False)
        db.query(BlockDef).filter(BlockDef.commit_id.in_(commit_ids)).delete(synchronize_session=False)
        db.query(ScheduleRun).filter(ScheduleRun.commit_id.in_(commit_ids)).delete(synchronize_session=False)
        db.query(Commit).filter(Commit.parent_commit_id.in_(commit_ids)).update(
            {Commit.parent_commit_id: None}, synchronize_session=False
        )
        db.query(Commit).filter(Commit.project_id == project_id).delete(synchronize_session=False)

    db.query(File).filter(File.project_id == project_id).delete(synchronize_session=False)
    db.query(Project).filter(Project.id == project_id).delete(synchronize_session=False)
    return True
