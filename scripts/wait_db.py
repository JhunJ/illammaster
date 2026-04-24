"""PostgreSQL이 응답할 때까지 대기 (run_all.ps1에서 사용)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.pg_connect import PG_CONNECT_KWARGS, normalize_sync_postgresql_url  # noqa: E402


def main() -> int:
    retries = int(os.environ.get("DB_WAIT_RETRIES", "60"))
    delay = float(os.environ.get("DB_WAIT_DELAY_SEC", "2"))
    url = normalize_sync_postgresql_url(get_settings().database_url)
    engine = create_engine(url, pool_pre_ping=True, connect_args=PG_CONNECT_KWARGS)
    for i in range(retries):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            print(f"[wait_db] OK ({i + 1}/{retries})")
            return 0
        except OperationalError as e:
            print(f"[wait_db] {i + 1}/{retries}: {e}")
            time.sleep(delay)
    print("[wait_db] timeout", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
