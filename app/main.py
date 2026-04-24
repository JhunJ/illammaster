"""Illammaster API — 건축 일람 DWG 구조 일람표 추출."""
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.api import commits, edit_sessions, preview, projects, schedule, uploads, users
from app.db.session import SessionLocal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Illammaster API",
    description="DWG/DXF → 일람 추출 (벽체·보·기둥·슬라브)",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router, prefix="/api")
app.include_router(edit_sessions.router, prefix="/api")
app.include_router(projects.router, prefix="/api")
app.include_router(uploads.router, prefix="/api")
app.include_router(commits.router, prefix="/api")
app.include_router(schedule.router, prefix="/api")
app.include_router(preview.router, prefix="/api")


@app.exception_handler(OperationalError)
async def database_unavailable_handler(request: Request, exc: OperationalError):
    """DB 미기동·URL 오류 시 500 대신 원인을 알 수 있게 503으로 응답."""
    logger.warning("Database operational error: %s", exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": (
                "PostgreSQL(PostGIS)에 연결할 수 없습니다. "
                "PostgreSQL 서비스가 떠 있는지, .env의 DATABASE_URL(호스트·포트·계정·비밀번호)이 맞는지 확인한 뒤 "
                "alembic upgrade head 를 실행하세요."
            ),
            "error_code": "database_unavailable",
        },
    )


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _read_html(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8")


@app.get("/", response_class=HTMLResponse)
def root():
    path = STATIC_DIR / "index.html"
    if not path.exists():
        return "<h1>static/index.html 없음</h1>"
    return _read_html(path)


@app.get("/manage", response_class=HTMLResponse)
def manage_page():
    path = STATIC_DIR / "manage.html"
    if not path.exists():
        return "<h1>static/manage.html 없음</h1>"
    return _read_html(path)


@app.get("/health")
def health():
    return {"status": "ok", "service": "illammaster"}


@app.get("/health/db")
@app.get("/api/health/db")
def health_database():
    """DB 연결 여부 확인(배포·로컬 진단용). /health/db 는 ?api= 베이스와 경로 합칠 때 /api 중복 404 방지용."""
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except OperationalError as e:
        logger.warning("health/db failed: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "database": "disconnected",
                "detail": str(e.orig) if getattr(e, "orig", None) else str(e),
            },
        )
    finally:
        db.close()
