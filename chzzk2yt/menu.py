"""번호 선택 메뉴 (menu.bat 이 실행).  python -m chzzk2yt.menu"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.toml"
IS_WIN = os.name == "nt"
EXIT_SETUP = 10  # menu.bat 에 설치/업데이트를 요청하는 종료 코드


def cls():
    os.system("cls" if IS_WIN else "clear")


def cli(*args) -> int:
    """chzzk2yt CLI 실행. Ctrl+C 는 해당 작업만 중단하고 메뉴로 돌아온다."""
    try:
        return subprocess.call([sys.executable, "-m", "chzzk2yt", "-c", str(CONFIG), *args], cwd=ROOT)
    except KeyboardInterrupt:
        print("\n중단했습니다. (중단된 영상은 다음 실행 때 다시 처리됩니다)")
        return 130


def ps(command: str) -> int:
    return subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command], cwd=ROOT)


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def open_path(p):
    if IS_WIN:
        os.startfile(str(p))  # noqa: S606
    else:
        print(p)


def _cfg() -> dict:
    try:
        return tomllib.load(open(CONFIG, "rb"))
    except Exception:  # noqa: BLE001
        return {}


def _data() -> Path:
    d = Path(_cfg().get("paths", {}).get("data_dir", "data"))
    return d if d.is_absolute() else ROOT / d


def _valid_channels(cfg=None) -> list[dict]:
    cfg = cfg if cfg is not None else _cfg()
    return [c for c in cfg.get("channels", [])
            if len(str(c.get("id", ""))) == 32 and set(str(c["id"])) != {"0"}]


def _clean_path(s: str) -> Path:
    return Path(s.strip().strip('"').strip("'"))


def channels_menu():
    from . import cfgedit, chzzk

    while True:
        chs = _valid_channels()
        print("\n현재 채널:")
        if not chs:
            print("  (없음)")
        for i, c in enumerate(chs, 1):
            name = c.get("name")
            if not name:
                try:
                    name = chzzk.channel_info(c["id"]).get("channelName", "")
                    c["name"] = name
                except Exception:  # noqa: BLE001
                    name = "?"
            print(f"  {i}. {name}  ({c['id']})  start={c.get('start', 'new')}")
        cmd = ask("\n[a] 추가  [d] 삭제  [Enter] 돌아가기: ").lower()
        if cmd == "a":
            url = ask("채널 URL 또는 ID: ")
            if not url:
                continue
            try:
                cid = chzzk.parse_channel_id(url)
                info = chzzk.channel_info(cid)
            except Exception as e:  # noqa: BLE001
                print(f"채널을 찾을 수 없습니다: {e}")
                continue
            if any(c["id"] == cid for c in chs):
                print("이미 등록된 채널입니다.")
                continue
            print(f"→ {info.get('channelName')}")
            print("시작 범위: [1] 앞으로 올라올 것만(기본)  [2] 과거 전부  [3] 특정 날짜 이후")
            k = ask("선택 [1]: ") or "1"
            start = {"1": "new", "2": "all"}.get(k)
            if k == "3":
                start = ask("날짜 (YYYY-MM-DD): ")
                try:
                    time.strptime(start, "%Y-%m-%d")
                except ValueError:
                    print("날짜 형식이 잘못됐습니다.")
                    continue
            start = start or "new"
            chs.append({"id": cid, "name": info.get("channelName", ""), "start": start})
            cfgedit.write_channels(CONFIG, chs)
            print(f"추가했습니다: {info.get('channelName')} (start={start})")
        elif cmd == "d":
            n = ask("삭제할 번호: ")
            if n.isdigit() and 1 <= int(n) <= len(chs):
                gone = chs.pop(int(n) - 1)
                cfgedit.write_channels(CONFIG, chs)
                print(f"삭제했습니다: {gone.get('name') or gone['id']}")
        else:
            return


def youtube_auth():
    import shutil

    secret = _data() / "client_secret.json"
    if not secret.exists():
        print("data\\client_secret.json 이 없습니다.")
        src = ask("구글에서 받은 client_secret JSON 파일을 이 창에 끌어다 놓고 Enter: ")
        if not src:
            return
        p = _clean_path(src)
        if not p.is_file():
            print(f"파일을 찾을 수 없습니다: {p}")
            return
        secret.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, secret)
        print("복사했습니다.")
    cli("auth-youtube")


def import_old():
    """이전 설치 폴더의 config.toml·data(토큰·쿠키·기록)를 가져온다."""
    import shutil

    src = ask("이전 chzzk2yt 폴더(config.toml 이 있는 폴더)를 끌어다 놓고 Enter: ")
    if not src:
        return
    old = _clean_path(src)
    if old.is_file():
        old = old.parent
    if old.resolve() == ROOT.resolve():
        print("지금 폴더와 같은 폴더입니다.")
        return
    if not (old / "config.toml").exists():
        print(f"config.toml 을 찾을 수 없습니다: {old}")
        return
    if CONFIG.exists():
        bak = CONFIG.with_suffix(f".toml.bak{int(time.time())}")
        shutil.copy2(CONFIG, bak)
        print(f"현재 설정 백업: {bak.name}")
    shutil.copy2(old / "config.toml", CONFIG)
    print("config.toml 가져옴")
    if (old / "data").is_dir():
        shutil.copytree(old / "data", ROOT / "data", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".lock", "*.db-wal", "*.db-shm"))
        print("data 폴더(유튜브 토큰·네이버 쿠키·처리 기록) 가져옴")
    print("완료. 12번(쿠키 확인)으로 확인하세요.")


def summary() -> str:
    """메뉴 상단 한 줄 요약 (네트워크 호출 없이 파일만 읽음)."""
    parts = []
    try:
        cfg = tomllib.load(open(CONFIG, "rb"))
    except Exception as e:  # noqa: BLE001
        return f"config.toml 읽기 실패: {e}"
    data = Path(cfg.get("paths", {}).get("data_dir", "data"))
    data = data if data.is_absolute() else ROOT / data
    chs = _valid_channels(cfg)
    parts.append(f"채널 {len(chs)}개")
    try:
        d = json.loads((data / "cookies.json").read_text(encoding="utf-8"))
        h = (time.time() - d.get("saved_at", 0)) / 3600
        parts.append(f"쿠키 갱신 {h:.0f}시간 전" if h >= 1 else "쿠키 갱신 1시간 이내")
    except Exception:  # noqa: BLE001
        parts.append("쿠키 없음")
    parts.append("유튜브 토큰 " + ("있음" if (data / "youtube_token.json").exists() else "없음"))
    try:
        import sqlite3

        con = sqlite3.connect(data / "state.db")
        rows = dict(con.execute("SELECT status, COUNT(*) FROM videos GROUP BY status").fetchall())
        con.close()
        parts.append(f"완료 {rows.get('UPLOADED', 0)} · 대기 {rows.get('NEW', 0) + rows.get('DOWNLOADED', 0)}"
                     f" · 진행 {rows.get('DOWNLOADING', 0) + rows.get('UPLOADING', 0)} · 실패 {rows.get('FAILED', 0)}"
                     f" · 보류 {rows.get('OUT', 0)}")
    except Exception:  # noqa: BLE001
        parts.append("기록 없음")
    line = "  |  ".join(parts)
    todo = []
    if not chs:
        todo.append("18 채널 추가")
    if not (data / "youtube_token.json").exists():
        todo.append("14 유튜브 인증")
    if not (data / "cookies.json").exists():
        todo.append("13 네이버 로그인")
    if todo:
        line += "\n  ▶ 초기 설정 필요: " + " → ".join(todo) + "   (이전 폴더가 있으면 20번으로 한 번에 가져오기)"
    return line


def task_status():
    ps("$t=Get-ScheduledTask -TaskName chzzk2yt -ErrorAction SilentlyContinue; "
       "if($t){$i=$t|Get-ScheduledTaskInfo; "
       "Write-Host ('상태: {0}  다음 실행: {1}  최근 실행: {2}  최근 결과: {3}' -f "
       "$t.State,$i.NextRunTime,$i.LastRunTime,$i.LastTaskResult)} else {Write-Host '자동 실행이 등록되어 있지 않습니다.'}")


def gui():
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw) if pyw.exists() else sys.executable
    subprocess.Popen([exe, "-m", "chzzk2yt.gui"], cwd=ROOT,
                     creationflags=(0x00000008 if IS_WIN else 0))  # DETACHED_PROCESS
    print("UI를 열었습니다.")


MENU = [
    ("실행", [
        ("1", "UI 열기", gui),
        ("2", "지금 실행 (스캔 → 다운로드 → 업로드)", lambda: cli("run")),
        ("3", "드라이런 - 설정대로 점검", lambda: cli("dry-run")),
        ("4", "드라이런 - 과거 영상 전체 점검", lambda: cli("dry-run", "--start", "all")),
    ]),
    ("목록·기록", [
        ("5", "목록 새로고침 (채널 영상 + 유튜브 업로드 여부 확인, 다운로드 안 함)", lambda: cli("scan")),
        ("6", "상태 보기 (진행 중/대기/실패)", lambda: cli("status")),
        ("7", "상태 보기 (전체 — 대상 아님 포함)", lambda: cli("status", "-a")),
        ("8", "다시보기 URL 수동 추가", lambda: (u := ask("URL (여러 개는 공백으로): ")) and cli("add", *u.split())),
        ("9", "'보류' 영상을 받기 목록에 추가", lambda: (u := ask("영상 번호 (여러 개는 공백으로): ")) and cli("queue", *u.split())),
        ("10", "실패 항목 모두 재시도", lambda: cli("retry", "all")),
        ("11", "특정 영상 제외", lambda: (u := ask("영상 번호/URL: ")) and cli("skip", *u.split())),
    ]),
    ("계정", [
        ("12", "쿠키 확인/갱신", lambda: cli("cookies")),
        ("13", "네이버 로그인 (최초 1회 / 쿠키 만료 시)", lambda: cli("login-naver")),
        ("14", "유튜브 인증 (최초 1회 / 토큰 만료 시)", youtube_auth),
    ]),
    ("자동 실행", [
        ("15", "자동 실행 등록/변경", lambda: ps(
            f"& '{ROOT / 'register_task.ps1'}' -Minutes {ask('몇 분마다? [30]: ') or 30}")),
        ("16", "자동 실행 해제", lambda: ps(
            "Unregister-ScheduledTask -TaskName chzzk2yt -Confirm:$false; Write-Host '해제했습니다.'")),
        ("17", "자동 실행 상태", task_status),
    ]),
    ("설정", [
        ("18", "채널 관리 (추가/삭제)", channels_menu),
        ("19", "설정 파일 열기 (메모장)", lambda: subprocess.call(["notepad", str(CONFIG)]) if IS_WIN else print(CONFIG)),
        ("20", "이전 폴더에서 설정·로그인 정보 가져오기", import_old),
    ]),
    ("기타", [
        ("21", "상태표(HTML) / 다운로드 폴더 열기", lambda: (open_path(_data() / "status.html"),
                                                   open_path(ROOT / "downloads"))),
        ("22", "로그 보기 (최근 40줄)", lambda: print("\n".join(
            (_data() / "logs" / "chzzk2yt.log").read_text(encoding="utf-8", errors="replace")
            .splitlines()[-40:]) if (_data() / "logs" / "chzzk2yt.log").exists() else "로그 없음")),
        ("23", "설치 / 업데이트 (setup.ps1)", None),
        ("28", "깃허브에서 최신 버전 받기 (설정·기록 유지)", lambda: cli("update")),
        ("24", "썸네일 일괄 적용 (이미 올린 영상 중 빠진 것)", lambda: cli("thumbs")),
        ("26", "썸네일 전부 다시 적용 (고화질 교체)", lambda: cli("thumbs", "--redo")),
        ("27", "특정 영상에 썸네일 적용", lambda: (u := ask("영상 번호 (여러 개는 공백으로): ")) and cli("thumb", *u.split())),
        ("25", "다시 업로드 (번호 입력, all = 올린 것 전부 — 유튜브에서 지운 뒤)", lambda: (u := ask("영상 번호 또는 all: ")) and cli("reupload", *u.split())),
    ]),
]


def main():
    if IS_WIN:
        os.system("title 치지직 → 유튜브")
    while True:
        cls()
        print("=" * 64)
        print("  치지직 → 유튜브 자동화")
        print("  " + summary())
        print("=" * 64)
        for sec, items in MENU:
            print(f"\n [{sec}]")
            for key, label, _ in items:
                print(f"  {key:>2}. {label}")
        print("\n   0. 종료")
        choice = ask("\n번호 선택: ")
        if choice in ("0", "q", ""):
            if choice == "":
                continue
            return 0
        action = next((a for _, items in MENU for k, _l, a in items if k == choice), "none")
        if action == "none":
            continue
        if action is None:  # 설치/업데이트는 menu.bat 이 venv 밖에서 수행
            return EXIT_SETUP
        print()
        try:
            action()
        except Exception as e:  # noqa: BLE001
            print(f"오류: {e}")
        ask("\n[Enter] 메뉴로 돌아가기")


if __name__ == "__main__":
    sys.exit(main())
