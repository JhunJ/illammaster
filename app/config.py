"""앱 설정: DB, ODA, 스토리지."""
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

os.environ.setdefault("PGCLIENTENCODING", "UTF8")

_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """env_file 은 프로젝트 루트 고정(작업 디렉터리와 무관)."""

    model_config = SettingsConfigDict(
        env_file=_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://caduser:cadpass@127.0.0.1:5432/illammaster"
    upload_root: Path = Path("./data/uploads")
    oda_fc_path: str | None = None
    oda_dxf_version: str = "ACAD2018"
    dev_allow_dxf_upload: bool = True
    sync_commit_processing: bool = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        oda = os.environ.get("ODA_FC_PATH", "").strip()
        if oda and not self.oda_fc_path:
            self.oda_fc_path = oda or None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Windows 사용자 환경에 남은 DATABASE_URL 이 .env 보다 우선하는 문제 방지 + 프로젝트 .env 고정."""
    if (_ROOT / ".env").is_file():
        os.environ.pop("DATABASE_URL", None)
        load_dotenv(_ROOT / ".env", override=True)
    return Settings()
