"""SQLite 상태 저장소 — 무엇을 받았고 무엇을 올렸는지의 단일 기록."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

# 상태 흐름: NEW → DOWNLOADING → DOWNLOADED → UPLOADING → UPLOADED(→ 파일 삭제)
#            실패 시 attempts 증가 후 이전 단계로 복귀, max 초과 시 FAILED
#            SKIPPED: 사용자가 제외
#            OUT: 채널 목록엔 있지만 처리 대상 아님(start 이전·필터 제외) — '대기열 추가'로 NEW 전환
STATUSES = ("NEW", "DOWNLOADING", "DOWNLOADED", "UPLOADING", "UPLOADED", "FAILED", "SKIPPED", "OUT", "YT_DELETED", "YT_SHORT")

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_no       INTEGER PRIMARY KEY,
    channel_id     TEXT,
    channel_name   TEXT,
    title          TEXT,
    video_type     TEXT,
    category       TEXT,
    publish_date   TEXT,
    publish_ts     INTEGER,
    duration       INTEGER,
    thumbnail_url  TEXT,
    adult          INTEGER DEFAULT 0,
    paid           INTEGER DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'NEW',
    failed_stage   TEXT,
    dl_attempts    INTEGER DEFAULT 0,
    ul_attempts    INTEGER DEFAULT 0,
    error          TEXT,
    resolution     INTEGER,
    files          TEXT,            -- JSON list (분할 시 여러 개)
    file_size      INTEGER,
    youtube_ids    TEXT,            -- JSON list, 파트 순서대로
    discovered_at  INTEGER,
    downloaded_at  INTEGER,
    uploaded_at    INTEGER,
    updated_at     INTEGER
);
CREATE INDEX IF NOT EXISTS ix_status ON videos(status);
CREATE TABLE IF NOT EXISTS channels (
    channel_id   TEXT PRIMARY KEY,
    channel_name TEXT,
    baseline_ts  INTEGER,           -- start="new" 기준 시각(ms)
    last_scan_at INTEGER
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def now() -> int:
    return int(time.time())


class DB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(videos)")}
        if "thumb_ids" not in cols:  # 마이그레이션: 썸네일 적용 완료된 유튜브 ID 목록
            self.conn.execute("ALTER TABLE videos ADD COLUMN thumb_ids TEXT")
        if "thumb_file" not in cols:  # 마이그레이션: 로컬에 받아 둔 치지직 썸네일 경로
            self.conn.execute("ALTER TABLE videos ADD COLUMN thumb_file TEXT")
        if "local_duration" not in cols:  # 검증된 로컬 파일 길이(초)
            self.conn.execute("ALTER TABLE videos ADD COLUMN local_duration INTEGER")
        if "yt_verified" not in cols:  # 유튜브 처리 후 확인된 길이(초)
            self.conn.execute("ALTER TABLE videos ADD COLUMN yt_verified INTEGER")
        if "thumb_unknown" not in cols:  # 이 프로그램이 올리지 않은(유튜브에 이미 있던) 영상 → 썸네일 상태 모름
            self.conn.execute("ALTER TABLE videos ADD COLUMN thumb_unknown INTEGER")
        if "privacy" not in cols:  # 공개 범위. 올라간 영상 = 유튜브의 실제 값, 대기 영상 = 이 영상만 따로 정한 값(없으면 채널/기본)
            self.conn.execute("ALTER TABLE videos ADD COLUMN privacy TEXT")

    # ── kv ────────────────────────────────────────────
    def get_kv(self, k, default=None):
        r = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def set_kv(self, k, v):
        self.conn.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))

    # ── channels ─────────────────────────────────────
    def channel(self, cid):
        return self.conn.execute("SELECT * FROM channels WHERE channel_id=?", (cid,)).fetchone()

    def upsert_channel(self, cid, name, baseline_ts=None):
        if self.channel(cid) is None:
            self.conn.execute(
                "INSERT INTO channels(channel_id, channel_name, baseline_ts, last_scan_at) VALUES(?,?,?,?)",
                (cid, name, baseline_ts, now()),
            )
        else:
            self.conn.execute(
                "UPDATE channels SET channel_name=?, last_scan_at=? WHERE channel_id=?", (name, now(), cid)
            )

    def purge_channel(self, cid) -> int:
        """채널 관리에서 뺀 채널 정리. 유튜브에 올리지 않은 항목(받아 둔 파일 포함)을 지우고
        채널 기록도 지워, 다시 추가하면 처음 등록처럼 시작한다. 유튜브에 올린 기록은 중복 업로드를 막으려고 남긴다."""
        rows = self.conn.execute(
            "SELECT video_no, files FROM videos WHERE channel_id=? AND status IN ('NEW','OUT','SKIPPED','FAILED','DOWNLOADED')"
            " AND (youtube_ids IS NULL OR youtube_ids IN ('', '[]'))", (cid,)).fetchall()
        for r in rows:
            for f in jload(r["files"], []) or []:
                try:
                    Path(f).unlink(missing_ok=True)
                except OSError:
                    pass  # 다른 작업이 쓰는 중이면 파일은 남긴다
            self.conn.execute("DELETE FROM videos WHERE video_no=?", (r["video_no"],))
        busy = self.conn.execute("SELECT 1 FROM videos WHERE channel_id=? AND status IN ('DOWNLOADING','UPLOADING')",
                                 (cid,)).fetchone()
        if not busy:  # 진행 중인 항목이 있으면 채널 기록을 남겨 다음 스캔 때 다시 정리한다
            self.conn.execute("DELETE FROM channels WHERE channel_id=?", (cid,))
        return len(rows)

    # ── videos ───────────────────────────────────────
    def get(self, video_no):
        return self.conn.execute("SELECT * FROM videos WHERE video_no=?", (video_no,)).fetchone()

    def insert_video(self, v: dict, status: str = "NEW") -> bool:
        """새 영상이면 status 로 넣고 True. 이미 있으면 메타만 갱신하고 False."""
        if self.get(v["video_no"]):
            self.conn.execute(
                "UPDATE videos SET title=?, duration=?, thumbnail_url=? WHERE video_no=?",
                (v["title"], v["duration"], v["thumbnail_url"], v["video_no"]),
            )
            return False
        cols = list(v) + ["status", "discovered_at", "updated_at"]
        vals = list(v.values()) + [status, now(), now()]
        self.conn.execute(
            f"INSERT INTO videos({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals
        )
        return True

    def update(self, video_no, **fields):
        for k in ("files", "youtube_ids", "thumb_ids"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        fields["updated_at"] = now()
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE videos SET {sets} WHERE video_no=?", (*fields.values(), video_no))

    def by_status(self, *statuses):
        q = f"SELECT * FROM videos WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY publish_ts"
        return self.conn.execute(q, statuses).fetchall()

    def all(self):
        return self.conn.execute("SELECT * FROM videos ORDER BY publish_ts DESC").fetchall()

    def recover_interrupted(self):
        """비정상 종료로 중간 상태에 남은 항목을 되돌린다."""
        self.conn.execute("UPDATE videos SET status='NEW' WHERE status='DOWNLOADING'")
        self.conn.execute("UPDATE videos SET status='DOWNLOADED' WHERE status='UPLOADING'")


def jload(s, default=None):
    if not s:
        return default if default is not None else []
    return json.loads(s)
