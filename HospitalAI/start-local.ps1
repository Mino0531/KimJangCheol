# Start the two local prototype servers without displaying credentials.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$phoneRoot = Join-Path $root 'backend'
$dashboardRoot = Join-Path $root 'external\KimJangCheol'
$settingsPath = Join-Path $phoneRoot '.env'
$dashboardDb = [IO.Path]::GetFullPath((Join-Path $dashboardRoot 'backend\.local-data\hospital.sqlite3'))
$logRoot = Join-Path ([IO.Path]::GetTempPath()) ('hospitalai-local-logs-' + [guid]::NewGuid().ToString('N'))

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'uv를 설치한 뒤 다시 실행하세요.' }
if (-not (Test-Path -LiteralPath $settingsPath -PathType Leaf)) { throw 'backend\.env 파일이 필요합니다.' }
if (-not (Test-Path -LiteralPath (Join-Path $dashboardRoot 'backend\app.py') -PathType Leaf)) {
    throw '병원 대시보드 저장소가 external\KimJangCheol에 필요합니다.'
}

$listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
foreach ($port in @(8000, 8767)) {
    if ($listeners | Where-Object { $_.Port -eq $port }) {
        throw "포트 $port 가 이미 사용 중입니다. 기존 서버는 변경하지 않았습니다."
    }
}

Push-Location $phoneRoot
try {
    & uv sync --python 3.11
    if ($LASTEXITCODE -ne 0) { throw '전화 AI 의존성을 준비하지 못했습니다.' }
} finally { Pop-Location }

$phonePython = Join-Path $phoneRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $phonePython -PathType Leaf)) { throw '전화 AI Python 환경을 찾을 수 없습니다.' }
$sharedToken = [Environment]::GetEnvironmentVariable('HOSPITAL_DASHBOARD_TOKEN', 'Process')
if (-not $sharedToken) {
    $sharedToken = & $phonePython -c 'import sys; from dotenv import dotenv_values; sys.stdout.write(dotenv_values(sys.argv[1]).get("HOSPITAL_DASHBOARD_TOKEN") or "")' $settingsPath
    if ($LASTEXITCODE -ne 0) { throw '대시보드 설정을 읽지 못했습니다.' }
}
if (-not $sharedToken) { throw 'backend\.env에 HOSPITAL_DASHBOARD_TOKEN을 설정해 주세요.' }

# Read separate intake and staff credentials without displaying their values.
$municipalTokens = @{}
foreach ($name in @('MUNICIPAL_INTAKE_TOKEN', 'MUNICIPAL_STAFF_TOKEN')) {
    $value = [Environment]::GetEnvironmentVariable($name, 'Process')
    if (-not $value) {
        $value = & $phonePython -c 'import sys; from dotenv import dotenv_values; sys.stdout.write(dotenv_values(sys.argv[1]).get(sys.argv[2]) or "")' $settingsPath $name
        if ($LASTEXITCODE -ne 0) { throw '지자체 인증 설정을 읽지 못했습니다.' }
    }
    if (-not $value) { throw "backend\.env에 $name 을 설정해 주세요." }
    $municipalTokens[$name] = $value
}
if ($municipalTokens['MUNICIPAL_INTAKE_TOKEN'] -ceq $municipalTokens['MUNICIPAL_STAFF_TOKEN'] -or
    $municipalTokens['MUNICIPAL_INTAKE_TOKEN'] -ceq $sharedToken -or
    $municipalTokens['MUNICIPAL_STAFF_TOKEN'] -ceq $sharedToken) {
    throw '지자체 수신·직원·병원 인증값은 각각 달라야 합니다.'
}

$municipalUrl = [Environment]::GetEnvironmentVariable('MUNICIPAL_INTAKE_URL', 'Process')
if (-not $municipalUrl) {
    $municipalUrl = & $phonePython -c 'import sys; from dotenv import dotenv_values; sys.stdout.write(dotenv_values(sys.argv[1]).get("MUNICIPAL_INTAKE_URL") or "")' $settingsPath
    if ($LASTEXITCODE -ne 0) { throw '지자체 서버 주소를 읽지 못했습니다.' }
}
$municipalUri = $null
if (-not [Uri]::TryCreate($municipalUrl, [UriKind]::Absolute, [ref]$municipalUri) -or
    $municipalUri.Scheme -ne 'http' -or $municipalUri.Host -notin @('127.0.0.1', 'localhost') -or
    $municipalUri.Port -ne 8767 -or $municipalUri.AbsolutePath -ne '/' -or
    $municipalUri.Query -or $municipalUri.Fragment -or $municipalUri.UserInfo) {
    throw '이 로컬 실행 스크립트는 MUNICIPAL_INTAKE_URL이 http://127.0.0.1:8767인 경우에만 작동합니다.'
}

$dashboardUrl = [Environment]::GetEnvironmentVariable('HOSPITAL_DASHBOARD_URL', 'Process')
if (-not $dashboardUrl) {
    $dashboardUrl = & $phonePython -c 'import sys; from dotenv import dotenv_values; sys.stdout.write(dotenv_values(sys.argv[1]).get("HOSPITAL_DASHBOARD_URL") or "")' $settingsPath
    if ($LASTEXITCODE -ne 0) { throw '대시보드 주소를 읽지 못했습니다.' }
}
$dashboardUri = $null
if (-not [Uri]::TryCreate($dashboardUrl, [UriKind]::Absolute, [ref]$dashboardUri) -or
    $dashboardUri.Scheme -ne 'http' -or $dashboardUri.Host -notin @('127.0.0.1', 'localhost') -or
    $dashboardUri.Port -ne 8767 -or $dashboardUri.AbsolutePath -ne '/' -or
    $dashboardUri.Query -or $dashboardUri.Fragment -or $dashboardUri.UserInfo) {
    throw '이 로컬 실행 스크립트는 HOSPITAL_DASHBOARD_URL이 http://127.0.0.1:8767인 경우에만 작동합니다.'
}

$phoneDbSetting = [Environment]::GetEnvironmentVariable('HOSPITAL_DB_PATH', 'Process')
if (-not $phoneDbSetting) {
    $phoneDbSetting = & $phonePython -c 'import sys; from dotenv import dotenv_values; sys.stdout.write(dotenv_values(sys.argv[1]).get("HOSPITAL_DB_PATH") or "hospital.sqlite3")' $settingsPath
    if ($LASTEXITCODE -ne 0) { throw '전화 AI DB 설정을 읽지 못했습니다.' }
}
if ([IO.Path]::IsPathRooted($phoneDbSetting)) { $phoneDb = [IO.Path]::GetFullPath($phoneDbSetting) }
else { $phoneDb = [IO.Path]::GetFullPath((Join-Path $phoneRoot $phoneDbSetting)) }
if ([String]::Equals($phoneDb, $dashboardDb, [StringComparison]::OrdinalIgnoreCase)) {
    throw '전화 AI와 병원 대시보드 DB 경로가 같습니다. 서로 다른 SQLite 파일을 설정해 주세요.'
}

$privateNames = @(Get-ChildItem Env: | Where-Object {
    $_.Name -match '(?i)(?:_API_KEY|_SPEECH_KEY|_SECRET|_SECRET_KEY|_ACCESS_TOKEN|_TOKEN|^AWS_ACCESS_KEY_ID)$' -and
    $_.Name -ne 'HOSPITAL_DASHBOARD_TOKEN'
} | ForEach-Object { $_.Name })
$privateValues = @{}
foreach ($name in $privateNames) {
    $privateValues[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    [Environment]::SetEnvironmentVariable($name, $null, 'Process')
}
try {
    Push-Location $dashboardRoot
    try {
        & uv run --python 3.14 --with-requirements backend/requirements.txt python -c 'import fastapi, sqlmodel, uvicorn'
        if ($LASTEXITCODE -ne 0) { throw '대시보드 의존성을 준비하지 못했습니다.' }
    } finally { Pop-Location }
} finally {
    foreach ($name in $privateNames) { [Environment]::SetEnvironmentVariable($name, $privateValues[$name], 'Process') }
}

New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$oldDb = [Environment]::GetEnvironmentVariable('HOSPITAL_DB_PATH', 'Process')
$oldToken = [Environment]::GetEnvironmentVariable('HOSPITAL_DASHBOARD_TOKEN', 'Process')
$oldMunicipalIntakeToken = [Environment]::GetEnvironmentVariable('MUNICIPAL_INTAKE_TOKEN', 'Process')
$oldMunicipalStaffToken = [Environment]::GetEnvironmentVariable('MUNICIPAL_STAFF_TOKEN', 'Process')
try {
    $env:HOSPITAL_DASHBOARD_TOKEN = $sharedToken
    $env:HOSPITAL_DB_PATH = $dashboardDb
    foreach ($name in $privateNames) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
    foreach ($name in $municipalTokens.Keys) { [Environment]::SetEnvironmentVariable($name, $municipalTokens[$name], 'Process') }
    $dashboard = Start-Process -FilePath (Get-Command uv).Source -WorkingDirectory $dashboardRoot `
        -ArgumentList @('run', '--python', '3.14', '--with-requirements', 'backend/requirements.txt', 'python', '-m', 'uvicorn', 'backend.app:app', '--host', '127.0.0.1', '--port', '8767', '--no-access-log') `
        -RedirectStandardOutput (Join-Path $logRoot 'dashboard.stdout.log') `
        -RedirectStandardError (Join-Path $logRoot 'dashboard.stderr.log') -WindowStyle Hidden -PassThru
    foreach ($name in $privateNames) { [Environment]::SetEnvironmentVariable($name, $privateValues[$name], 'Process') }
    [Environment]::SetEnvironmentVariable('MUNICIPAL_INTAKE_TOKEN', $municipalTokens['MUNICIPAL_INTAKE_TOKEN'], 'Process')
    [Environment]::SetEnvironmentVariable('MUNICIPAL_STAFF_TOKEN', $null, 'Process')
    $env:HOSPITAL_DB_PATH = $phoneDb
    $phone = Start-Process -FilePath (Get-Command uv).Source -WorkingDirectory $phoneRoot `
        -ArgumentList @('run', 'python', '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000', '--no-access-log') `
        -RedirectStandardOutput (Join-Path $logRoot 'phone.stdout.log') `
        -RedirectStandardError (Join-Path $logRoot 'phone.stderr.log') -WindowStyle Hidden -PassThru
} finally {
    foreach ($name in $privateNames) { [Environment]::SetEnvironmentVariable($name, $privateValues[$name], 'Process') }
    [Environment]::SetEnvironmentVariable('HOSPITAL_DB_PATH', $oldDb, 'Process')
    [Environment]::SetEnvironmentVariable('HOSPITAL_DASHBOARD_TOKEN', $oldToken, 'Process')
    [Environment]::SetEnvironmentVariable('MUNICIPAL_INTAKE_TOKEN', $oldMunicipalIntakeToken, 'Process')
    [Environment]::SetEnvironmentVariable('MUNICIPAL_STAFF_TOKEN', $oldMunicipalStaffToken, 'Process')
}

function Wait-LocalHealth([string]$Url, [int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Url -TimeoutSec 2 -UseBasicParsing
            if ($response.StatusCode -eq 200) { return $true }
        } catch {
            # Startup can temporarily refuse or time out while dependencies initialize.
        }
        Start-Sleep -Milliseconds 300
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

$dashboardReady = Wait-LocalHealth 'http://127.0.0.1:8767/api/health' 30
$phoneReady = Wait-LocalHealth 'http://127.0.0.1:8000/health' 30
if (-not $dashboardReady -or -not $phoneReady) {
    Write-Warning "서버 준비를 확인하지 못했습니다. 로그: $logRoot. 실행한 프로세스 ID: 전화 $($phone.Id), 병원 $($dashboard.Id)"
    exit 1
}
Write-Output '전화 상담 화면: http://127.0.0.1:8000/ui/'
Write-Output '지자체 대시보드: http://127.0.0.1:8767/municipality'
Write-Output '병원 대시보드: http://127.0.0.1:8767/'
Write-Output "시작한 프로세스 ID: 전화 $($phone.Id), 병원 $($dashboard.Id)"
Write-Output "오류 로그 위치: $logRoot"
Write-Output '지자체 직원 및 병원 대시보드 인증값은 backend\.env에서 확인하세요. 전화 화면에는 토큰 입력이 필요하지 않습니다. 실제 환자 정보는 입력하지 마세요.'
