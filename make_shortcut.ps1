# 바탕화면 바로가기 생성: 프로그램 화면(GUI, 권장) + 명령창 메뉴(고급)
$root = $PSScriptRoot
$desk = [Environment]::GetFolderPath('Desktop')
$ws = New-Object -ComObject WScript.Shell

# 예전 이름의 바로가기 정리
foreach ($old in @('치지직→유튜브.lnk', '치지직→유튜브 메뉴.lnk')) {
    $p = Join-Path $desk $old
    if (Test-Path $p) { Remove-Item $p -Force }
}

$lnk = $ws.CreateShortcut((Join-Path $desk '치지직→유튜브 (프로그램·권장).lnk'))
$lnk.TargetPath = Join-Path $root '.venv\Scripts\pythonw.exe'
$lnk.Arguments = '-m chzzk2yt.gui'
$lnk.WorkingDirectory = $root
$lnk.IconLocation = "$env:SystemRoot\System32\imageres.dll,18"
$lnk.Description = '치지직 다시보기 → 유튜브 자동 업로드 (프로그램 화면)'
$lnk.Save()
Write-Host "바탕화면에 '치지직→유튜브 (프로그램·권장)' 바로가기를 만들었습니다. ← 평소엔 이걸 쓰세요"

$lnk2 = $ws.CreateShortcut((Join-Path $desk '치지직→유튜브 (명령창 메뉴).lnk'))
$lnk2.TargetPath = Join-Path $root 'menu.bat'
$lnk2.WorkingDirectory = $root
$lnk2.IconLocation = "$env:SystemRoot\System32\imageres.dll,263"
$lnk2.Description = '치지직→유튜브 번호 메뉴 (고급: 설치/업데이트, 명령창)'
$lnk2.Save()
Write-Host "바탕화면에 '치지직→유튜브 (명령창 메뉴)' 바로가기를 만들었습니다. (고급용)"
