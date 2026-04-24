# Illammaster: venv -> (Docker PostGIS) -> alembic -> pytest -> uvicorn
# 사용: 프로젝트 루트에서  .\scripts\run_all.ps1  또는  .\run_all.bat
# 옵션:
#   -Thin          DB·compose·마이그레이션 생략, 단위 테스트만 + 서버 (Docker 없는 PC용)
#   -SkipDocker     compose 생략 (이미 떠 있는 PostgreSQL만 사용)
#   -SkipTests      pytest 생략
#   -SkipServer     uvicorn 생략

[CmdletBinding()]
param(
    [switch] $Thin,
    [switch] $SkipDocker,
    [switch] $SkipTests,
    [switch] $SkipServer
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot
$env:PYTHONPATH = $ProjectRoot
$env:PYTHONIOENCODING = "utf-8"
# CAD Manage 등이 8000을 쓰는 경우가 많아 Illammaster 기본은 8011 (환경변수 ILLAM_PORT 로 변경)
if (-not $env:ILLAM_PORT) { $env:ILLAM_PORT = "8011" }
$IllamPort = $env:ILLAM_PORT

function Find-DockerExe {
    $cmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        "${env:ProgramFiles}\Docker\Docker\resources\bin\docker.exe",
        "${env:ProgramFiles(x86)}\Docker\Docker\resources\bin\docker.exe",
        "${env:LocalAppData}\Programs\Docker\Docker\resources\bin\docker.exe"
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path -LiteralPath $p)) { return $p }
    }
    return $null
}

function Invoke-ComposeUp {
    param([string] $ComposeFile)
    $dockerExe = Find-DockerExe
    if ($dockerExe) {
        Write-Host "[compose] $dockerExe compose -f $ComposeFile up -d"
        & $dockerExe "compose" "-f" $ComposeFile "up" "-d"
        if ($LASTEXITCODE -eq 0) { return $true }
        Write-Warning "[run_all] docker compose 실패 (exit $LASTEXITCODE)"
    }
    $podman = Get-Command podman -ErrorAction SilentlyContinue
    if ($podman) {
        Write-Host "[compose] podman compose -f $ComposeFile up -d"
        & $podman.Source "compose" "-f" $ComposeFile "up" "-d"
        if ($LASTEXITCODE -eq 0) { return $true }
        Write-Warning "[run_all] podman compose 실패 (exit $LASTEXITCODE)"
    }
    Write-Warning "[run_all] Docker/Podman 없음 또는 compose 실패 → DB는 수동 기동 필요(.env 의 DATABASE_URL)."
    return $false
}

Write-Host "[run_all] ProjectRoot=$ProjectRoot"

# .env
$envFile = Join-Path $ProjectRoot ".env"
$example = Join-Path $ProjectRoot ".env.example"
if (-not (Test-Path -LiteralPath $envFile) -and (Test-Path -LiteralPath $example)) {
    Copy-Item -LiteralPath $example -Destination $envFile
    Write-Host "[run_all] .env 생성 (.env.example 복사)"
}

# venv + 패키지
$py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$pip = Join-Path $ProjectRoot ".venv\Scripts\pip.exe"
if (-not (Test-Path -LiteralPath $py)) {
    Write-Host "[run_all] python -m venv .venv"
    & python -m venv (Join-Path $ProjectRoot ".venv")
    if (-not (Test-Path -LiteralPath $py)) { throw "venv 생성 실패" }
}
& $pip install -q -r (Join-Path $ProjectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install 실패" }

# Thin: Docker/DB 없이 UI·단위 테스트만
if ($Thin) {
    Write-Host "[run_all] -Thin: compose / DB 대기 / alembic 생략"
    if (-not $SkipTests) {
        & $py -m pytest (Join-Path $ProjectRoot "tests") -q -m "not integration" --tb=short
        if ($LASTEXITCODE -ne 0) { throw "pytest 실패" }
    }
    if (-not $SkipServer) {
        $uvicorn = Join-Path $ProjectRoot ".venv\Scripts\uvicorn.exe"
        $argList = @("app.main:app", "--host", "0.0.0.0", "--port", $IllamPort, "--reload")
        Write-Host "[run_all] uvicorn 0.0.0.0:$IllamPort (Thin, 새 창) — LAN/Cloudflare 원본 가능"
        Start-Process -FilePath $uvicorn -ArgumentList $argList -WorkingDirectory $ProjectRoot
    }
    Write-Host "[run_all] 완료 (Thin)"
    exit 0
}

# Docker/Podman PostGIS (있으면 기동, 없거나 실패해도 이후 wait_db 로 판별)
if (-not $SkipDocker) {
    $composeFile = Join-Path $ProjectRoot "docker-compose.yml"
    if (-not (Test-Path -LiteralPath $composeFile)) { throw "docker-compose.yml 없음" }
    Invoke-ComposeUp -ComposeFile $composeFile | Out-Null
} else {
    Write-Host "[run_all] -SkipDocker: compose 생략"
}

# DB 대기
& $py (Join-Path $ProjectRoot "scripts\wait_db.py")
if ($LASTEXITCODE -ne 0) { throw "DB 연결 대기 실패. Docker Desktop 설치 후 재실행하거나, 로컬 PG면 db/setup_local_postgis.sql 및 .env DATABASE_URL(로컬 보통 5432, Docker 호스트 5434)을 확인하세요." }

# 마이그레이션
$alembic = Join-Path $ProjectRoot ".venv\Scripts\alembic.exe"
& $alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "alembic upgrade 실패" }

# 테스트 (통합 포함: 동기 커밋 처리)
if (-not $SkipTests) {
    $env:SYNC_COMMIT_PROCESSING = "true"
    & $py -m pytest (Join-Path $ProjectRoot "tests") -q --tb=short
    if ($LASTEXITCODE -ne 0) { throw "pytest 실패" }
} else {
    Write-Host "[run_all] -SkipTests: 테스트 생략"
}

# API 서버 (백그라운드)
if (-not $SkipServer) {
    $uvicorn = Join-Path $ProjectRoot ".venv\Scripts\uvicorn.exe"
    $argList = @(
        "app.main:app",
        "--host", "0.0.0.0",
        "--port", $IllamPort,
        "--reload"
    )
    Write-Host "[run_all] uvicorn 0.0.0.0:$IllamPort (새 창) — LAN/Cloudflare 원본 가능"
    Start-Process -FilePath $uvicorn -ArgumentList $argList -WorkingDirectory $ProjectRoot
} else {
    Write-Host "[run_all] -SkipServer: 서버 생략"
}

Write-Host "[run_all] 완료"
