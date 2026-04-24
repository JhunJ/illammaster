-- caduser 비밀번호를 .env 의 cadpass 와 맞출 때 (슈퍼유저 postgres 로 실행)
-- "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -h 127.0.0.1 -p 5434 -d postgres -f db/repair_caduser_password.sql

\set ON_ERROR_STOP on

DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'caduser') THEN
    ALTER ROLE caduser WITH PASSWORD 'cadpass';
  ELSE
    CREATE ROLE caduser WITH LOGIN PASSWORD 'cadpass';
  END IF;
END
$$;
