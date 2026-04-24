"""PostgreSQL 동기 드라이버 연결 설정.

Windows에서 psycopg2+libpq가 서버 오류 메시지(CP949 등)를 UTF-8로만 처리하며
UnicodeDecodeError가 나는 경우가 있어, 동기 연결은 psycopg v3를 쓴다.
"""

# psycopg.connect() / SQLAlchemy connect_args
# 일부 PostgreSQL 환경(권한 제한 계정)에서는 `lc_messages` 세션 파라미터 설정이 거부된다.
# 로컬 실행 안정성을 위해 UTF-8 인코딩만 강제하고 lc_messages는 건드리지 않는다.
PG_CONNECT_KWARGS: dict[str, str] = {
    "options": "-c client_encoding=UTF8",
}

# 하위 호환 (import 깨짐 방지)
PSYCOPG2_CONNECT_ARGS = PG_CONNECT_KWARGS


def normalize_sync_postgresql_url(url: str) -> str:
    """sync 엔진용 URL: +psycopg2 / +asyncpg → +psycopg."""
    if not url:
        return url
    u = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    u = u.replace("postgresql+psycopg2://", "postgresql+psycopg://")
    return u
