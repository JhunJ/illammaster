"""도면 프리뷰(캔버스용) API."""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Commit
from app.services.viewer_preview import MAX_LIMIT, build_viewer_preview

router = APIRouter(tags=["preview"])


@router.get("/commits/{commit_id}/preview")
def get_commit_preview(
    commit_id: int,
    limit: int = Query(
        0,
        ge=0,
        le=MAX_LIMIT,
        description="0이면 커밋 전체 엔티티. 양수면 최대 개수(상한).",
    ),
    simplify: float = Query(
        0.0,
        ge=0.0,
        le=10.0,
        description="Shapely simplify 허용 오차(도면 단위). 0이면 생략",
    ),
    db: Session = Depends(get_db),
):
    """커밋에 적재된 기하·텍스트를 2D 캔버스에 그리기 위한 JSON."""
    c = db.query(Commit).filter(Commit.id == commit_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Commit not found")
    data = build_viewer_preview(db, commit_id, limit=limit, simplify_tol=simplify)
    if data.get("error") == "commit_not_found":
        raise HTTPException(status_code=404, detail="Commit not found")
    return JSONResponse(
        content=data,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )
