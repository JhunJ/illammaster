import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_db
from app.models import Commit, File as FileModel, Project
from app.schemas.commit import CommitResponse
from app.services.storage import save_upload
from app.workers.commit_processor import process_commit

logger = logging.getLogger(__name__)
router = APIRouter(tags=["uploads"])


def _parse_json_form(value: str | None) -> dict[str, Any] | None:
    if not value or value.strip() in ("", "null"):
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


@router.post("/projects/{project_id}/uploads", response_model=CommitResponse)
def upload_file(
    project_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    created_by: int | None = Form(None),
    version_label: str | None = Form(None),
    parent_commit_id: int | None = Form(None),
    settings: str | None = Form(None),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    parent_id = None
    if parent_commit_id is not None and str(parent_commit_id).strip():
        try:
            parent_id = int(parent_commit_id)
        except (ValueError, TypeError):
            parent_id = None

    settings_dict = _parse_json_form(settings)
    allow_dxf = get_settings().dev_allow_dxf_upload
    filename = file.filename or "upload"
    suffix = filename.rsplit(".", 1)[-1].upper() if "." in filename else ""
    if suffix == "DWG":
        pass
    elif suffix == "DXF" and allow_dxf:
        pass
    elif suffix == "DXF":
        raise HTTPException(
            status_code=400,
            detail="DXF upload disabled. Set DEV_ALLOW_DXF_UPLOAD=true.",
        )
    else:
        raise HTTPException(status_code=400, detail="지원 형식: DWG, DXF")

    f = FileModel(
        project_id=project_id,
        original_filename=filename,
        storage_path="",
        sha256=None,
        file_size=None,
        uploaded_by=created_by,
    )
    db.add(f)
    db.flush()

    storage_path, sha256, file_size = save_upload(project_id, f.id, filename, file.file)
    f.storage_path = str(storage_path).replace("\\", "/")
    f.sha256 = sha256
    f.file_size = file_size

    commit = Commit(
        project_id=project_id,
        file_id=f.id,
        parent_commit_id=parent_id,
        version_label=version_label,
        status="PENDING",
        created_by=created_by,
        settings=settings_dict,
    )
    db.add(commit)
    db.commit()
    db.refresh(commit)

    if get_settings().sync_commit_processing:
        process_commit(commit.id)
    else:
        background_tasks.add_task(process_commit, commit.id)
    return commit
