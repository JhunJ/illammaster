"""Create illammaster DB + postgis if missing (uses project .env DATABASE_URL)."""
import sys
from pathlib import Path

import psycopg
from sqlalchemy.engine.url import make_url

_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    sys.path.insert(0, str(_ROOT))
    from app.config import get_settings

    get_settings.cache_clear()
    u = make_url(get_settings().database_url)
    if not u.password:
        raise SystemExit("DATABASE_URL 에 비밀번호가 없습니다.")
    host = u.host or "127.0.0.1"
    port = u.port or 5432
    user = u.username or "postgres"
    pw = u.password
    dbn = (u.database or "illammaster").split("?")[0]
    admin = (
        f"host={host} port={port} user={user} "
        f"password={pw} dbname=postgres connect_timeout=10"
    )
    conn = psycopg.connect(admin)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbn,))
    if not cur.fetchone():
        cur.execute(
            f'CREATE DATABASE "{dbn}" OWNER "{user}" ENCODING \'UTF8\' TEMPLATE template0'
        )
        print(f"CREATE DATABASE {dbn} OK")
    else:
        print(f"DB {dbn} already exists")
    cur.close()
    conn.close()

    app = (
        f"host={host} port={port} user={user} "
        f"password={pw} dbname={dbn} connect_timeout=10"
    )
    conn2 = psycopg.connect(app)
    conn2.autocommit = True
    cur2 = conn2.cursor()
    cur2.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    print("CREATE EXTENSION postgis OK")
    cur2.close()
    conn2.close()
    print("Done")


if __name__ == "__main__":
    main()
    sys.exit(0)
