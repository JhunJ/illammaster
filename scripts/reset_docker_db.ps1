# Docker Desktop 이 켜진 상태에서만 동작합니다.
# 기존 illammaster 볼륨을 지우고 compose 대로 DB를 다시 만들면 caduser/cadpass 가 다시 맞습니다.
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

$docker = $null
foreach ($c in @(
        (Get-Command docker -ErrorAction SilentlyContinue).Source,
        "${env:ProgramFiles}\Docker\Docker\resources\bin\docker.exe"
    )) {
    if ($c -and (Test-Path -LiteralPath $c)) { $docker = $c; break }
}
if (-not $docker) {
    throw "docker.exe 없음. Docker Desktop 설치 후 실행하세요."
}

Write-Host "[reset_docker_db] docker compose down -v ..."
& $docker "compose" "-f" (Join-Path $root "docker-compose.yml") "down" "-v"
if ($LASTEXITCODE -ne 0) { throw "compose down 실패" }

Write-Host "[reset_docker_db] docker compose up -d ..."
& $docker "compose" "-f" (Join-Path $root "docker-compose.yml") "up" "-d"
if ($LASTEXITCODE -ne 0) { throw "compose up 실패" }

Write-Host "[reset_docker_db] 완료. .env 가 postgresql+psycopg://caduser:cadpass@127.0.0.1:5434/illammaster 인지 확인 후 alembic upgrade head"
