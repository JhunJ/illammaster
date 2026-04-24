-- 로컬에 설치한 PostgreSQL(EDB 등)에서, 슈퍼유저 postgres 로 한 번 실행하세요.
-- 예:
--   & "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -h 127.0.0.1 -p 5432 -f db/setup_local_postgis.sql
--
-- 이미 caduser 또는 illammaster 가 있으면 해당 줄에서 오류가 날 수 있습니다. 그때는 수동으로 맞추거나
-- 기존 DB/역할을 지운 뒤 다시 실행하세요.
--
-- 실행 후 .env 의 DATABASE_URL 이 여기 비밀번호·호스트·포트·DB명과 같아야 합니다.

\set ON_ERROR_STOP on

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'caduser') THEN
    CREATE ROLE caduser WITH LOGIN PASSWORD 'cadpass';
  END IF;
END
$$;

SELECT format(
  'CREATE DATABASE illammaster OWNER caduser TEMPLATE template0 ENCODING %L',
  'UTF8'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'illammaster');
\gexec

\c illammaster postgres
CREATE EXTENSION IF NOT EXISTS postgis;
