"""
로컬 파일 스토리지 추상화.
MVP: ./data/uploads. 추후 S3/MinIO 교체 가능하도록 인터페이스 유지.
"""
import hashlib
import logging
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO

from app.config import get_settings

logger = logging.getLogger(__name__)


def get_upload_root() -> Path:
    root = get_settings().upload_root
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_upload(project_id: int, file_id: int, filename: str, stream: BinaryIO) -> tuple[Path, str, int]:
    """
    업로드 스트림을 저장하고 (storage_path, sha256, file_size) 반환.
    경로: {upload_root}/projects/{project_id}/{file_id}_{safe_filename}
    """
    root = get_upload_root()
    project_dir = root / "projects" / str(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    safe_name = (filename or "upload").replace(" ", "_")
    if len(safe_name) > 200:
        ext = Path(safe_name).suffix
        safe_name = f"{uuid.uuid4().hex}{ext}"
    storage_name = f"{file_id}_{safe_name}"
    storage_path = project_dir / storage_name

    hasher = hashlib.sha256()
    size = 0
    with open(storage_path, "wb") as f:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
            f.write(chunk)
            size += len(chunk)

    sha256 = hasher.hexdigest()
    rel_path = storage_path.relative_to(root)
    return Path(rel_path), sha256, size


def get_full_path(relative_storage_path: str | Path) -> Path:
    """상대 storage_path -> 절대 경로."""
    root = get_upload_root()
    return root / relative_storage_path


def read_file(relative_storage_path: str | Path) -> bytes:
    path = get_full_path(relative_storage_path)
    return path.read_bytes()


def delete_file(relative_storage_path: str | Path) -> bool:
    """물리 파일 삭제. 없으면 False 반환."""
    path = get_full_path(relative_storage_path)
    if not path.exists() or not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        logger.warning("Failed to delete file: %s", path)
        return False


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_annotation_image(
    project_id: int,
    annotation_id: int,
    filename: str,
    stream: BinaryIO,
) -> str:
    """
    주석 이미지 저장. 경로: projects/{project_id}/annotations/{annotation_id}/{uuid}.{ext}
    반환: 상대 경로 (str로 사용 시 replace backslash)
    """
    root = get_upload_root()
    ann_dir = root / "projects" / str(project_id) / "annotations" / str(annotation_id)
    ann_dir.mkdir(parents=True, exist_ok=True)
    safe_name = (filename or "image").replace(" ", "_")
    ext = Path(safe_name).suffix.lower() if "." in safe_name else ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        ext = ".png"
    storage_name = f"{uuid.uuid4().hex}{ext}"
    storage_path = ann_dir / storage_name

    with open(storage_path, "wb") as f:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            f.write(chunk)

    rel_path = storage_path.relative_to(root)
    return str(rel_path).replace("\\", "/")
