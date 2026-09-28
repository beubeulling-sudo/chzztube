"""chzzk2yt 데스크톱 UI (PySide6).

실행:  .venv\\Scripts\\pythonw.exe -m chzzk2yt.gui   (바탕화면 바로가기가 이걸 실행)

긴 작업(실행·드라이런·로그인·인증)은 CLI를 별도 프로세스로 띄워 출력을 스트리밍한다.
→ UI가 멈추지 않고, 작업 스케줄러 실행과도 같은 잠금(.lock)을 공유해 겹치지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, QObject, QProcess, QProcessEnvironment, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPen, QPixmap, QPolygon, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
    QSystemTrayIcon, QTabBar, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from . import cfgedit, chzzk
from .db import DB, jload
from .report import LABEL

ROOT = Path(os.environ.get("CHZZK2YT_ROOT", Path(__file__).resolve().parent.parent))
CONFIG = ROOT / "config.toml"
TASK_NAME = "chzzk2yt"
IS_WIN = os.name == "nt"
STATUS_COLOR = {
    "UPLOADED": "#1a7f37", "FAILED": "#cf222e", "SKIPPED": "#8c959f", "DOWNLOADING": "#bf8700",
    "UPLOADING": "#bf8700", "DOWNLOADED": "#0969da", "NEW": "#57606a", "OUT": "#8c959f", "YT_DELETED": "#9a6700", "YT_SHORT": "#cf222e",
}
STATUS_BG = {  # 목록 칸 배경색 (상태별)
    "UPLOADED": "#dafbe1", "FAILED": "#ffebe9", "DOWNLOADING": "#fff8c5", "UPLOADING": "#fff8c5",
    "DOWNLOADED": "#ddf4ff", "NEW": "#eef1f4", "OUT": "#f6f8fa", "SKIPPED": "#f6f8fa",
    "YT_DELETED": "#fff1e5", "YT_SHORT": "#ffebe9",
}
BG_OK, BG_WARN = "#dafbe1", "#fff1e5"
NO_PLAYLIST = "none"  # 채널별 재생목록에서 '넣지 않음' (기본 재생목록도 쓰지 않음)
PRIV_KO = {"public": "공개", "unlisted": "일부 공개", "private": "비공개"}
PRIV_COLOR = {"public": "#1a7f37", "unlisted": "#9a6700", "private": "#57606a"}


def app_icon() -> QIcon:
    """빨간 둥근 사각형 + 흰 재생 삼각형 (창·트레이 아이콘)."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#e5322d"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 10, 56, 44, 12, 12)
    p.setBrush(QColor("white"))
    p.drawPolygon(QPolygon([QPoint(26, 22), QPoint(26, 42), QPoint(43, 32)]))
    p.end()
    return QIcon(pm)


def _python_console() -> str:
    """자식 프로세스는 콘솔 python으로 (pythonw는 stdout이 없어 출력이 사라진다)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def _load_cfg() -> dict:
    with open(CONFIG, "rb") as f:
        return tomllib.load(f)


def _data_dir(cfg: dict) -> Path:
    p = Path(cfg.get("paths", {}).get("data_dir", "data"))
    return p if p.is_absolute() else ROOT / p


def _dl_dir(cfg: dict) -> Path:
    p = Path(cfg.get("paths", {}).get("download_dir", "downloads"))
    return p if p.is_absolute() else ROOT / p


def _open(path_or_url):
    s = str(path_or_url)
    QDesktopServices.openUrl(QUrl(s) if s.startswith("http") else QUrl.fromLocalFile(s))


def _no_window():
    return {"creationflags": 0x08000000} if IS_WIN else {}  # CREATE_NO_WINDOW


# ── 백그라운드 작업 ────────────────────────────────────────
class _Sig(QObject):
    done = Signal(str, object)


class Job(QRunnable):
    def __init__(self, key, fn, sig):
        super().__init__()
        self.key, self.fn, self.sig = key, fn, sig

    def run(self):
        try:
            res = self.fn()
        except Exception as e:  # noqa: BLE001
            res = e
        self.sig.done.emit(self.key, res)


def check_naver(data_dir: Path):
    from .cookies import check

    try:
        d = json.loads((data_dir / "cookies.json").read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "nick": None, "saved": None}
    ok, nick = check({"NID_AUT": d.get("NID_AUT", ""), "NID_SES": d.get("NID_SES", "")})
    return {"ok": ok, "nick": nick, "saved": d.get("saved_at")}


def check_youtube(data_dir: Path):
    from . import youtube

    svc = youtube.service(data_dir)
    r = svc.channels().list(part="snippet", mine=True).execute()
    if not r.get("items"):
        raise RuntimeError("이 구글 계정에 유튜브 채널이 없습니다 — youtube.com 에서 채널을 만든 뒤 다시 ‘유튜브 인증’")
    return r["items"][0]["snippet"]["title"]


def _playlist_titles(data_dir: Path) -> dict:
    from . import youtube

    svc = youtube.service(data_dir)
    out, tok = {}, None
    while True:
        r = svc.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=tok).execute()
        out.update({it["id"]: it["snippet"]["title"] for it in r.get("items", [])})
        tok = r.get("nextPageToken")
        if not tok:
            return out


def _update_check():
    from . import config as config_mod, updater

    try:
        cfg = config_mod.load(CONFIG)
    except BaseException:  # noqa: BLE001
        cfg = None
    cur, new, need = updater.check(cfg)
    return cur, new, need, (updater.whats_new(cur, new, cfg) if need else "")


def check_task():
    if not IS_WIN:
        return None
    ps = (f"$t=Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue; "
          "if($t){$i=$t|Get-ScheduledTaskInfo; "
          "'{0}|{1}|{2}' -f $t.State,$i.NextRunTime.ToString('MM-dd HH:mm'),$i.LastRunTime.ToString('MM-dd HH:mm')}")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         timeout=20, **_no_window()).stdout.strip()
    if not out:
        return {"registered": False}
    state, nxt, last = (out.split("|") + ["", "", ""])[:3]
    return {"registered": True, "state": state, "next": nxt, "last": last}


# ── 카드 위젯 ───────────────────────────────────────────────
class Card(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)
        t = QLabel(title)
        t.setObjectName("cardTitle")
        self.value = QLabel("확인 중…")
        self.value.setObjectName("cardValue")
        self.sub = QLabel("")
        self.sub.setObjectName("cardSub")
        self.sub.setWordWrap(True)
        for w in (t, self.value, self.sub):
            lay.addWidget(w)

    def set(self, value, sub="", level="ok"):
        color = {"ok": "#1a7f37", "warn": "#bf8700", "bad": "#cf222e", "info": "#57606a"}[level]
        self.value.setText(value)
        self.value.setStyleSheet(f"color:{color}")
        self.sub.setText(sub)


# ── 채널 관리 ───────────────────────────────────────────────
class ChannelDialog(QDialog):
    def __init__(self, parent, channels, default_pl="", fetch_playlists=None, pl_titles=None, default_privacy="public"):
        super().__init__(parent)
        self.setWindowTitle("채널 관리")
        self.resize(780, 440)
        self.channels = [dict(c) for c in channels]
        self.default_pl = default_pl
        self.default_privacy = default_privacy
        self.fetch_playlists = fetch_playlists
        self.pl_titles = pl_titles if pl_titles is not None else {}
        for c in self.channels:  # 이름 없는 채널은 조회해서 채움
            if not c.get("name"):
                try:
                    c["name"] = chzzk.channel_info(c["id"]).get("channelName", "")
                except Exception:  # noqa: BLE001
                    pass
        lay = QVBoxLayout(self)
        self.list = QListWidget()
        lay.addWidget(self.list)
        row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("치지직 채널 URL 또는 32자리 ID")
        self.start = QComboBox()
        self.start.setEditable(True)
        self.start.addItems(["new", "all"])
        self.start.setToolTip('new: 지금 이후 올라오는 것만 / all: 과거 전부 / 2026-09-01: 그 날짜 이후')
        add = QPushButton("추가")
        add.clicked.connect(self._add)
        row.addWidget(self.url, 1)
        row.addWidget(QLabel("시작"))
        row.addWidget(self.start)
        row.addWidget(add)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        pl = QPushButton("선택 채널 재생목록 지정…")
        pl.setToolTip("채널마다 다른 재생목록에 넣을 때. 지정 안 하면 기본 재생목록(설정)에 들어갑니다.")
        pl.clicked.connect(self._set_playlist)
        pv = QPushButton("선택 채널 공개 범위…")
        pv.setToolTip("채널마다 공개/일부 공개/비공개를 다르게 올릴 때. 지정 안 하면 설정의 기본 공개 범위를 따릅니다.")
        pv.clicked.connect(self._set_privacy)
        rm = QPushButton("선택 채널 삭제")
        rm.clicked.connect(self._remove)
        row2.addWidget(pl)
        row2.addWidget(pv)
        row2.addWidget(rm)
        row2.addStretch(1)
        lay.addLayout(row2)
        note = QLabel("※ start 이전 영상은 목록에 '보류'로 표시됩니다. 과거 영상도 전부 올리려면 start=all, 일부만이면 표에서 우클릭 → '받기'.")
        note.setWordWrap(True)
        note.setObjectName("cardSub")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._refresh()

    def _pl_name(self, pid):
        if pid == NO_PLAYLIST:
            return "넣지 않음"
        return self.pl_titles.get(pid, pid) if pid else "없음"

    def _refresh(self):
        self.list.clear()
        for c in self.channels:
            pid = c.get("playlist_id", "")
            pl = f"재생목록: {self._pl_name(pid)}" if pid else f"재생목록: 기본({self._pl_name(self.default_pl)})"
            pv = c.get("privacy", "")
            pv = (f"공개 범위: {PRIV_KO[pv]}" if pv in PRIV_KO
                  else f"공개 범위: 기본({PRIV_KO.get(self.default_privacy, self.default_privacy)})")
            self.list.addItem(f'{c.get("name") or "(이름 없음)"}  ·  start={c.get("start", "new")}  ·  {pl}  ·  {pv}'
                              f'\n    {c["id"]}')

    def _set_privacy(self):
        i = self.list.currentRow()
        if i < 0:
            QMessageBox.information(self, "공개 범위", "먼저 목록에서 채널을 선택하세요.")
            return
        c = self.channels[i]
        keys = ["", "public", "unlisted", "private"]
        labels = [f"기본 공개 범위 따르기 (지금: {PRIV_KO.get(self.default_privacy, self.default_privacy)})",
                  "공개", "일부 공개 (링크가 있는 사람만)", "비공개 (나만)"]
        cur = keys.index(c.get("privacy", "")) if c.get("privacy", "") in keys else 0
        choice, ok = QInputDialog.getItem(self, f"{c.get('name') or '채널'} 공개 범위",
                                          "이 채널 영상을 어떤 공개 범위로 올릴까요?\n"
                                          "(이미 올라간 영상은 바뀌지 않습니다. 목록에서 우클릭 → 공개 범위로 바꿀 수 있어요)",
                                          labels, cur, False)
        if ok:
            c["privacy"] = keys[labels.index(choice)]
            self._refresh()
            self.list.setCurrentRow(i)

    def _set_playlist(self):
        i = self.list.currentRow()
        if i < 0:
            QMessageBox.information(self, "재생목록", "먼저 목록에서 채널을 선택하세요.")
            return
        if not self.fetch_playlists:
            return
        items = self.fetch_playlists()
        if items is None:
            return
        c = self.channels[i]
        dflt = self._pl_name(self.default_pl) if self.default_pl else "없음 — 재생목록에 넣지 않음"
        dlg = PlaylistDialog(self, items, c.get("playlist_id", ""), "top",
                             first_label=f"기본 재생목록 따르기 (지금: {dflt})",
                             extra=[(NO_PLAYLIST, "재생목록에 넣지 않음 (이 채널만)")],
                             show_position=False, title=f"{c.get('name') or '채널'} 재생목록")
        if dlg.exec():
            c["playlist_id"] = dlg.value()[0]
            self._refresh()
            self.list.setCurrentRow(i)

    def _add(self):
        try:
            cid = chzzk.parse_channel_id(self.url.text())
            info = chzzk.channel_info(cid)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "채널 추가", f"채널을 찾을 수 없습니다.\n{e}")
            return
        if any(c["id"] == cid for c in self.channels):
            QMessageBox.information(self, "채널 추가", "이미 등록된 채널입니다.")
            return
        st = self.start.currentText().strip() or "new"
        if st not in ("new", "all") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", st):
            QMessageBox.warning(self, "채널 추가", "시작은 new / all / YYYY-MM-DD 중 하나여야 합니다.")
            return
        self.channels.append({"id": cid, "name": info.get("channelName", ""), "start": st,
                              "include_keywords": [], "exclude_keywords": [], "playlist_id": "", "privacy": ""})
        self.url.clear()
        self._refresh()

    def _remove(self):
        i = self.list.currentRow()
        if i >= 0:
            self.channels.pop(i)
            self._refresh()


# ── 설정 ───────────────────────────────────────────────────
class SettingsDialog(QDialog):
    FIELDS = [
        ("download", "resolution", "선호 해상도", "int", (144, 2160)),
        ("download", "min_age_minutes", "게시 후 대기(분)", "int", (0, 1440)),
        ("download", "max_attempts", "재시도 횟수", "int", (1, 10)),
        ("upload", "enabled", "유튜브 업로드", "bool", None),
        ("upload", "privacy", "기본 공개 범위 (채널별 설정이 우선)", "choice", ["private", "unlisted", "public"]),
        ("upload", "delete_after_upload", "업로드 후 로컬 삭제", "bool", None),
        ("upload", "set_thumbnail", "치지직 썸네일 적용", "bool", None),
        ("upload", "oauth_testing", "구글 앱 테스트 중 (7일마다 재인증)", "bool", None),
        ("upload", "playlist_id", "재생목록 ID", "str", None),
        ("upload", "title_template", "제목 형식", "str", None),
        ("cookies", "mode", "쿠키 방식", "choice", ["browser", "manual", "none"]),
        ("cookies", "browser_channel", "브라우저", "choice", ["msedge", "chrome", "chromium"]),
        ("cookies", "headless", "쿠키 갱신 시 창 숨김", "bool", None),
    ]

    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.setWindowTitle("설정")
        self.resize(520, 0)
        form = QFormLayout(self)
        self.widgets = {}
        for sec, key, label, kind, opt in self.FIELDS:
            cur = cfg.get(sec, {}).get(key)
            if kind == "int":
                w = QSpinBox()
                w.setRange(*opt)
                w.setValue(int(cur if cur is not None else opt[0]))
            elif kind == "bool":
                w = QCheckBox()
                w.setChecked(bool(cur))
            elif kind == "choice":
                w = QComboBox()
                w.addItems(opt)
                if cur in opt:
                    w.setCurrentText(cur)
            else:
                w = QLineEdit("" if cur is None else str(cur))
            self.widgets[(sec, key, kind)] = w
            form.addRow(label, w)
        tip = QLabel("변경 사항은 다음 실행부터 적용됩니다. 나머지 항목은 '설정 파일 열기'에서 수정하세요.")
        tip.setObjectName("cardSub")
        tip.setWordWrap(True)
        form.addRow(tip)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def values(self):
        out = []
        for (sec, key, kind), w in self.widgets.items():
            v = (w.value() if kind == "int" else w.isChecked() if kind == "bool"
                 else w.currentText() if kind == "choice" else w.text())
            out.append((sec, key, v))
        return out


# ── 시작 가이드 ─────────────────────────────────────────────
MANUAL = ROOT / "사용설명서.html"


def _secret_ok(data_dir: Path) -> tuple[bool, str]:
    p = data_dir / "client_secret.json"
    if not p.exists():
        return False, "아직 없음"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False, "파일이 손상됨 — 다시 받아 넣으세요"
    if "installed" not in d:
        return False, "종류가 '데스크톱 앱'이 아님 — OAuth 클라이언트를 '데스크톱 앱'으로 다시 만드세요"
    return True, "넣음"


class SetupGuide(QDialog):
    """처음 쓰는 사람용 단계별 체크리스트. 각 단계의 상태를 보여주고 바로 실행할 버튼을 둔다."""

    def __init__(self, main: "Main"):
        super().__init__(main)
        self.m = main
        self.setWindowTitle("시작 가이드")
        self.resize(720, 560)
        lay = QVBoxLayout(self)
        head = QLabel("위에서부터 순서대로 하면 됩니다. ✅ 가 되면 다음 단계로. (필수 1~3, 7 · 권장 6, 8)")
        head.setWordWrap(True)
        lay.addWidget(head)
        self.grid = QGridLayout()
        self.grid.setColumnStretch(1, 1)
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(12)
        lay.addLayout(self.grid)
        lay.addStretch(1)
        low = QHBoxLayout()
        low.addWidget(self.m._btn("사용설명서 열기", lambda: _open(MANUAL)))
        low.addStretch(1)
        self.again = QCheckBox("다음에 켤 때도 보기")
        self.again.setChecked(True)
        low.addWidget(self.again)
        close = QPushButton("닫기")
        close.clicked.connect(self.close)
        low.addWidget(close)
        lay.addLayout(low)
        self.rows = []
        self.refresh()

    def _steps(self):
        m, cfg, dd = self.m, self.m.cfg, self.m.data_dir
        chs = [c for c in cfg.get("channels", []) if len(str(c.get("id", ""))) == 32]
        sec_ok, sec_msg = _secret_ok(dd)
        tok = (dd / "youtube_token.json").exists()
        pl = cfg.get("upload", {}).get("playlist_id", "")
        naver = (dd / "cookies.json").exists()
        task = getattr(m, "_task_registered", None)
        try:
            n_out = len(m.db.by_status("OUT"))
        except Exception:  # noqa: BLE001
            n_out = 0
        priv = {"private": "비공개", "unlisted": "일부 공개", "public": "공개"}.get(
            cfg.get("upload", {}).get("privacy", "public"), "?")
        return [
            ("1. 올릴 치지직 채널 추가",
             f"등록됨: {', '.join(c.get('name') or c['id'][:8] for c in chs)}" if chs
             else "치지직 채널 주소를 붙여넣어 추가합니다.",
             bool(chs), [("채널 관리…", m.edit_channels)]),
            ("2. 구글 API 파일 넣기 (client_secret.json)",
             f"{sec_msg}. 유튜브에 올리려면 본인 구글 클라우드에서 만든 파일이 필요합니다 (사용설명서 3장, 10분).",
             sec_ok, [("파일 선택…", m.pick_client_secret), ("만드는 방법", lambda: _open(MANUAL))]),
            ("3. 유튜브 인증",
             "브라우저가 열리면 올릴 채널의 구글 계정으로 로그인 → 'Google에서 확인하지 않은 앱' 화면에서 '계속' → 모두 허용. "
             "7일마다 다시 해야 합니다(만료일은 위쪽 '유튜브 계정' 칸에 표시)."
             + (" (완료)" if tok else ""),
             tok, [("유튜브 인증", m.auth_youtube)]),
            ("4. 업로드 방식 확인 (공개 범위 · 재생목록)",
             f"공개 범위: {priv}. 기본 재생목록: {self._pl(pl)}."
             + (" 채널별: " + " · ".join(f"{c.get('name') or c['id'][:6]} → "
                                         + (self._pl(c.get('playlist_id')) if c.get('playlist_id') else "기본")
                                         for c in chs) if len(chs) > 1 else "")
             + (" ⚠ 채널이 여러 개인데 모두 같은 재생목록으로 들어갑니다 — 나누려면 '채널별 재생목록…'"
                if len(chs) > 1 and not any(c.get("playlist_id") for c in chs) else ""),
             None, [("기본 재생목록…", m.pick_playlist), ("채널별 재생목록…", m.edit_channels), ("설정…", m.edit_settings)]),
            ("5. (선택) 네이버 로그인 — 19+ 영상도 받을 때",
             "성인 인증된 네이버 계정으로 한 번 로그인해 두면 이후 자동 유지됩니다. 19+ 방송이 없으면 건너뛰세요.",
             naver, [("네이버 로그인", lambda: m.start_cli("네이버 로그인", ["login-naver"]))]),
            ("6. (권장) 드라이런으로 점검",
             "실제로 받거나 올리지 않고 목록·권한·경로가 정상인지 확인합니다.",
             None, [("드라이런", lambda: m.start_cli("드라이런", ["dry-run"]))]),
            ("7. 자동 실행 켜기",
             "30분마다 새 다시보기를 확인해 받고 올립니다. PC가 켜져 있고 윈도우에 로그인돼 있어야 합니다.",
             bool(task), [("자동 실행 등록", m.toggle_task)] if not task else []),
            ("8. (권장) 채널 등록 전 과거 방송도 올리기",
             (f"보류 중인 과거 방송 {n_out}개. " if n_out else "")
             + "오래된 순서대로 올라가도록 '어디서부터' 올릴지 한 번에 고르세요. (하나씩 '받기' 하면 순서가 꼬일 수 있음)",
             None, [("과거 방송 가져오기…", m.import_past)]),
        ]

    def _pl(self, pid):
        if pid == NO_PLAYLIST:
            return "넣지 않음"
        return (self.m._pl_titles.get(pid, pid) if pid else "없음")

    def refresh(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        for i, (title, desc, done, btns) in enumerate(self._steps()):
            mark = QLabel("✅" if done else ("•" if done is None else "⬜"))
            mark.setStyleSheet("font-size:18px")
            txt = QLabel(f"<b>{title}</b><br><span style='color:#57606a'>{desc}</span>")
            txt.setWordWrap(True)
            box = QWidget()
            bl = QHBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            for label, fn in btns:
                bl.addWidget(self.m._btn(label, fn))
            self.grid.addWidget(mark, i, 0, Qt.AlignTop)
            self.grid.addWidget(txt, i, 1)
            self.grid.addWidget(box, i, 2, Qt.AlignTop)

    def essentials_done(self) -> bool:
        st = self._steps()
        return all(s[2] for s in st[:3])

    def closeEvent(self, e):
        try:
            self.m.db.set_kv("guide_hide", 0 if self.again.isChecked() else 1)
        except Exception:  # noqa: BLE001
            pass
        e.accept()


class PlaylistDialog(QDialog):
    def __init__(self, parent, items, current, position, first_label="넣지 않음", show_position=True,
                 title="재생목록 선택", extra=()):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(460, 380)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("업로드한 영상을 넣을 재생목록" + ("" if not show_position else " (기본: 채널별 지정이 없을 때)")))
        self.list = QListWidget()
        self.ids = [""] + [i for i, _ in extra] + [i for i, _ in items]
        self.list.addItem(first_label)
        for _, t in extra:
            self.list.addItem(t)
        for _, t in items:
            self.list.addItem(t)
        self.list.setCurrentRow(self.ids.index(current) if current in self.ids else 0)
        lay.addWidget(self.list, 1)
        self.pos = QComboBox()
        self.pos.addItems(["맨 위 (최신이 위)", "맨 아래 (최신이 아래)"])
        self.pos.setCurrentIndex(0 if position == "top" else 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("새 영상 위치"))
        row.addWidget(self.pos, 1)
        if show_position:
            lay.addLayout(row)
        else:
            self.pos.hide()
        note = QLabel("※ 유튜브 스튜디오에서 재생목록 정렬이 '수동'일 때만 위치가 적용됩니다. 자동 정렬이면 유튜브 규칙대로 놓입니다.\n"
                      "※ 새 재생목록은 유튜브 스튜디오 → 콘텐츠 → 재생목록 → '새 재생목록'으로 먼저 만든 뒤 여기서 고르세요.")
        note.setWordWrap(True)
        note.setObjectName("cardSub")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def value(self):
        return self.ids[self.list.currentRow()], ("top" if self.pos.currentIndex() == 0 else "bottom")


class PastDialog(QDialog):
    """채널 등록 전(보류) 방송을 '이 방송부터 최신까지' 한 번에 대기열로. 방송일 순서가 유지된다."""

    def __init__(self, parent, rows, uploaded_ts):
        super().__init__(parent)
        self.setWindowTitle("과거 방송 가져오기")
        self.resize(720, 520)
        self.rows = rows  # 오래된 순
        self.uploaded_ts = uploaded_ts
        lay = QVBoxLayout(self)
        t = QLabel("올리기 시작할 <b>가장 오래된 방송</b>을 하나 고르세요. 그 방송부터 최신 방송까지 전부 "
                   "<b>방송일 순서대로</b> 받아서 올립니다. 목록은 오래된 순입니다.")
        t.setWordWrap(True)
        lay.addWidget(t)
        self.list = QListWidget()
        for r in rows:
            d = r["duration"] or 0
            self.list.addItem(f'{(r["publish_date"] or "")[:16]}   {d // 3600}:{d % 3600 // 60:02d}   {r["title"]}')
        self.list.currentRowChanged.connect(self._changed)
        lay.addWidget(self.list, 1)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("대기열에 추가")
        self.ok = bb.button(QDialogButtonBox.Ok)
        self.ok.setEnabled(False)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _changed(self, i):
        if i < 0:
            return
        sel = self.rows[i:]
        hrs = sum((r["duration"] or 0) for r in sel) / 3600
        msg = (f"{len(sel)}개 방송 (약 {hrs:.0f}시간, 디스크는 한 편씩만 사용). "
               f"한 편에 30분~1시간 정도라 다 올리려면 오래 걸립니다. 하루 업로드 한도에 걸리면 다음 날 이어서 올립니다.")
        if any(ts and ts > (self.rows[i]["publish_ts"] or 0) for ts in self.uploaded_ts):
            msg += ("<br><span style='color:#bf8700'>⚠ 이미 올라간 영상보다 오래된 방송이 포함돼 있습니다. "
                    "유튜브 업로드 순서는 그 영상보다 뒤가 됩니다. (재생목록 정렬이 '수동'이면 재생목록 안에서는 날짜순 자리에 넣습니다)</span>")
        self.info.setText(msg)
        self.ok.setEnabled(True)

    def selected(self):
        i = self.list.currentRow()
        return [r["video_no"] for r in self.rows[i:]] if i >= 0 else []


# ── 제목 표시줄 (─ □ ✕ 옆에 '트레이로' 버튼을 두기 위해 직접 그림) ─────────────────
def _cap_icon(kind: str, color: str = "#1f2328") -> QIcon:
    """윈도우 11 모양의 선 아이콘 (40px에 그려 작게 표시 → 선명)."""
    pm = QPixmap(40, 40)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(color), 2.4))
    if kind == "min":
        p.drawLine(8, 20, 32, 20)
    elif kind == "max":
        p.drawRoundedRect(QRectF(9, 9, 22, 22), 3, 3)
    elif kind == "restore":
        p.drawRoundedRect(QRectF(8, 13, 19, 19), 3, 3)
        p.drawLine(13, 8, 29, 8)
        p.drawLine(32, 11, 32, 27)
    elif kind == "close":
        p.drawLine(9, 9, 31, 31)
        p.drawLine(31, 9, 9, 31)
    elif kind == "tray":  # 트레이로: 아래 화살표 + 받침
        p.drawLine(20, 5, 20, 25)
        p.drawLine(12, 17, 20, 25)
        p.drawLine(28, 17, 20, 25)
        p.drawLine(7, 33, 33, 33)
    p.end()
    return QIcon(pm)


class TitleBar(QWidget):
    H = 34

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setObjectName("titleBar")
        self.setFixedHeight(self.H)
        self.setAttribute(Qt.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 0, 0)
        lay.setSpacing(0)
        ic = QLabel()
        ic.setPixmap(app_icon().pixmap(18, 18))
        lay.addWidget(ic)
        lay.addSpacing(8)
        self.title = QLabel(win.windowTitle())
        self.title.setObjectName("titleText")
        lay.addWidget(self.title)
        lay.addStretch(1)
        self.b_tray = self._cap("tray", "트레이로 최소화 (시계 옆 아이콘으로 숨기기)", win.to_tray)
        self.b_min = self._cap("min", "최소화", win.showMinimized)
        self.b_max = self._cap("max", "최대화", self.toggle_max)
        self.b_close = self._cap("close", "닫기", win.close)
        self.b_close.setObjectName("capClose")
        self.b_close.installEventFilter(self)  # 닫기 버튼에 마우스를 올리면 흰 X
        for b in (self.b_tray, self.b_min, self.b_max, self.b_close):
            lay.addWidget(b)

    def _cap(self, kind, tip, fn):
        b = QToolButton(self)
        b.setObjectName("capBtn")
        b.setIcon(_cap_icon(kind))
        b.setIconSize(QSize(12, 12))
        b.setFixedSize(46, self.H)
        b.setToolTip(tip)
        b.clicked.connect(fn)
        return b

    def eventFilter(self, obj, ev):
        if obj is self.b_close and ev.type() in (QEvent.Enter, QEvent.Leave):
            self.b_close.setIcon(_cap_icon("close", "#ffffff" if ev.type() == QEvent.Enter else "#1f2328"))
        return False

    def toggle_max(self):
        if self.win.isMaximized():
            self.win.showNormal()
        else:
            self.win.showMaximized()

    def sync(self):
        m = self.win.isMaximized()
        self.b_max.setIcon(_cap_icon("restore" if m else "max"))
        self.b_max.setToolTip("이전 크기로" if m else "최대화")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.win.windowHandle():
            self.win.windowHandle().startSystemMove()  # 끌어서 화면 가장자리에 붙이기(스냅)도 그대로 동작

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.toggle_max()


# ── 메인 창 ─────────────────────────────────────────────────
class Main(QMainWindow):
    COLS = ["썸네일 · 번호", "채널", "제목", "방송일", "길이", "상태", "화질", "유튜브 업로드", "공개", "썸네일", "메모"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("치지직 → 유튜브")
        self.setWindowIcon(app_icon())
        # 기본 창 틀 대신 직접 그린 제목 표시줄 (─ □ ✕ 옆 '트레이로' 버튼). 작업 표시줄 클릭 최소화는 유지
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowMinMaxButtonsHint)
        self.titlebar = TitleBar(self)
        self.setMenuWidget(self.titlebar)
        self.setObjectName("mainWin")
        QApplication.instance().installEventFilter(self)  # 가장자리 끌어서 크기 조절
        self._quitting = False
        self._last_auth_warn = 0.0
        self._setup_tray()
        self.resize(1180, 780)
        self.pool = QThreadPool.globalInstance()
        self._pl_titles: dict[str, str] = {}
        self.sig = _Sig()
        self.sig.done.connect(self._on_job)
        self.proc: QProcess | None = None
        self.proc_name = ""
        self._last_log_size = -1

        central = QWidget()
        central.setMouseTracking(True)
        self.titlebar.setMouseTracking(True)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # 상태 카드
        cards = QGridLayout()
        cards.setSpacing(10)
        self.c_naver = Card("네이버 쿠키")
        self.c_yt = Card("유튜브 계정")
        self.c_task = Card("자동 실행")
        self.c_queue = Card("처리 현황")
        for i, c in enumerate((self.c_naver, self.c_yt, self.c_task, self.c_queue)):
            cards.addWidget(c, 0, i)
        root.addLayout(cards)

        # 깃허브 주소 (업데이트 버튼 위, 작게)
        from .updater import DEFAULT_REPO as repo

        gh = QLabel(f'<a href="https://github.com/{repo}" style="color:#8a8f98; text-decoration:none;">'
                    f'github.com/{repo}</a>')
        gh.setOpenExternalLinks(True)
        gh.setToolTip("깃허브 저장소 열기 (새 버전·사용설명서)")
        gh.setStyleSheet("font-size: 11px;")
        ghrow = QHBoxLayout()
        ghrow.setContentsMargins(0, 0, 2, 0)
        ghrow.addStretch(1)
        ghrow.addWidget(gh)
        root.addLayout(ghrow)
        root.addSpacing(-8)  # 버튼 줄에 붙여 둔다

        # 버튼
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.b_run = self._btn("▶ 지금 실행", lambda: self.start_cli("실행", ["run"]), primary=True)
        self.b_dry = QToolButton()
        self.b_dry.setText("드라이런")
        self.b_dry.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(self)
        m.addAction("설정대로 점검", lambda: self.start_cli("드라이런", ["dry-run"]))
        m.addAction("과거 영상 전체 점검 (start=all)", lambda: self.start_cli("드라이런", ["dry-run", "--start", "all"]))
        self.b_dry.setMenu(m)
        self.b_stop = self._btn("■ 중지", self.stop_cli)
        self.b_stop.setEnabled(False)
        bar.addWidget(self.b_run)
        bar.addWidget(self.b_dry)
        bar.addWidget(self.b_stop)
        bar.addWidget(self._btn("목록 새로고침", lambda: self.start_cli("목록 새로고침", ["scan"])))
        bar.addSpacing(16)
        bar.addWidget(self._btn("쿠키 갱신", lambda: self.start_cli("쿠키 갱신", ["cookies"])))
        bar.addWidget(self._btn("네이버 로그인", lambda: self.start_cli("네이버 로그인", ["login-naver"])))
        bar.addWidget(self._btn("유튜브 인증", self.auth_youtube))
        bar.addSpacing(16)
        self.b_task = self._btn("자동 실행 등록", self.toggle_task)
        bar.addWidget(self.b_task)
        bar.addStretch(1)
        self.b_update = self._btn("업데이트", self.do_update)
        self.b_update.setToolTip("깃허브에서 최신 버전 확인·받기 (설정·기록·인증은 그대로)")
        bar.addWidget(self.b_update)
        bar.addWidget(self._btn("시작 가이드", self.open_guide))
        more = QToolButton()
        more.setText("관리")
        more.setPopupMode(QToolButton.InstantPopup)
        mm = QMenu(self)
        mm.addAction("채널 관리…", self.edit_channels)
        mm.addAction("재생목록 선택…", self.pick_playlist)
        mm.addAction("과거 방송 가져오기 (순서대로)…", self.import_past)
        mm.addAction("설정…", self.edit_settings)
        mm.addAction("구글 API 파일(client_secret.json) 넣기…", self.pick_client_secret)
        mm.addAction("사용설명서", lambda: _open(MANUAL))
        mm.addAction("바탕화면 바로가기 다시 만들기", self.make_shortcuts)
        mm.addSeparator()
        mm.addAction("설정 파일 열기", lambda: _open(CONFIG))
        mm.addAction("다운로드 폴더 열기", lambda: _open(_dl_dir(self.cfg)))
        mm.addAction("상태표(HTML) 열기", lambda: _open(_data_dir(self.cfg) / "status.html"))
        mm.addAction("로그 폴더 열기", lambda: _open(_data_dir(self.cfg) / "logs"))
        mm.addSeparator()
        mm.addAction("실패 항목 모두 재시도", lambda: self.start_cli("재시도", ["retry", "all"]))
        mm.addAction("썸네일 일괄 적용 (빠진 것만)", lambda: self.start_cli("썸네일 적용", ["thumbs"]))
        mm.addAction("썸네일 전부 다시 적용 (고화질 교체)", lambda: self.start_cli("썸네일 재적용", ["thumbs", "--redo"]))
        mm.addAction("전부 다시 올리기 (유튜브에서 지운 뒤)…", self.reupload_all)
        more.setMenu(mm)
        bar.addWidget(more)
        root.addLayout(bar)

        # URL 추가
        addrow = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("다시보기 URL을 붙여넣어 수동 추가 (예: https://chzzk.naver.com/video/12345678) — 여러 개는 공백으로 구분")
        self.url.returnPressed.connect(self.add_urls)
        addrow.addWidget(self.url, 1)
        addrow.addWidget(self._btn("URL 추가", self.add_urls))
        self.filter = QComboBox()
        self.filter.addItems(["전체", "진행 중/대기", "실패", "업로드완료", "보류(미업로드)"])
        self.filter.currentIndexChanged.connect(lambda: self.refresh_table(force=True))
        addrow.addWidget(QLabel("보기"))
        addrow.addWidget(self.filter)
        root.addLayout(addrow)

        # 채널 탭 (채널마다 다른 재생목록에 올릴 때 헷갈리지 않게)
        self.chtabs = QTabBar()
        self.chtabs.setExpanding(False)
        self.chtabs.setDrawBase(False)
        self.chtabs.currentChanged.connect(lambda _i: self.refresh_table(force=True))
        self._tab_key = None
        self.pl_label = QLabel("")
        self.pl_label.setObjectName("cardSub")
        tabrow = QHBoxLayout()
        tabrow.addWidget(self.chtabs)
        tabrow.addSpacing(12)
        tabrow.addWidget(self.pl_label)
        tabrow.addStretch(1)
        root.addLayout(tabrow)

        # 표 + 로그
        split = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.row_menu)
        self.table.cellDoubleClicked.connect(self.row_open)
        self.table.setIconSize(QSize(96, 54))  # 치지직 썸네일 미리보기
        self.table.verticalHeader().setDefaultSectionSize(60)
        self._icons: dict[str, QIcon] = {}
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setMinimumSectionSize(40)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(10, QHeaderView.Interactive)
        self.table.setColumnWidth(10, 240)
        split.addWidget(self.table)

        self.tabs = QTabWidget()
        mono = QFont("Consolas" if IS_WIN else "Monospace", 9)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFont(mono)
        self.out.setMaximumBlockCount(5000)
        self.logv = QPlainTextEdit()
        self.logv.setReadOnly(True)
        self.logv.setFont(mono)
        self.tabs.addTab(self.out, "작업 출력")
        self.tabs.addTab(self.logv, "로그 (자동 실행 포함)")
        split.addWidget(self.tabs)
        split.setSizes([470, 260])
        root.addWidget(split, 1)

        self.statusBar().showMessage("준비")
        self.reload_cfg()
        self.refresh_all()

        self.t_fast = QTimer(self, interval=3000, timeout=self._tick)
        self.t_fast.start()
        self.t_slow = QTimer(self, interval=5 * 60 * 1000, timeout=self.refresh_status)
        self.t_slow.start()
        # 창을 열면 채널 목록을 한 번 불러온다 (다운로드는 하지 않음)
        QTimer.singleShot(800, lambda: self.cfg.get("channels") and self.start_cli("목록 새로고침", ["scan"]))
        self.guide = None
        QTimer.singleShot(400, self._maybe_guide)

    # 공통
    def _btn(self, text, fn, primary=False):
        b = QPushButton(text)
        if primary:
            b.setObjectName("primary")
        b.clicked.connect(fn)
        return b

    def reload_cfg(self):
        try:
            self.cfg = _load_cfg()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "설정 오류", f"config.toml 을 읽을 수 없습니다.\n{e}")
            self.cfg = {}
        self.data_dir = _data_dir(self.cfg)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        old = getattr(self, "db", None)
        self.db = DB(self.data_dir / "state.db")
        try:
            self._pl_titles.update(json.loads(self.db.get_kv("playlist_titles") or "{}"))
        except Exception:  # noqa: BLE001
            pass
        if old is not None:
            try:
                old.conn.close()
            except Exception:  # noqa: BLE001
                pass
        if getattr(self, "guide", None) and self.guide.isVisible():
            self.guide.refresh()

    def _submit(self, key, fn):
        self.pool.start(Job(key, fn, self.sig))

    def refresh_all(self):
        self.refresh_table(force=True)
        self.refresh_status()

    def refresh_status(self):
        self.c_naver.set("확인 중…", "", "info")
        self.c_yt.set("확인 중…", "", "info")
        mode = self.cfg.get("cookies", {}).get("mode", "browser")
        if mode == "none":
            self.c_naver.set("사용 안 함", "연령제한·멤버십 영상은 받지 않음", "info")
        else:
            self._submit("naver", lambda: check_naver(self.data_dir))
        if self.cfg.get("upload", {}).get("enabled", True):
            self._submit("yt", lambda: check_youtube(self.data_dir))
        else:
            self.c_yt.set("업로드 꺼짐", "설정에서 켤 수 있음", "info")
        self._submit("task", check_task)
        # 켤 때 한 번만 확인하면, 그때 실패하거나 켜 둔 사이 새 버전이 나와도 표시가 안 뜬다.
        # 성공하면 30분마다, 실패하면 다음 상태 새로고침(5분) 때 다시 확인한다.
        if time.time() - getattr(self, "_upd_at", 0) > (30 * 60 if getattr(self, "_upd_ok", False) else 4 * 60):
            self._upd_at = time.time()
            self._submit("update", _update_check)

    def _on_job(self, key, res):
        if key == "naver":
            if not isinstance(res, Exception) and not res["ok"] and not res.get("saved") \
                    and not (self.data_dir / "naver_profile").exists():
                self.c_naver.set("로그인 안 함 (선택)", "19+ 영상을 받을 때만 ‘네이버 로그인’ 필요", "info")
            elif isinstance(res, Exception) or not res["ok"]:
                self.c_naver.set("만료 / 없음", "‘네이버 로그인’을 눌러 다시 로그인하세요", "bad")
            else:
                ago = ""
                if res.get("saved"):
                    mins = int((time.time() - res["saved"]) / 60)
                    ago = f"마지막 갱신 {mins // 60}시간 {mins % 60}분 전" if mins >= 60 else f"마지막 갱신 {mins}분 전"
                self.c_naver.set(f"유효 · {res['nick']}", ago, "ok")
        elif key == "yt":
            if isinstance(res, Exception):
                if not (self.data_dir / "youtube_token.json").exists():
                    self.c_yt.set("설정 필요", "‘시작 가이드’ 2~3단계: 구글 API 파일 → 유튜브 인증", "warn")
                else:
                    self.c_yt.set("인증 필요", str(res)[:120], "bad")
                    self._auth_reminder("인증 만료 또는 오류")
            else:
                q = self.db.get_kv("quota_block")
                from .pipeline import quota_day

                blocked = q == quota_day()
                from .youtube import expiry_text

                exp_txt, exp_state = expiry_text(self.data_dir, bool(self.cfg.get("upload", {}).get("oauth_testing", True)))
                sub = ("오늘 업로드 한도 초과 — 내일 재개" if blocked else "업로드 가능") + " · " + exp_txt
                state = "bad" if exp_state == "bad" else ("warn" if blocked or exp_state == "warn" else "ok")
                self.c_yt.set(res, sub, state)
                if not getattr(self, "_pl_loaded", False):  # 탭에 재생목록 이름을 보여주려고 한 번 불러옴
                    self._pl_loaded = True
                    self._submit("pltitles", lambda: _playlist_titles(self.data_dir))
                if exp_state in ("warn", "bad"):
                    self._auth_reminder(exp_txt)
        elif key == "update":
            self._upd_ok = isinstance(res, tuple)
            if isinstance(res, tuple) and res[2]:
                first = res[1] != getattr(self, "_new_ver", None)  # 같은 버전 알림은 한 번만
                self._new_ver = res[1]
                self._new_notes = res[3]
                if res[3]:
                    self.b_update.setToolTip(f"새 버전 v{res[1]} 내용\n\n{res[3]}")
                self.b_update.setText(f"업데이트 (v{res[1]}) ●")
                self.b_update.setObjectName("primary")
                self.b_update.style().unpolish(self.b_update)
                self.b_update.style().polish(self.b_update)
                if self.tray and first:
                    self.tray.showMessage("치지직 → 유튜브", f"새 버전 v{res[1]}이 있습니다. '업데이트' 버튼을 누르세요.",
                                          QSystemTrayIcon.Information, 8000)
        elif key == "pltitles":
            if isinstance(res, dict) and res:
                self._pl_titles.update(res)
                try:
                    self.db.set_kv("playlist_titles", json.dumps(self._pl_titles, ensure_ascii=False))
                except Exception:  # noqa: BLE001
                    pass
                self._tab_key = None
                self.refresh_table(force=True)
        elif key == "task":
            if res is None:
                self.c_task.set("윈도우 전용", "", "info")
                self.b_task.setEnabled(False)
            elif isinstance(res, Exception):
                self.c_task.set("확인 실패", str(res)[:120], "warn")
            elif not res["registered"]:
                self._task_registered = False
                self.c_task.set("꺼짐", "‘자동 실행 등록’으로 30분마다 실행", "warn")
                self.b_task.setText("자동 실행 등록")
            else:
                self._task_registered = True
                st = "켜짐" if res["state"] != "Disabled" else "비활성"
                self.c_task.set(st, f"다음 {res['next']} · 최근 {res['last']}", "ok" if st == "켜짐" else "warn")
                self.b_task.setText("자동 실행 해제")

        if getattr(self, "guide", None) and self.guide.isVisible():
            self.guide.refresh()

    def _tick(self):
        self.refresh_table()
        self.refresh_log()

    # 표
    def _privacy_of_channel(self, cid):
        from .pipeline import privacy_of

        return privacy_of(self.cfg, {"privacy": None, "status": "NEW", "channel_id": cid})

    def _playlist_of(self, cid):
        up = self.cfg.get("upload", {})
        for c in self.cfg.get("channels", []):
            if c.get("id") == cid and c.get("playlist_id"):
                return ("" if c["playlist_id"] == NO_PLAYLIST else c["playlist_id"]), False
        return up.get("playlist_id", ""), True

    def _sync_tabs(self, rows):
        """목록의 채널별 탭을 맞추고, 선택된 채널 ID(전체면 "")를 돌려준다."""
        order = [c.get("id") for c in self.cfg.get("channels", [])]
        names, counts = {}, {}
        for r in rows:
            names.setdefault(r["channel_id"], r["channel_name"] or r["channel_id"][:8])
            counts[r["channel_id"]] = counts.get(r["channel_id"], 0) + 1
        for c in self.cfg.get("channels", []):
            names.setdefault(c.get("id"), c.get("name") or str(c.get("id"))[:8])
        cids = sorted(names, key=lambda x: (order.index(x) if x in order else 999, names[x]))
        key = (tuple(cids), tuple(counts.get(c, 0) for c in cids), len(rows), tuple(sorted(self._pl_titles.items())),
               tuple((c.get("id"), c.get("playlist_id")) for c in self.cfg.get("channels", [])),
               self.cfg.get("upload", {}).get("playlist_id", ""))
        cur = self.chtabs.tabData(self.chtabs.currentIndex()) if self.chtabs.count() else ""
        if key != self._tab_key:
            self._tab_key = key
            self.chtabs.blockSignals(True)
            while self.chtabs.count():
                self.chtabs.removeTab(0)
            self.chtabs.addTab(f"전체 ({len(rows)})")
            self.chtabs.setTabData(0, "")
            for c in cids:
                i = self.chtabs.addTab(f"{names[c]} ({counts.get(c, 0)})" + ("" if c in order else " · 감시 안 함"))
                self.chtabs.setTabData(i, c)
                pid, default = self._playlist_of(c)
                pl = self._pl_titles.get(pid, pid) if pid else "없음"
                self.chtabs.setTabToolTip(i, f"재생목록: {pl}" + (" (기본 재생목록)" if default else " (채널 지정)")
                                          + ("" if c in order else "\n채널 관리에서 빠진 채널 — 기록만 표시"))
            idx = next((i for i in range(self.chtabs.count()) if self.chtabs.tabData(i) == cur), 0)
            self.chtabs.setCurrentIndex(idx)
            self.chtabs.blockSignals(False)
            self.chtabs.setVisible(len(cids) > 1)
        sel = self.chtabs.tabData(self.chtabs.currentIndex()) or ""
        if sel:
            pid, default = self._playlist_of(sel)
            pl = self._pl_titles.get(pid, pid) if pid else "없음"
            pv, pv_src = self._privacy_of_channel(sel)
            self.pl_label.setText(f"▶ 올라가는 재생목록: {pl}" + (" (기본)" if default else "")
                                  + f"  ·  공개 범위: {PRIV_KO.get(pv, pv)}" + (" (기본)" if pv_src == "기본" else ""))
        else:
            self.pl_label.setText("")
        self.pl_label.setVisible(self.chtabs.isVisible())
        return sel

    def refresh_table(self, force=False):
        try:
            rows = self.db.all()
        except Exception:
            return
        try:
            from .pipeline import thumb_pause_left

            pause = thumb_pause_left(self.db)
        except Exception:  # noqa: BLE001
            pause = 0
        cid = self._sync_tabs(rows)
        if cid:
            rows = [r for r in rows if r["channel_id"] == cid]
        sig = hash(tuple((r["video_no"], r["status"], r["updated_at"]) for r in rows)) ^ self.filter.currentIndex() \
            ^ hash(-(-pause // 60)) ^ hash(cid)
        if any(r["status"] == "NEW" for r in rows):  # 게시 후 대기 남은 시간을 1분마다 갱신
            sig ^= hash(int(time.time() // 60))
        if not force and sig == getattr(self, "_sig", None):
            return
        self._sig = sig
        counts = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        active = counts.get("DOWNLOADING", 0) + counts.get("UPLOADING", 0)
        waiting = counts.get("NEW", 0) + counts.get("DOWNLOADED", 0)
        self.c_queue.set(
            f"완료 {counts.get('UPLOADED', 0)} · 대기 {waiting} · 실패 {counts.get('FAILED', 0)}",
            ("진행 중 " + str(active) + "건 · ") * bool(active)
            + f"보류 {counts.get('OUT', 0)} · 전체 {len(rows)}건",
            "bad" if counts.get("FAILED") else ("warn" if active else "ok"),
        )
        f = self.filter.currentIndex()
        if f == 1:
            rows = [r for r in rows if r["status"] in ("NEW", "DOWNLOADING", "DOWNLOADED", "UPLOADING")]
        elif f == 2:
            rows = [r for r in rows if r["status"] == "FAILED"]
        elif f == 3:
            rows = [r for r in rows if r["status"] == "UPLOADED"]
        elif f == 4:
            rows = [r for r in rows if r["status"] == "OUT"]
        sel = {self.table.item(i.row(), 0).text() for i in self.table.selectionModel().selectedRows()} \
            if self.table.rowCount() else set()
        self.table.setRowCount(len(rows))
        from .pipeline import WAIT_PREFIX, wait_text

        min_age = int(self.cfg.get("download", {}).get("min_age_minutes", 30))
        for i, r in enumerate(rows):
            d = r["duration"] or 0
            ids = jload(r["youtube_ids"])
            yt = ("✔ " + " ".join(ids)) if ids else "✗ 안 올라감"
            memo = (r["error"] or "") if r["status"] != "UPLOADED" else ""
            if r["status"] == "NEW" and (not memo or memo.startswith(WAIT_PREFIX)):
                # 게시 후 대기 안내는 목록을 새로 그릴 때마다 다시 계산 (남은 시간이 맞게)
                memo = wait_text(r["publish_ts"], min_age) or (
                    "대기 끝 — 다음 실행 때 받음" if memo.startswith(WAIT_PREFIX) else memo)
            keys = r.keys()
            if not memo and "local_duration" in keys and r["local_duration"]:
                ld = r["local_duration"]
                memo = f"파일 검증 ✓ {ld // 3600}:{ld % 3600 // 60:02d}:{ld % 60:02d}"
                if "yt_verified" in keys and r["yt_verified"]:
                    memo += " · 유튜브 길이 ✓"
                elif r["status"] == "UPLOADED":
                    memo += " · 유튜브 처리 대기"
            tdone = set(jload(r["thumb_ids"])) if "thumb_ids" in keys else set()
            tn = sum(1 for v in ids if v in tdone)
            if not ids:
                th, th_c = "", "#8c959f"
            elif "thumb_unknown" in keys and r["thumb_unknown"]:
                th, th_c = "기존 영상 · 확인 안 함", "#57606a"
            elif tn == len(ids):
                th, th_c = "✔ 적용" + (f" ({tn}/{len(ids)})" if len(ids) > 1 else ""), "#1a7f37"
            else:
                th = f"✗ 미적용 ({tn}/{len(ids)})" if len(ids) > 1 else "✗ 미적용"
                th += f" · 제한 대기 {-(-pause // 60)}분" if pause else " · 자동 재시도 대기"
                th_c = "#bc4c00"
            from .pipeline import privacy_of

            pv, pv_src = privacy_of(self.cfg, r)
            if ids:
                pv_txt = PRIV_KO.get(r["privacy"] or "", "확인 전")
            else:
                pv_txt = f"예정: {PRIV_KO.get(pv, pv)}" + (" (이 영상만)" if pv_src == "영상" else "")
            if r["status"] == "OUT" and not memo:
                memo = "채널 등록 전 방송 — 올리려면 관리 → 과거 방송 가져오기"
            vals = [str(r["video_no"]), r["channel_name"] or "", r["title"] or "", (r["publish_date"] or "")[:16],
                    f"{d // 3600}:{d % 3600 // 60:02d}", LABEL.get(r["status"], r["status"]),
                    f"{r['resolution']}p" if r["resolution"] else "", yt, pv_txt, th,
                    ("19+ " if r["adult"] else "") + memo]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j == 5:
                    it.setBackground(QColor(STATUS_BG.get(r["status"], "#ffffff")))
                    it.setForeground(QColor(STATUS_COLOR.get(r["status"], "#000")))
                    f_ = it.font()
                    f_.setBold(True)
                    it.setFont(f_)
                if j == 0:
                    ic = self._thumb_icon(r["thumb_file"] if "thumb_file" in r.keys() else None)
                    if ic:
                        it.setIcon(ic)
                        it.setToolTip(f'<img src="{Path(r["thumb_file"]).as_uri()}" width="480">')
                if j == 8:
                    it.setForeground(QColor(PRIV_COLOR.get(r["privacy"], "#8c959f") if ids else "#8c959f"))
                    it.setToolTip("우클릭 → 공개 범위 바꾸기")
                if j == 9:
                    it.setForeground(QColor(th_c))
                    if th.startswith("✔"):
                        it.setBackground(QColor(BG_OK))
                    elif th.startswith("✗"):
                        it.setBackground(QColor(BG_WARN))
                    if th.startswith("기존"):
                        it.setToolTip("이 프로그램이 올리지 않고 유튜브에 이미 있던 영상이라 썸네일 적용 여부를 알 수 없습니다.\n"
                                      "썸네일이 없으면 우클릭 → 썸네일 적용, 이미 있으면 우클릭 → 썸네일 적용됨으로 표시")
                if j == 10 and r["error"]:
                    it.setToolTip(r["error"])
                if j == 7:
                    it.setForeground(QColor("#1a7f37" if ids else "#8c959f"))
                    if ids:
                        it.setBackground(QColor(BG_OK))
                self.table.setItem(i, j, it)
            if vals[0] in sel:
                self.table.selectRow(i)

    def _thumb_icon(self, path):
        if not path or not Path(path).exists():
            return None
        ic = self._icons.get(path)
        if ic is None:
            pm = QPixmap(path)
            if pm.isNull():
                return None
            ic = QIcon(pm.scaled(192, 108, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self._icons[path] = ic
        return ic

    def _selected(self):
        return [int(self.table.item(i.row(), 0).text()) for i in self.table.selectionModel().selectedRows()]

    def row_menu(self, pos):
        nos = self._selected()
        if not nos:
            return
        m = QMenu(self)
        m.addAction("받기 (대기열에 추가)", lambda: self._queue_rows(nos))
        m.addAction("재시도", lambda: self._retry(nos))
        m.addAction("썸네일 적용 (선택한 영상)", lambda: self.start_cli("썸네일 적용", ["thumb", *map(str, nos)]))
        m.addAction("썸네일 적용됨으로 표시 (이미 붙어 있을 때)", lambda: self.start_cli("썸네일 표시", ["thumb-done", *map(str, nos)]))
        m.addAction("다시 업로드 (유튜브 쪽이 잘못됐을 때)", lambda: self.start_cli("다시 업로드", ["reupload", *map(str, nos)]))
        m.addAction("제외", lambda: self._skip(nos))
        pm = m.addMenu("공개 범위 바꾸기")
        for key, label in (("public", "공개"), ("unlisted", "일부 공개"), ("private", "비공개"),
                           ("default", "채널/기본 설정 따르기")):
            pm.addAction(label, lambda k=key: self._set_privacy(nos, k))
        m.addSeparator()
        m.addAction("치지직에서 열기", lambda: [_open(chzzk.video_url(n)) for n in nos])
        r = self.db.get(nos[0])
        for vid in jload(r["youtube_ids"]):
            m.addAction(f"유튜브에서 열기 ({vid})", lambda v=vid: _open(f"https://youtu.be/{v}"))
        files = jload(r["files"])
        if files:
            m.addAction("파일 위치 열기", lambda: _open(Path(files[0]).parent))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def row_open(self, row, _col):
        no = int(self.table.item(row, 0).text())
        ids = jload(self.db.get(no)["youtube_ids"])
        _open(f"https://youtu.be/{ids[0]}" if ids else chzzk.video_url(no))

    def _retry(self, nos):
        self.start_cli("재시도", ["retry", *map(str, nos)])

    def _set_privacy(self, nos, key):
        up = [n for n in nos if jload(self.db.get(n)["youtube_ids"])]
        if up and QMessageBox.question(
                self, "공개 범위", f"이미 올라간 {len(up)}개 영상은 유튜브에서 바로 공개 범위가 바뀝니다. 진행할까요?\n"
                "(아직 안 올라간 영상은 올라갈 때 이 공개 범위가 적용됩니다)") != QMessageBox.Yes:
            return
        self.start_cli("공개 범위", ["privacy", key, *map(str, nos)])

    def _skip(self, nos):
        if QMessageBox.question(self, "제외", f"{len(nos)}개 항목을 처리 대상에서 제외할까요?") == QMessageBox.Yes:
            self.start_cli("제외", ["skip", *map(str, nos)])

    # 로그
    def refresh_log(self):
        p = self.data_dir / "logs" / "chzzk2yt.log"
        try:
            size = p.stat().st_size
        except OSError:
            return
        if size == self._last_log_size:
            return
        self._last_log_size = size
        with open(p, "rb") as f:
            f.seek(max(0, size - 60_000))
            txt = f.read().decode("utf-8", "replace")
        lines = txt.splitlines()[-400:]
        for ln in reversed(lines):  # 최근 진행률을 상태바에
            if "진행률" in ln or "업로드 " in ln and "%" in ln:
                self.statusBar().showMessage(ln.split("] ", 1)[-1][:160])
                break
        bar = self.logv.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        self.logv.setPlainText("\n".join(lines))
        if at_end:
            self.logv.moveCursor(QTextCursor.End)

    # CLI 프로세스
    def start_cli(self, name, args):
        if args and args[0] in ("run", "dry-run", "scan") and not self.cfg.get("channels"):
            QMessageBox.information(self, name, "먼저 올릴 치지직 채널을 추가하세요. (관리 → 채널 관리, 또는 시작 가이드 1단계)")
            return
        if self.proc and self.proc.state() != QProcess.NotRunning:
            QMessageBox.information(self, "작업 중", f"‘{self.proc_name}’ 작업이 진행 중입니다.")
            return
        self.proc = QProcess(self)
        self.proc_name = name
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONPATH", str(Path(__file__).resolve().parent.parent))
        self.proc.setProcessEnvironment(env)
        self.proc.setWorkingDirectory(str(ROOT))
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_out)
        self.proc.finished.connect(self._on_finished)
        self.tabs.setCurrentIndex(0)
        self._append(f"\n━━ {name} 시작 ({time.strftime('%H:%M:%S')}) ━━\n")
        self.b_stop.setEnabled(True)
        self.b_run.setEnabled(False)
        self.statusBar().showMessage(f"{name} 진행 중…")
        self.proc.start(_python_console(), ["-m", "chzzk2yt", "-c", str(CONFIG), *args])

    def _append(self, text):
        self.out.moveCursor(QTextCursor.End)
        self.out.insertPlainText(text)
        self.out.moveCursor(QTextCursor.End)

    def _on_out(self):
        self._append(bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace"))

    def _on_finished(self, code, _status):
        self._append(f"━━ {self.proc_name} 종료 (코드 {code}) ━━\n")
        self.statusBar().showMessage(f"{self.proc_name} 완료" if code == 0 else f"{self.proc_name} 실패 (코드 {code})")
        self.b_stop.setEnabled(False)
        self.b_run.setEnabled(True)
        if self.proc_name in ("네이버 로그인", "쿠키 갱신", "유튜브 인증"):
            self.refresh_status()
        if self.guide and self.guide.isVisible():
            self.guide.refresh()
        self.refresh_table(force=True)
        if self.proc_name == "업데이트" and code == 0 and "업데이트 완료" in self.out.toPlainText()[-3000:]:
            if QMessageBox.question(self, "업데이트", "업데이트했습니다. 지금 프로그램을 다시 켤까요?") == QMessageBox.Yes:
                QProcess.startDetached(sys.executable, ["-m", "chzzk2yt.gui"], str(ROOT))
                self._quit()

    def stop_cli(self):
        if self.proc and self.proc.state() != QProcess.NotRunning:
            if QMessageBox.question(self, "중지", "진행 중인 작업을 중지할까요?\n(중단된 영상은 다음 실행 때 처음부터 다시 처리됩니다)") \
                    == QMessageBox.Yes:
                if IS_WIN:  # 자식(ffmpeg 등)까지 함께 종료
                    subprocess.run(["taskkill", "/PID", str(self.proc.processId()), "/T", "/F"],
                                   capture_output=True, **_no_window())
                else:
                    self.proc.kill()

    # 동작
    def add_urls(self):
        urls = self.url.text().split()
        if not urls:
            return
        self.url.clear()
        self.start_cli("URL 추가", ["add", *urls])

    def reupload_all(self):
        if QMessageBox.question(
                self, "전부 다시 올리기",
                "업로드완료·업로드중·실패 항목을 모두 다시 업로드 대기로 되돌립니다.\n"
                "유튜브 스튜디오에서 기존 영상을 먼저 지우셨나요?\n"
                "(로컬 파일이 남아 있으면 업로드만, 없으면 다운로드부터 다시 합니다)") == QMessageBox.Yes:
            self.start_cli("전부 다시 올리기", ["reupload", "all"])

    def toggle_task(self):
        if not IS_WIN:
            return
        if self.b_task.text() == "자동 실행 해제":
            cmd = ["powershell", "-NoProfile", "-Command", f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false"]
        else:
            mins, ok = QInputDialog.getInt(self, "자동 실행 등록", "몇 분마다 실행할까요?", 30, 10, 1440)
            if not ok:
                return
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "register_task.ps1"),
                   "-Minutes", str(mins)]
        r = subprocess.run(cmd, capture_output=True, text=True, **_no_window())
        self._append((r.stdout or "") + (r.stderr or ""))
        self._submit("task", check_task)

    def edit_channels(self):
        chs = [c for c in self.cfg.get("channels", [])
               if len(str(c.get("id", ""))) == 32 and set(str(c["id"])) != {"0"}]
        dlg = ChannelDialog(self, chs, self.cfg.get("upload", {}).get("playlist_id", ""), self._fetch_playlists,
                            self._pl_titles, self.cfg.get("upload", {}).get("privacy", "public"))
        if dlg.exec():
            cfgedit.write_channels(CONFIG, dlg.channels)
            self.reload_cfg()
            self._append(f"채널 {len(dlg.channels)}개 저장됨\n")
            from .pipeline import purge_removed

            # 설정을 못 읽으면(cfg == {}) 모든 채널이 빠진 것으로 보이므로 정리하지 않는다
            for name, n in (purge_removed(self.cfg.get("channels", []), self.db) if self.cfg else []):
                self._append(f"뺀 채널 정리: {name} — 올리지 않은 항목 {n}개를 목록에서 지움\n")
            self.refresh_table(force=True)
            if dlg.channels:
                self.start_cli("목록 새로고침", ["scan"])

    def edit_settings(self):
        dlg = SettingsDialog(self, self.cfg)
        if dlg.exec():
            for sec, key, v in dlg.values():
                cfgedit.set_value(CONFIG, sec, key, v)
            self.reload_cfg()
            self._append("설정 저장됨\n")
            self.refresh_status()

    # 시작 가이드 / 첫 설정 도우미
    def _maybe_guide(self):
        try:
            hide = int(self.db.get_kv("guide_hide") or 0)
        except Exception:  # noqa: BLE001
            hide = 0
        g = SetupGuide(self)
        if not g.essentials_done() or not hide:
            self.guide = g
            g.show()
        else:
            g.deleteLater()

    def open_guide(self):
        if self.guide is None or not self.guide.isVisible():
            self.guide = SetupGuide(self)
        self.guide.show()
        self.guide.raise_()

    def pick_client_secret(self):
        f, _ = QFileDialog.getOpenFileName(self, "구글에서 받은 client_secret JSON 파일 선택", str(Path.home() / "Downloads"),
                                           "JSON (*.json)")
        if not f:
            return
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "구글 API 파일", f"JSON 파일을 읽을 수 없습니다.\n{e}")
            return
        if "installed" not in d:
            QMessageBox.warning(self, "구글 API 파일",
                                "이 파일은 '데스크톱 앱' 종류가 아닙니다.\n구글 클라우드 → 클라이언트 만들기에서 "
                                "애플리케이션 유형을 '데스크톱 앱'으로 골라 다시 받으세요.")
            return
        dst = self.data_dir / "client_secret.json"
        dst.write_text(json.dumps(d), encoding="utf-8")
        self._append(f"구글 API 파일 저장: {dst}\n")
        if self.guide and self.guide.isVisible():
            self.guide.refresh()
        if QMessageBox.question(self, "구글 API 파일", "저장했습니다. 바로 유튜브 인증을 할까요?") == QMessageBox.Yes:
            self.auth_youtube()

    def auth_youtube(self):
        ok, msg = _secret_ok(self.data_dir)
        if not ok:
            QMessageBox.information(self, "유튜브 인증", f"구글 API 파일(client_secret.json)이 먼저 필요합니다: {msg}\n"
                                    "시작 가이드 2단계에서 넣어 주세요.")
            self.open_guide()
            return
        self.start_cli("유튜브 인증", ["auth-youtube"])

    def _fetch_playlists(self):
        """내 유튜브 재생목록 [(id, 제목)]. 실패하면 안내 후 None."""
        try:
            from . import youtube

            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                svc = youtube.service(self.data_dir)
                items, tok = [], None
                while True:
                    r = svc.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=tok).execute()
                    items += [(it["id"], it["snippet"]["title"]) for it in r.get("items", [])]
                    tok = r.get("nextPageToken")
                    if not tok:
                        break
            finally:
                QApplication.restoreOverrideCursor()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "재생목록", f"재생목록을 불러오지 못했습니다. 먼저 '유튜브 인증'을 하세요.\n{e}")
            return None
        self._pl_titles.update(dict(items))
        try:
            self.db.set_kv("playlist_titles", json.dumps(self._pl_titles, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass
        return items

    def pick_playlist(self):
        items = self._fetch_playlists()
        if items is None:
            return
        up = self.cfg.get("upload", {})
        dlg = PlaylistDialog(self, items, up.get("playlist_id", ""), up.get("playlist_position", "bottom"))
        if dlg.exec():
            pid, pos = dlg.value()
            cfgedit.set_value(CONFIG, "upload", "playlist_id", pid)
            cfgedit.set_value(CONFIG, "upload", "playlist_position", pos)
            self.reload_cfg()
            self._append(f"재생목록 설정: {pid or '넣지 않음'} ({pos})\n")

    def import_past(self):
        rows = list(self.db.by_status("OUT"))
        if not rows:
            QMessageBox.information(self, "과거 방송 가져오기",
                                    "보류 중인 과거 방송이 없습니다. (채널 추가 후 '목록 새로고침'을 먼저 하세요)")
            return
        ups = [r["publish_ts"] for r in self.db.by_status("UPLOADED", "UPLOADING", "DOWNLOADED")]
        dlg = PastDialog(self, rows, ups)
        if dlg.exec():
            nos = dlg.selected()
            if nos:
                self.start_cli("과거 방송 가져오기", ["queue", *map(str, nos)])

    def _queue_rows(self, nos):
        """우클릭 '받기': 보류 방송을 하나씩 넣을 때 순서가 꼬이지 않도록 확인."""
        rows = [self.db.get(n) for n in nos]
        first = min((r["publish_ts"] or 0) for r in rows)
        older_out = [r for r in self.db.by_status("OUT") if (r["publish_ts"] or 0) < first and r["video_no"] not in nos]
        newer_done = [r for r in self.db.by_status("UPLOADED", "UPLOADING", "DOWNLOADED")
                      if (r["publish_ts"] or 0) > first]
        warn = []
        if newer_done:
            warn.append(f"이 방송보다 최신인 영상 {len(newer_done)}개가 이미 올라가 있어, 유튜브 업로드 순서가 뒤바뀝니다.")
        if older_out:
            warn.append(f"이 방송보다 오래된 보류 방송이 {len(older_out)}개 있습니다. 나중에 그걸 올리면 순서가 꼬입니다.")
        if warn:
            b = QMessageBox.question(
                self, "받기", "\n".join(warn) + "\n\n과거 방송을 순서대로 올리려면 '과거 방송 가져오기'를 쓰는 게 좋습니다.\n"
                "그래도 선택한 것만 받을까요?  (아니요 = 과거 방송 가져오기 열기)",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if b == QMessageBox.No:
                self.import_past()
                return
            if b != QMessageBox.Yes:
                return
        self.start_cli("대기열 추가", ["queue", *map(str, nos)])

    # 트레이 / 인증 알림
    def _setup_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip("치지직 → 유튜브")
        m = QMenu()
        for text, fn in (("열기", self._restore), ("▶ 지금 실행", lambda: self.start_cli("실행", ["run"])),
                         ("유튜브 인증", lambda: (self._restore(), self.auth_youtube()))):
            a = QAction(text, m)
            a.triggered.connect(fn)
            m.addAction(a)
        m.addSeparator()
        q = QAction("종료", m)
        q.triggered.connect(self._quit)
        m.addAction(q)
        self._tray_menu = m
        self.tray.setContextMenu(m)
        self.tray.activated.connect(
            lambda r: self._restore() if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.messageClicked.connect(self._restore)
        self.tray.show()

    def _restore(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self._quitting = True
        self.close()

    # 틀 없는 창: 가장자리 크기 조절 + 최대화 아이콘 동기화
    EDGE = 6

    def _edges(self, gp):
        if self.isMaximized():
            return Qt.Edges()
        p = self.mapFromGlobal(gp)
        e = Qt.Edges()
        if p.x() <= self.EDGE:
            e |= Qt.LeftEdge
        if p.x() >= self.width() - self.EDGE:
            e |= Qt.RightEdge
        if p.y() <= self.EDGE:
            e |= Qt.TopEdge
        if p.y() >= self.height() - self.EDGE:
            e |= Qt.BottomEdge
        return e

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.MouseMove, QEvent.MouseButtonPress) and isinstance(obj, QWidget) and obj.window() is self:
            e = self._edges(ev.globalPosition().toPoint())
            if t == QEvent.MouseButtonPress and e and ev.button() == Qt.LeftButton and self.windowHandle():
                self.windowHandle().startSystemResize(e)
                return True
            if t == QEvent.MouseMove and not ev.buttons():
                if e in (Qt.LeftEdge, Qt.RightEdge):
                    cur = Qt.SizeHorCursor
                elif e in (Qt.TopEdge, Qt.BottomEdge):
                    cur = Qt.SizeVerCursor
                elif e in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge):
                    cur = Qt.SizeFDiagCursor
                elif e:
                    cur = Qt.SizeBDiagCursor
                else:
                    cur = None
                if cur is not None:
                    self.setCursor(cur)
                    self._edge_cur = True
                elif getattr(self, "_edge_cur", False):
                    self.unsetCursor()
                    self._edge_cur = False
        return super().eventFilter(obj, ev)

    def changeEvent(self, e):
        if e.type() == QEvent.WindowStateChange and hasattr(self, "titlebar"):
            self.titlebar.sync()
        super().changeEvent(e)

    def to_tray(self):
        """트레이로 숨기기 (일반 최소화는 작업 표시줄로 그대로)."""
        if not self.tray:
            self.showMinimized()
            return
        self.hide()
        if not getattr(self, "_tray_hint_shown", False):
            self._tray_hint_shown = True
            self.tray.showMessage("치지직 → 유튜브", "트레이로 숨겼습니다. 아이콘을 클릭하면 다시 열립니다.",
                                  QSystemTrayIcon.Information, 4000)

    def _auth_reminder(self, text):
        """유튜브 인증 만료 하루 전(6일째)부터 알림. 6시간에 한 번."""
        if time.time() - self._last_auth_warn < 6 * 3600:
            return
        self._last_auth_warn = time.time()
        msg = f"유튜브 {text}\n'유튜브 인증'을 누르면 그때부터 7일 연장됩니다."
        if self.tray:
            self.tray.showMessage("유튜브 인증 필요", msg, QSystemTrayIcon.Warning, 15000)
        if self.isVisible() and not self.isMinimized():
            if QMessageBox.question(self, "유튜브 인증", msg + "\n\n지금 인증할까요?") == QMessageBox.Yes:
                self.auth_youtube()

    def make_shortcuts(self):
        if not IS_WIN:
            return
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "make_shortcut.ps1")],
                           capture_output=True, text=True, **_no_window())
        self._append((r.stdout or "") + (r.stderr or ""))

    def do_update(self):
        from . import __version__

        new = getattr(self, "_new_ver", None)
        msg = (f"현재 v{__version__} → 새 버전 v{new}으로 업데이트할까요?" if new else
               f"현재 v{__version__}. 깃허브에서 최신 버전을 확인하고, 있으면 받을까요?")
        notes = getattr(self, "_new_notes", "") if new else ""
        if notes:
            msg += "\n\n이번 업데이트 내용\n" + notes
        msg += ("\n\n설정·채널·기록·인증(config.toml, data 폴더)은 그대로 유지됩니다. "
                "바꾸기 전 파일은 data\\backup 에 보관됩니다.\n다운로드·업로드가 진행 중이면 끝난 뒤에 하는 걸 권장합니다.")
        if QMessageBox.question(self, "업데이트", msg) == QMessageBox.Yes:
            self.start_cli("업데이트", ["update"])

    def closeEvent(self, e):
        if self.proc and self.proc.state() != QProcess.NotRunning:
            if QMessageBox.question(self, "종료", f"‘{self.proc_name}’ 작업이 진행 중입니다. 창을 닫으면 작업도 중지됩니다. 닫을까요?") \
                    != QMessageBox.Yes:
                e.ignore()
                return
            self.proc.kill()
        if self.tray:
            self.tray.hide()
        e.accept()
        QApplication.quit()


QSS = """
QMainWindow, QDialog { background: #f6f8fa; }
#mainWin { border: 1px solid #aeb6bf; }
#titleBar { background: #eef1f4; border-bottom: 1px solid #d0d7de; }
#titleText { color: #1f2328; font-size: 12px; }
QToolButton#capBtn, QToolButton#capClose { border: none; border-radius: 0; background: transparent; padding: 0; }
QToolButton#capBtn:hover { background: #dde1e6; }
QToolButton#capBtn:pressed { background: #cfd4da; }
QToolButton#capClose:hover { background: #c42b1c; }
#card { background: white; border: 1px solid #d0d7de; border-radius: 8px; }
#cardTitle { color: #57606a; font-size: 11px; }
#cardValue { font-size: 15px; font-weight: 600; }
#cardSub { color: #6e7781; font-size: 11px; }
QPushButton, QToolButton { padding: 6px 12px; border: 1px solid #d0d7de; border-radius: 6px; background: white; }
QPushButton:hover, QToolButton:hover { background: #f3f4f6; }
QPushButton:disabled { color: #a0a7b0; }
QPushButton#primary { background: #1f883d; color: white; border-color: #1a7f37; font-weight: 600; }
QPushButton#primary:hover { background: #1a7f37; }
QPushButton#primary:disabled { background: #94d3a2; }
QTableWidget { background: white; border: 1px solid #d0d7de; border-radius: 6px; gridline-color: #eaeef2; }
QPlainTextEdit { background: #0d1117; color: #c9d1d9; border-radius: 6px; }
QLineEdit, QComboBox, QSpinBox { padding: 5px; border: 1px solid #d0d7de; border-radius: 6px; background: white; }
"""


def main():
    os.chdir(ROOT)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    if not CONFIG.exists():
        ex = ROOT / "config.example.toml"
        if not ex.exists():
            QMessageBox.critical(None, "chzzk2yt", f"설정 파일이 없습니다:\n{CONFIG}\nmenu.bat 을 먼저 실행하세요.")
            return
        CONFIG.write_text(ex.read_text(encoding="utf-8"), encoding="utf-8")
    w = Main()
    w.show()
    if "--screenshot" in sys.argv:  # 개발용: 렌더링 확인
        out = sys.argv[sys.argv.index("--screenshot") + 1]
        QTimer.singleShot(6000, lambda: (w.grab().save(out), app.quit()))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
