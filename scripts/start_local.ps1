# 로컬 PostgreSQL(PostGIS)만 사용 — Docker 없음 (CAD Manage 와 같은 방식)
# 사전: db/setup_local_postgis.sql 실행 + .env 의 DATABASE_URL 이 해당 DB를 가리킴
# 사용: .\start_local.bat

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot
$env:PYTHONPATH = $ProjectRoot
$env:PYTHONIOENCODING = "utf-8"
if (-not $env:ILLAM_PORT) { $env:ILLAM_PORT = "8011" }
$IllamPort = $env:ILLAM_PORT

Write-Host "[start_local] ProjectRoot=$ProjectRoot (Docker 없음, 로컬 PostGIS 전제)"

$envFile = Join-Path $ProjectRoot ".env"
$example = Join-Path $ProjectRoot ".env.example"
if (-not (Test-Path -LiteralPath $envFile) -and (Test-Path -LiteralPath $example)) {
    Copy-Item -LiteralPath $example -Destination $envFile
    Write-Host "[start_local] .env 생성 (.env.example 복사) — DATABASE_URL 포트(5432 등) 확인"
}

$py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$pip = Join-Path $ProjectRoot ".venv\Scripts\pip.exe"
if (-not (Test-Path -LiteralPath $py)) {
    & python -m venv (Join-Path $ProjectRoot ".venv")
}
& $pip install -q -r (Join-Path $ProjectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install 실패" }

& $py (Join-Path $ProjectRoot "scripts\wait_db.py")
if ($LASTEXITCODE -ne 0) {
    throw @"
DB 연결 실패. 다음을 확인하세요.
1) PostgreSQL 서비스 실행
2) db\setup_local_postgis.sql 을 psql -U postgres 로 실행했는지
3) .env 의 DATABASE_URL (호스트·포트·사용자·비밀번호·DB명)
"@
}

$alembic = Join-Path $ProjectRoot ".venv\Scripts\alembic.exe"
& $alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "alembic upgrade 실패 — PostGIS 확장(create extension postgis) 여부 확인" }

Write-Host ""
Write-Host "[start_local] http://127.0.0.1:$IllamPort / LAN은 이 PC IP:$IllamPort (0.0.0.0 바인딩, 이 창을 닫으면 서버 종료)"
Write-Host ""
& $py -m uvicorn "app.main:app" "--host" "0.0.0.0" "--port" $IllamPort "--reload"
