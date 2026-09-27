# 치지직 → 유튜브 자동화 설치 스크립트 (Windows PowerShell)
# 실행:  powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12  # 구형 윈도우10 다운로드 실패 방지

# 검증한 upstream 커밋 (내부 함수를 재사용하므로 고정. 올릴 땐 테스트 후 변경)
$UpstreamCommit = "4aad567d97deb9be851bb03de37c6cb66e117428"
$Vendor = "vendor\chzzk-vod-downloader-v2"

# 1) uv
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[1/4] uv 설치"
    powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
} else { Write-Host "[1/4] uv 확인됨" }

# 2) chzzk-vod-downloader-v2 (git 없이 zip으로)
if (-not (Test-Path "$Vendor\scripts\headless_download.py")) {
    Write-Host "[2/4] chzzk-vod-downloader-v2 다운로드 ($($UpstreamCommit.Substring(0,7)))"
    New-Item -ItemType Directory -Force vendor | Out-Null
    if (Test-Path $Vendor) { Remove-Item -Recurse -Force $Vendor }   # 이전에 받다 만 폴더 정리
    $zip = "vendor\cvd.zip"
    Invoke-WebRequest "https://github.com/honey720/chzzk-vod-downloader-v2/archive/$UpstreamCommit.zip" -OutFile $zip
    Expand-Archive $zip -DestinationPath vendor -Force
    Rename-Item "vendor\chzzk-vod-downloader-v2-$UpstreamCommit" "chzzk-vod-downloader-v2"
    Remove-Item $zip
} else { Write-Host "[2/4] 다운로더 확인됨" }

# 3) 파이썬 환경
Write-Host "[3/4] 파이썬 패키지 설치"
uv sync -p 3.13
if ($LASTEXITCODE -ne 0) { Write-Host "파이썬 패키지 설치 실패 — 인터넷 연결을 확인하고 다시 실행하세요." -ForegroundColor Red; exit 1 }

# 4) 설정 파일
if (-not (Test-Path config.toml)) {
    Copy-Item config.example.toml config.toml
    Write-Host "[4/4] config.toml 생성됨"
} else { Write-Host "[4/4] config.toml 확인됨" }

# 5) 바탕화면 바로가기
powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "make_shortcut.ps1")

Write-Host ""
Write-Host "설치 완료! 바탕화면의 '치지직→유튜브 (프로그램·권장)' 아이콘으로 여세요." -ForegroundColor Green
Write-Host "처음 열면 '시작 가이드'가 뜹니다. 자세한 설명은 폴더의 사용설명서.html 을 보세요."
Start-Process -FilePath (Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe") -ArgumentList "-m", "chzzk2yt.gui" -WorkingDirectory $PSScriptRoot
