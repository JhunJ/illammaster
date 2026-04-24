# PostGIS(Docker) + alembic + uvicorn 을 한 터미널에서 기동 (개발용)
# 로컬 PostgreSQL만 쓸 때(CAD Manage 방식): .\start_local.bat
# 사용: .\start_dev.bat  또는  powershell -File .\scripts\start_dev.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot
$env:PYTHONPATH = $ProjectRoot
$env:PYTHONIOENCODING = "utf-8"
if (-not $env:ILLAM_PORT) { $env:ILLAM_PORT = "8011" }
$IllamPort = $env:ILLAM_PORT

function Find-DockerExe {
    $cmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @(
        "${env:ProgramFiles}\Docker\Docker\resources\bin\docker.exe",
        "${env:ProgramFiles(x86)}\Docker\Docker\resources\bin\docker.exe",
        "${env:LocalAppData}\Programs\Docker\Docker\resources\bin\docker.exe"
    )) {
        if ($p -and (Test-Path -LiteralPath $p)) { return $p }
    }
    return $null
}

function Start-DockerDesktopIfInstalled {
    $dd = "${env:ProgramFiles}\Docker\Docker\Docker Desktop.exe"
    if (Test-Path -LiteralPath $dd) {
        Write-Host "[start_dev] Docker Desktop 실행 시도"
        Start-Process -FilePath $dd -ErrorAction SilentlyContinue
    }
}

function Wait-DockerEngine {
    param([string] $DockerExe)
    for ($i = 0; $i -lt 120; $i++) {
        & $DockerExe "info" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[start_dev] Docker 엔진 준비됨"
            return
        }
        if ($i -eq 0) { Start-DockerDesktopIfInstalled }
        if ($i % 10 -eq 0) { Write-Host "[start_dev] Docker 엔진 대기 중... ($i/120)" }
        Start-Sleep 2
    }
    throw "Docker 엔진이 응답하지 않습니다. Docker Desktop을 설치·실행한 뒤 이 스크립트를 다시 실행하세요."
}

function Invoke-ComposeUp {
    param([string] $ComposeFile, [string] $DockerExe)
    Write-Host "[start_dev] $DockerExe compose -f $ComposeFile up -d"
    & $DockerExe "compose" "-f" $ComposeFile "up" "-d"
    if ($LASTEXITCODE -eq 0) { return }
    $podman = Get-Command podman -ErrorAction SilentlyContinue
    if ($podman) {
        Write-Host "[start_dev] podman compose (대체)"
        & $podman.Source "compose" "-f" $ComposeFile "up" "-d"
        if ($LASTEXITCODE -eq 0) { return }
    }
    throw "docker compose 실패 (exit $LASTEXITCODE)"
}

Write-Host "[start_dev] ProjectRoot=$ProjectRoot"

$envFile = Join-Path $ProjectRoot ".env"
$example = Join-Path $ProjectRoot ".env.example"
if (-not (Test-Path -LiteralPath $envFile) -and (Test-Path -LiteralPath $example)) {
    Copy-Item -LiteralPath $example -Destination $envFile
    Write-Host "[start_dev] .env 생성"
}

$py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$pip = Join-Path $ProjectRoot ".venv\Scripts\pip.exe"
if (-not (Test-Path -LiteralPath $py)) {
    & python -m venv (Join-Path $ProjectRoot ".venv")
}
& $pip install -q -r (Join-Path $ProjectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install 실패" }

$dockerExe = Find-DockerExe
if (-not $dockerExe) {
    throw @"
Docker CLI 를 찾지 못했습니다.
1) winget install -e --id Docker.DockerDesktop --accept-source-agreements
2) 설치 후 Docker Desktop 실행, 트레이에서 'Engine running' 확인
3) 새 터미널에서 다시 .\start_dev.bat
"@
}

Wait-DockerEngine -DockerExe $dockerExe

$composeFile = Join-Path $ProjectRoot "docker-compose.yml"
Invoke-ComposeUp -ComposeFile $composeFile -DockerExe $dockerExe

& $py (Join-Path $ProjectRoot "scripts\wait_db.py")
if ($LASTEXITCODE -ne 0) { throw "DB 연결 실패 — docker compose 로그 확인: docker compose logs" }

$alembic = Join-Path $ProjectRoot ".venv\Scripts\alembic.exe"
& $alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "alembic upgrade 실패" }

Write-Host ""
Write-Host "[start_dev] http://127.0.0.1:$IllamPort 또는 LAN IP:$IllamPort (0.0.0.0, 이 창을 닫으면 서버 종료)"
Write-Host ""
& $py -m uvicorn "app.main:app" "--host" "0.0.0.0" "--port" $IllamPort "--reload"
