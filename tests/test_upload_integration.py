"""PostgreSQL(PostGIS)가 준비된 경우에만 실행되는 업로드 스모크 테스트.

로컬에서 DB가 없으면 skip 됩니다. DB를 띄운 뒤:

  set SYNC_COMMIT_PROCESSING=true
  pytest tests/test_upload_integration.py -m integration -v
"""
from __future__ import annotations

import io
import os
import uuid

# 업로드 응답 시점에 process_commit까지 끝나도록 (get_settings()가 매 요청 시 읽음)
os.environ.setdefault("SYNC_COMMIT_PROCESSING", "true")

import ezdxf  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.db.session import engine  # noqa: E402
from app.main import app  # noqa: E402


def _db_available() -> tuple[bool, str]:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as e:
        return False, str(e)
    return True, ""


@pytest.mark.integration
def test_upload_minimal_dxf_commit_ready():
    ok, err = _db_available()
    if not ok:
        pytest.skip(f"PostgreSQL 연결 불가: {err}")

    def make_dxf_bytes() -> bytes:
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("D22", dxfattribs={"height": 2.5, "insert": (0, 0)})
        buf = io.StringIO()
        doc.write(buf)
        return buf.getvalue().encode("utf-8")

    c = TestClient(app)
    code = "it-" + uuid.uuid4().hex[:12]
    rp = c.post(
        "/api/projects",
        json={"name": "Upload integration", "code": code, "created_by": None},
    )
    assert rp.status_code == 200, rp.text
    pid = rp.json()["id"]

    ru = c.post(
        f"/api/projects/{pid}/uploads",
        files={"file": ("minimal.dxf", make_dxf_bytes(), "application/acad")},
    )
    assert ru.status_code == 200, ru.text
    body = ru.json()
    assert body.get("status") == "READY", body
