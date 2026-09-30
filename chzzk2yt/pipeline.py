"""스캔 → 다운로드 → (분할) → 업로드 → 정리."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path

from . import chzzk, cookies as cookie_mod, downloader, report, youtube
from .db import DB, jload, now

log = logging.getLogger("chzzk2yt")
YT_MAX_SECONDS = 12 * 3600


def quota_day() -> str:
    """유튜브 쿼터는 태평양 시간 자정에 리셋된다."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


class Notifier:
    """중요한 알림을 로그에 남긴다 (외부로 보내지 않음). once_per_day 키는 하루 한 번만."""

    def __init__(self, cfg, db: DB):
        self.db = db

    def __call__(self, msg: str, once_per_day: str | None = None):
        if once_per_day:
            key = f"notified:{once_per_day}"
            today = time.strftime("%Y-%m-%d")
            if self.db.get_kv(key) == today:
                return
            self.db.set_kv(key, today)
        log.info("알림: %s", msg)


# ── 스캔 ───────────────────────────────────────────────
def _baseline_ms(start: str, stored: int | None) -> int:
    if start == "all":
        return 0
    if start == "new":
        return stored if stored is not None else int(time.time() * 1000)
    return int(datetime.strptime(start, "%Y-%m-%d").timestamp() * 1000)


def _match(title: str, inc: list, exc: list) -> bool:
    t = title.lower()
    if inc and not any(k.lower() in t for k in inc):
        return False
    return not any(k.lower() in t for k in exc)


def purge_removed(channels: list, db: DB) -> list[tuple[str, int]]:
    """채널 관리에서 뺀 채널의 올리지 않은 항목을 목록에서 지운다 (남겨 두면 계속 처리 대상이 된다).
    수동 추가(URL)는 channels 표에 기록되지 않으므로 여기서 지워지지 않는다. [(채널 이름, 지운 개수)]"""
    keep = {ch.get("id") for ch in channels}
    done = []
    for rec in db.conn.execute("SELECT channel_id, channel_name FROM channels").fetchall():
        if rec["channel_id"] not in keep:
            name = rec["channel_name"] or rec["channel_id"]
            n = db.purge_channel(rec["channel_id"])
            log.info("채널 삭제 정리: %s — 올리지 않은 항목 %d개를 목록에서 지움", name, n)
            done.append((name, n))
    return done


def scan(cfg, db: DB, cookies: dict) -> int:
    types = set(cfg["download"]["video_types"])
    added = 0
    purge_removed(cfg["channels"], db)
    if not cfg["channels"]:
        log.warning("감시할 채널이 없습니다. 메뉴 '채널 관리'에서 추가하세요.")
    for ch in cfg["channels"]:
        cid = ch["id"]
        try:
            info = chzzk.channel_info(cid, cookies)
            rec = db.channel(cid)
            first = rec is None
            base = _baseline_ms(ch["start"], rec["baseline_ts"] if rec else None)
            db.upsert_channel(cid, info.get("channelName"), base)
            known_streak = 0

            def wanted(row):
                return (row["publish_ts"] > base and row["video_type"] in types
                        and _match(row["title"], ch["include_keywords"], ch["exclude_keywords"]))

            # 채널의 모든 다시보기를 목록에 올린다. 처리 대상이 아니면 OUT(대상 아님)으로.
            for v in chzzk.iter_videos(cid, cookies):
                row = chzzk.to_row(v)
                target = wanted(row)
                if db.insert_video(row, "NEW" if target else "OUT"):
                    known_streak = 0
                    if target:
                        added += 1
                        log.info("새 영상: [%s] %s (%s)", row["channel_name"], row["title"], row["video_no"])
                else:
                    known_streak += 1
                    if known_streak >= 30 and not first:
                        break  # 이미 아는 영상이 연속 → 더 과거는 볼 필요 없음
            # start 를 과거로 바꾼 경우 등: 이제 대상이 된 OUT 항목을 대기로 승격
            for r in db.conn.execute(
                "SELECT * FROM videos WHERE channel_id=? AND status='OUT' AND publish_ts>?", (cid, base)
            ).fetchall():
                if wanted(r):
                    db.update(r["video_no"], status="NEW")
                    added += 1
            if first and ch["start"] == "new":
                log.info("채널 등록: %s — 지금 이후 올라오는 다시보기부터 처리합니다.", info.get("channelName"))
        except Exception as e:
            log.error("채널 스캔 실패 %s: %r", cid, e)
    return added


def add_manual(cfg, db: DB, url: str, cookies: dict) -> int:
    no = chzzk.parse_video_no(url)
    row = chzzk.to_row(chzzk.video_detail(no, cookies))
    if not db.insert_video(row):
        r = db.get(no)
        if r["status"] in ("FAILED", "SKIPPED", "OUT"):
            db.update(no, status="NEW", dl_attempts=0, ul_attempts=0, error=None)
    return no


# ── 처리 ───────────────────────────────────────────────
def _fmt(tpl: str, r, part: str = "") -> str:
    date = (r["publish_date"] or "")[:10]
    return tpl.format(
        title=r["title"], date=date, channel=r["channel_name"] or "", category=r["category"] or "",
        url=chzzk.video_url(r["video_no"]), video_no=r["video_no"],
    ).strip() + part


def privacy_of(cfg_raw: dict, r) -> tuple[str, str]:
    """이 영상이 올라갈 공개 범위와 그 출처 ("영상" | "채널" | "기본").
    우선순위: 영상별로 정한 값 > 채널별 설정 > 설정의 기본 공개 범위."""
    from .youtube import PRIVACY

    if r["privacy"] in PRIVACY and r["status"] not in ("UPLOADED", "YT_SHORT", "YT_DELETED"):
        return r["privacy"], "영상"
    for ch in cfg_raw.get("channels", []):
        if ch.get("id") == r["channel_id"] and ch.get("privacy") in PRIVACY:
            return ch["privacy"], "채널"
    return cfg_raw.get("upload", {}).get("privacy", "public"), "기본"


def _playlist(cfg, r) -> str:
    for ch in cfg["channels"]:
        if ch["id"] == r["channel_id"] and ch.get("playlist_id"):
            return "" if ch["playlist_id"] == "none" else ch["playlist_id"]  # "none" = 이 채널은 재생목록에 넣지 않음
    return cfg["upload"]["playlist_id"]


def _fail(cfg, db: DB, r, stage: str, err: Exception, notify):
    field = "dl_attempts" if stage == "download" else "ul_attempts"
    n = (r[field] or 0) + 1
    back = "NEW" if stage == "download" else "DOWNLOADED"
    if n >= cfg["download"]["max_attempts"]:
        db.update(r["video_no"], status="FAILED", failed_stage=stage, error=str(err)[:1000], **{field: n})
        notify(f"❌ {stage} 실패 (포기): {r['title']} — {err}")
    else:
        db.update(r["video_no"], status=back, error=str(err)[:1000], **{field: n})
    log.error("%s 실패 (%d회): %s — %s", stage, n, r["video_no"], err)


def _hms(s) -> str:
    s = int(s or 0)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


PROGRESS_KEY = "progress:"  # kv: 진행 중인 영상의 진행률 (GUI 목록이 읽어서 표시)


def progress_writer(db: DB, video_no: int, part: str = ""):
    """진행률을 kv에 기록하는 함수를 돌려준다. 너무 자주 쓰지 않게 2초 간격 (단계가 바뀌면 바로)."""
    last = {"t": 0.0, "stage": None}

    def write(stage: str, done: float = 0, total: float = 0, speed: float = 0):
        now_ = time.time()
        if stage == last["stage"] and now_ - last["t"] < 2 and done < total:
            return
        last.update(t=now_, stage=stage)
        eta = int((total - done) / speed) if speed > 0 and total > done else 0
        db.set_kv(f"{PROGRESS_KEY}{video_no}",
                  {"stage": stage, "pct": round(done * 100 / total, 1) if total else None, "speed": speed,
                   "eta": eta, "part": part, "at": int(now_)})

    return write


def progress_clear(db: DB, video_no: int):
    db.conn.execute("DELETE FROM kv WHERE k=?", (f"{PROGRESS_KEY}{video_no}",))


WAIT_PREFIX = "게시 후 "  # 대기 안내 문구 앞머리 (목록이 알아보고 남은 시간을 새로 계산하는 데 씀)


def wait_text(publish_ts, min_age: int) -> str | None:
    """게시 후 대기 규칙(min_age_minutes)에 걸려 있으면 안내 문구, 아니면 None."""
    if not publish_ts:
        return None
    age = (time.time() * 1000 - publish_ts) / 60000
    if age >= min_age:
        return None
    ready = time.strftime("%H:%M", time.localtime(publish_ts / 1000 + min_age * 60))
    return f"{WAIT_PREFIX}{int(age)}분 — {min_age}분 대기 규칙 (치지직 인코딩 완료 대기), {ready}부터 받음"


def do_download(cfg, db: DB, r, cookies: dict, notify) -> bool:
    d = cfg["download"]
    wait = wait_text(r["publish_ts"], d["min_age_minutes"])
    if wait:
        db.update(r["video_no"], error=wait)
        return False
    no = r["video_no"]
    if r["adult"] and not cookies:
        # 쿠키 없으면 반드시 실패 → 재시도 횟수를 소모하지 않고 쿠키 복구까지 대기
        db.update(no, error="19+ 영상: '네이버 로그인'(성인인증 계정) 후 자동 진행 — 안 받을 거면 우클릭 → 제외")
        return False
    db.update(no, status="DOWNLOADING", error=None)
    report.write(db, cfg.data_dir)
    out_dir, stem = _target(cfg, r)
    prog = progress_writer(db, no)
    prog("다운로드")
    try:
        expected = r["duration"] or 0
        timeout = int(d["timeout_hours"] * 3600)
        threads = int(d.get("max_threads", 16))
        engine = d.get("engine", "direct")
        path, salvaged = None, 0
        if engine == "direct":
            try:
                path, res, salvaged = downloader.download_direct(cfg.repo, no, cookies, out_dir, stem,
                                                       d["resolution"], timeout, threads, on_progress=prog)
            except downloader._NoHls as e:
                log.info("직접 방식 불가(%s) → 기본 다운로더 사용", e)
        if path is None:
            path, res = downloader.download(cfg.repo, chzzk.video_url(no), cookies, out_dir, stem,
                                            d["resolution"], timeout, threads)
        # ── 검증 1: 받은 파일 (스트림·길이 ±2%·앞/중간/끝 디코딩) ──
        prog("검증")
        try:
            got = downloader.verify_media(path, expected)
        except downloader.VerifyError as ve:
            path.unlink(missing_ok=True)
            raise downloader.DownloadError(f"검증 실패: {ve}") from ve
        log.info("검증 통과: 파일 %s / 치지직 %s (스트림·디코딩 정상)", _hms(got), _hms(expected))
        files = [path]
        n = _parts(cfg, r["duration"] or 0)
        if n > 1:
            prog("분할")
            files = downloader.split(path, _part_seconds(r["duration"], n))
            # ── 검증 2: 분할 조각 각각 + 합계 길이 ──
            part_d = [downloader.verify_media(f) for f in files]
            if abs(sum(part_d) - got) > max(5, got * 0.005):
                raise downloader.DownloadError(
                    f"분할 합계 불일치: {_hms(sum(part_d))} / {_hms(got)}")
            log.info("분할 검증 통과: %s", " + ".join(_hms(x) for x in part_d))
        size = sum(f.stat().st_size for f in files)
        db.update(no, status="DOWNLOADED", resolution=res, files=[str(f) for f in files],
                  file_size=size, downloaded_at=now(), error=None, local_duration=int(got), salvaged=salvaged)
        log.info("다운로드 완료: %s (%d파일, %.2f GB)", r["title"], len(files), size / 1e9)
        return True
    except Exception as e:
        _fail(cfg, db, r, "download", e, notify)
        return False
    finally:
        progress_clear(db, no)
        report.write(db, cfg.data_dir)


def do_upload(cfg, db: DB, r, svc, notify):
    u = cfg["upload"]
    no = r["video_no"]
    files = [Path(f) for f in jload(r["files"])]
    ids = jload(r["youtube_ids"])
    todo = files[len(ids):]  # 아직 안 올라간 파트 (이미 올라간 파트는 지워졌어도 괜찮음)
    if not files or any(not f.exists() for f in todo):
        if ids:
            msg = "분할 영상의 남은 파트 파일이 없습니다 — 유튜브에서 올라간 파트를 지우고 '다시 업로드' 하세요"
            log.error("%s: %s", msg, no)
            db.update(no, status="FAILED", failed_stage="upload", error=msg)
            return
        log.warning("파일이 없어 다시 다운로드합니다: %s", no)
        db.update(no, status="NEW", files=None, youtube_ids=None)
        return
    # ── 검증 3: 올리기 직전 파일이 그대로인지 (크기) ──
    if r["file_size"] and not ids and sum(f.stat().st_size for f in files) != r["file_size"]:
        log.error("업로드 전 파일 크기가 다운로드 때와 다릅니다 → 다시 다운로드: %s", no)
        db.update(no, status="NEW", files=None, youtube_ids=None, error="파일 변경 감지 — 다시 다운로드")
        for f in files:
            f.unlink(missing_ok=True)
        return
    db.update(no, status="UPLOADING")
    report.write(db, cfg.data_dir)
    tags = [t for t in [r["channel_name"], r["category"], "치지직"] if t]
    privacy = privacy_of(cfg.raw, r)[0]
    try:
        for i, f in enumerate(files):
            if i < len(ids):
                continue  # 이미 올라간 파트
            title = _titles(cfg, r, len(files))[i]
            skey = f"upload_session:{no}:{i}"
            prog = progress_writer(db, no, f"{i + 1}/{len(files)}" if len(files) > 1 else "")
            vid = youtube.upload(svc, f, title, _fmt(u["description_template"], r), tags,
                                 u["category_id"], privacy,
                                 session=(lambda k=skey: db.get_kv(k), lambda v, k=skey: db.set_kv(k, v)),
                                 chunk_mb=int(u.get("chunk_mb", 256)), on_progress=prog)
            progress_clear(db, no)
            ids.append(vid)
            db.update(no, youtube_ids=ids)
            pl = _playlist(cfg, r)
            if pl:
                try:
                    psvc = youtube.fresh(svc)
                    auto_key = f"playlist_autosort:{pl}"
                    auto = int(db.get_kv(auto_key) or 0) > time.time() - 86400
                    pos = None if auto else _playlist_position(
                        psvc, db, pl, r["publish_ts"], u.get("playlist_position", "bottom") == "top")
                    placed = youtube.add_to_playlist(psvc, pl, vid, position=pos)
                    if placed == "autosort":
                        db.set_kv(auto_key, int(time.time()))
                        log.info("재생목록에 추가: %s (자동 정렬 재생목록이라 순서는 유튜브가 정함)", pl)
                    else:
                        log.info("재생목록에 추가: %s", pl)
                except Exception as e:
                    log.warning("재생목록 추가 실패: %r", e)
            if u["set_thumbnail"]:
                apply_thumbnail(cfg, db, no, vid, youtube.fresh(svc), notify)
        db.update(no, status="UPLOADED", uploaded_at=now(), error=None, privacy=privacy)
        if u["set_thumbnail"] and not thumb_pause_left(db):
            try:  # 앞서 제한 때문에 빠진 썸네일이 있으면 하나씩 따라잡기
                ok, total = apply_missing_thumbnails(cfg, db, svc, limit=1)
                if total:
                    log.info("빠진 썸네일 따라잡기: %d/%d", ok, total)
            except Exception as e:  # noqa: BLE001
                log.warning("썸네일 따라잡기 실패: %r", e)
        notify(f"✅ 업로드 완료: {r['title']} → " + " ".join(f"https://youtu.be/{i}" for i in ids))
        if u["delete_after_upload"]:
            for f in files:
                f.unlink(missing_ok=True)
    except (youtube.QuotaError, youtube.AuthError):
        db.update(no, status="DOWNLOADED")
        raise
    except Exception as e:
        _fail(cfg, db, db.get(no), "upload", e, notify)
    finally:
        progress_clear(db, no)
        report.write(db, cfg.data_dir)


def _norm_title(t: str) -> str:
    t = re.sub(r"^\[\d{4}-\d{2}-\d{2}\]\s*", "", t or "")
    t = re.sub(r"\s*\(\d+/\d+\)\s*$", "", t)
    return re.sub(r"\s+", " ", t.replace("<", "(").replace(">", ")")).strip().lower()


def sync_youtube(cfg, db: DB, svc) -> int:
    """유튜브 채널에 이미 올라간 영상을 찾아 목록에 '업로드완료'로 반영 (중복 업로드 방지).

    1순위: 설명의 치지직 주소(…/video/번호) 일치  2순위: 제목 일치(날짜 접두어·(n/m) 제거 후)
    """
    ups = youtube.list_my_uploads(svc)
    by_no: dict[int, list] = {}
    by_title: dict[str, list] = {}
    for u in ups:
        m = re.search(r"chzzk\.naver\.com/video/(\d+)", u["description"])
        if m:
            by_no.setdefault(int(m.group(1)), []).append(u)
        else:
            by_title.setdefault(_norm_title(u["title"]), []).append(u)

    def part_no(u):
        m = re.search(r"\((\d+)/\d+\)\s*$", u["title"])
        return int(m.group(1)) if m else 0

    states = youtube.video_states(svc, [u["id"] for u in ups]) if ups else {}
    changed = 0
    for r in db.all():
        if r["status"] in ("UPLOADING", "DOWNLOADING"):
            continue
        # 설명에 치지직 주소가 없으면 '제목 + 방송일'이 둘 다 같아야 같은 영상으로 봄 (같은 제목 반복 방송 오인 방지)
        date = (r["publish_date"] or "")[:10]
        found = by_no.get(r["video_no"]) or (
            [u for u in by_title.get(_norm_title(r["title"]), []) if date and date in u["title"]] or None)
        if not found:
            continue
        if r["status"] == "UPLOADED" and {u["id"] for u in found} <= set(jload(r["youtube_ids"])):
            continue  # 이미 우리 기록과 일치 (유튜브 쪽 처리 중이어도 문제 없음)
        done = [u for u in found if states.get(u["id"]) == "processed"]
        if len(done) < len(found):
            log.info("유튜브에 있지만 처리 완료 전이거나 중간에 끊긴 업로드라 건너뜀: %s (%s)", r["title"],
                     ", ".join(f"{u['id']}={states.get(u['id'], '?')}" for u in found if u not in done))
        if not done:
            continue
        found = done
        ids = [u["id"] for u in sorted(found, key=part_no)]
        if r["status"] == "UPLOADED" and set(ids) <= set(jload(r["youtube_ids"])):
            continue
        # 이 프로그램이 올린 게 아니라 유튜브에 이미 있던 영상 → 썸네일은 확인할 방법이 없으니
        # '확인 안 함'으로 두고 자동 재적용 대상에서 뺀다 (이미 제대로 붙어 있는 경우가 대부분)
        extra = {} if jload(r["thumb_ids"]) else {"thumb_ids": ids, "thumb_unknown": 1}
        db.update(r["video_no"], status="UPLOADED", youtube_ids=ids, error=None,
                  uploaded_at=r["uploaded_at"] or now(), **extra)
        changed += 1
        log.info("유튜브에 이미 있음 → 업로드완료로 표시: %s (%s)", r["title"], ", ".join(ids))
    # 반대 방향: 목록엔 '업로드완료'인데 유튜브에서 지워진 영상 → '유튜브에서 삭제됨'
    # + 처리 끝난 영상의 길이가 치지직 원본보다 짧으면 '유튜브 영상 잘림'
    uploaded = db.by_status("UPLOADED")
    our_ids = [i for r in uploaded for i in jload(r["youtube_ids"])]
    alive = youtube.video_durations(svc, our_ids) if our_ids else {}
    for r in uploaded:
        ids = jload(r["youtube_ids"])
        if ids and not any(i in alive for i in ids):
            db.update(r["video_no"], status="YT_DELETED", error="유튜브에서 삭제됨 — '다시 업로드'로 다시 올릴 수 있음")
            changed += 1
            log.info("유튜브에서 삭제된 영상: %s (%s)", r["title"], ", ".join(ids))
            continue
        states = [alive.get(i) for i in ids]
        # 유튜브 스튜디오에서 바꾼 공개 범위도 목록에 반영 (파트가 여럿이면 첫 파트 기준)
        pv = next((s[2] for s in states if s and s[2]), "")
        if pv and pv != r["privacy"]:
            db.update(r["video_no"], privacy=pv)
        bad =[i for i, s in zip(ids, states) if s and s[0] in ("failed", "rejected")]
        if bad:
            db.update(r["video_no"], status="YT_SHORT",
                      error="유튜브 처리 실패(" + ", ".join(bad) + ") — 유튜브에서 지우고 '다시 업로드'")
            changed += 1
            log.warning("유튜브 처리 실패: %s (%s)", r["title"], ", ".join(bad))
            continue
        if (ids and any(s and s[0] == "uploaded" for s in states) and r["uploaded_at"]
                and time.time() - r["uploaded_at"] > 3600 and not db.get_kv(f"yt_slow:{r['video_no']}")):
            db.set_kv(f"yt_slow:{r['video_no']}", int(time.time()))  # 영상마다 한 번만 알림
            log.warning("유튜브 처리가 1시간 넘게 끝나지 않음: %s%s — 유튜브 스튜디오에서 멈춰 있으면 지우고 '다시 업로드'",
                        r["title"], f" (손상 조각 {r['salvaged']}개 살린 파일)" if r["salvaged"] else "")
        if ids and all(s and s[0] == "processed" for s in states) and r["duration"]:
            total = sum(s[1] for s in states)
            # ── 검증 4: 유튜브 처리 후 길이 ──
            if total >= r["duration"] * 0.98 and not r["yt_verified"]:
                db.update(r["video_no"], yt_verified=int(total))
                log.info("유튜브 길이 검증 통과: %s (%s)", r["title"], _hms(total))
            if total < r["duration"] * 0.95:
                db.update(r["video_no"], status="YT_SHORT",
                          error=f"유튜브 영상 잘림 {_hms(total)} / 원본 {_hms(r['duration'])} — 유튜브에서 지우고 '다시 업로드'")
                changed += 1
                log.warning("유튜브 영상이 원본보다 짧음: %s (%s / %s)", r["title"], _hms(total), _hms(r["duration"]))
    log.info("유튜브 동기화: 내 채널 영상 %d개 확인, %d개 반영", len(ups), changed)
    return changed


_thumb_blocked = False  # 이번 실행에서 권한 없음(403) 확인 → 더 시도하지 않음

# 유튜브 썸네일 429 는 영상별이 아니라 '채널 단위' 단기 제한으로 확인됨
# (한 번도 시도 안 한 새 영상도 바로 429). → 채널 전체를 잠깐 쉬었다가 다음 영상에서 다시 시도.
_THUMB_PAUSES = [15, 30, 60, 120, 180]  # 연속으로 걸릴 때마다 늘어나는 쉬는 시간(분)


def thumb_pause_left(db: DB) -> int:
    """썸네일 쉬는 시간이 몇 초 남았는지 (0 = 지금 가능)."""
    return max(0, int(db.get_kv("thumb_pause_until") or 0) - int(time.time()))


def apply_thumbnail(cfg, db: DB, video_no: int, vid: str, svc, notify) -> bool:
    """치지직 썸네일을 유튜브 영상에 적용. 실패해도 업로드는 성공으로 둔다 (나중에 자동으로 채움)."""
    global _thumb_blocked
    if _thumb_blocked:
        return False
    left = thumb_pause_left(db)
    if left:
        log.info("썸네일 제한으로 쉬는 중(%d분 남음) → https://youtu.be/%s 는 나중에 적용", -(-left // 60), vid)
        return False
    r = db.get(video_no)
    path = r["thumb_file"]
    if not path or not Path(path).exists():
        path = fetch_thumbnail(cfg, db, video_no)
    if not path:
        log.warning("썸네일 파일 없음: %s", video_no)
        return False
    try:
        youtube.set_thumbnail_file(svc, vid, Path(path), cfg.data_dir)
    except youtube.ThumbnailForbidden as e:
        _thumb_blocked = True
        log.error("%s", e)
        notify(f"⚠️ {e}", once_per_day="thumb_forbidden")
        return False
    except youtube.ThumbnailRateLimited:
        streak = int(db.get_kv("thumb_pause_streak") or 0)
        mins = _THUMB_PAUSES[min(streak, len(_THUMB_PAUSES) - 1)]
        db.set_kv("thumb_pause_until", int(time.time()) + mins * 60)
        db.set_kv("thumb_pause_streak", streak + 1)
        log.warning("썸네일 횟수 제한(채널 단위): %s — %d분 쉬었다가 다음 영상에서 이어서 적용", vid, mins)
        return False
    except youtube.QuotaError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("썸네일 설정 실패 %s: %r", vid, e)
        return False
    db.set_kv("thumb_pause_streak", 0)
    done = jload(db.get(video_no)["thumb_ids"])
    db.update(video_no, thumb_ids=done if vid in done else done + [vid], thumb_unknown=0)
    log.info("썸네일 적용: https://youtu.be/%s", vid)
    return True


def fetch_thumbnail(cfg, db: DB, video_no: int, cookies: dict | None = None) -> str | None:
    """치지직 썸네일(1280x720)을 data/thumbs/ 에 받아 두고 경로를 기록."""
    r = db.get(video_no)
    url = r["thumbnail_url"]
    if not url:
        try:
            url = chzzk.video_detail(video_no, cookies).get("thumbnailImageUrl") or ""
            if url:
                db.update(video_no, thumbnail_url=url)
        except Exception:  # noqa: BLE001
            url = ""
    if not url:
        return None
    try:
        data = youtube.download_thumbnail(url)
    except Exception as e:  # noqa: BLE001
        log.warning("썸네일 다운로드 실패 %s: %r", video_no, e)
        return None
    d = cfg.data_dir / "thumbs"
    d.mkdir(exist_ok=True)
    ext = "png" if data[:4] == b"\x89PNG" else "jpg"
    p = d / f"{video_no}.{ext}"
    p.write_bytes(data)
    db.update(video_no, thumb_file=str(p))
    return str(p)


def fetch_thumbnails(cfg, db: DB, cookies: dict | None = None, out_limit: int = 60) -> int:
    """목록 표시·업로드용 썸네일을 미리 받아 둔다.
    보류(채널 등록 전) 방송도 목록에서 알아볼 수 있게 최근 것부터 out_limit 개까지 받는다. 제외 항목은 건너뜀."""
    n = out = 0
    for r in db.all():  # 최신순
        if r["status"] == "SKIPPED":
            continue
        if r["thumb_file"] and Path(r["thumb_file"]).exists():
            continue
        if r["status"] == "OUT":
            out += 1
            if out > out_limit:
                continue
        if fetch_thumbnail(cfg, db, r["video_no"], cookies):
            n += 1
    if n:
        log.info("치지직 썸네일 %d개 받음", n)
    return n


def apply_missing_thumbnails(cfg, db: DB, svc=None, limit: int | None = None) -> tuple[int, int]:
    """이미 업로드된 영상 중 썸네일이 안 붙은 것에 일괄 적용. (성공, 대상) 반환."""
    svc = youtube.fresh(svc) if svc else youtube.service(cfg.data_dir)
    notify = Notifier(cfg, db)
    ok = total = 0
    for r in db.by_status("UPLOADED"):
        done = set(jload(r["thumb_ids"]))
        for vid in jload(r["youtube_ids"]):
            if vid in done:
                continue
            if limit is not None and total >= limit:
                return ok, total
            if _thumb_blocked or thumb_pause_left(db):
                return ok, total
            total += 1
            if apply_thumbnail(cfg, db, r["video_no"], vid, svc, notify):
                ok += 1
    return ok, total


def _playlist_position(svc, db: DB, playlist_id: str, publish_ts: int, newest_first: bool) -> int | None:
    """방송일 순서에 맞는 재생목록 위치. 나중에 다시 올린 옛날 영상도 제자리에 들어간다."""
    try:
        items = youtube.playlist_items(svc, playlist_id)
    except Exception as e:  # noqa: BLE001
        log.warning("재생목록 조회 실패(%r) → %s에 추가", e, "맨 위" if newest_first else "맨 아래")
        return 0 if newest_first else None
    ts_of = {}
    for row in db.all():
        for i in jload(row["youtube_ids"]):
            ts_of[i] = row["publish_ts"] or 0
    for idx, vid in enumerate(items):
        t = ts_of.get(vid)
        if t is None:
            continue
        if (newest_first and t < publish_ts) or (not newest_first and t > publish_ts):
            return idx
    return len(items) if items else None


def _target(cfg, r):
    out_dir = cfg.download_dir / _safe(r["channel_name"] or r["channel_id"])
    return out_dir, f"{(r['publish_date'] or '')[:10]}_{r['video_no']}_{r['title']}"


def _parts(cfg, duration: int) -> int:
    part_s = int(cfg["upload"]["max_part_hours"] * 3600)
    if not cfg["upload"]["enabled"] or duration <= min(part_s, YT_MAX_SECONDS):
        return 1
    return -(-duration // part_s)


def _part_seconds(duration: int, n: int) -> int:
    """n개로 균등 분할할 조각 길이. 키프레임 오차로 n+1번째 자투리가 생기지 않게 5초 여유."""
    return -(-duration // n) + 5


def _titles(cfg, r, n: int) -> list[str]:
    base = _fmt(cfg["upload"]["title_template"], r)
    out = []
    for i in range(n):
        part = f" ({i + 1}/{n})" if n > 1 else ""
        out.append(youtube.clean_title(base[: 100 - len(part)] + part))
    return out


def win_toast(title: str, msg: str) -> None:
    """윈도우 알림 풍선 (자동 실행 중 프로그램 창이 없어도 보이도록). 실패해도 무시."""
    import os
    import subprocess

    if os.name != "nt":
        return
    t = title.replace("'", "''")
    m = msg.replace("'", "''")
    ps = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
          "$n=New-Object System.Windows.Forms.NotifyIcon; $n.Icon=[System.Drawing.SystemIcons]::Warning; "
          f"$n.BalloonTipTitle='{t}'; $n.BalloonTipText='{m}'; $n.Visible=$true; "
          "$n.ShowBalloonTip(15000); Start-Sleep -Seconds 16; $n.Dispose()")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=0x08000000)
    except Exception:  # noqa: BLE001
        pass


def _safe(name: str) -> str:
    return "".join("_" if c in '<>:"/\\|?*' else c for c in name).strip(". ") or "channel"


def run_once(cfg, db: DB):
    global _thumb_blocked
    _thumb_blocked = False
    notify = Notifier(cfg, db)
    db.recover_interrupted()
    cookies = cookie_mod.get_cookies(cfg, notify)
    if cfg["upload"]["enabled"] and cfg["upload"].get("oauth_testing", True):
        exp = youtube.token_expiry(cfg.data_dir)
        if exp and exp - time.time() < 86400:
            msg, _ = youtube.expiry_text(cfg.data_dir)
            log.warning("유튜브 %s", msg)
            notify(f"⚠️ 유튜브 {msg} — 앱에서 '유튜브 인증'을 눌러 연장하세요", once_per_day="yt_expiry")
            if db.get_kv("toast_yt_expiry") != time.strftime("%Y-%m-%d"):  # 윈도우 알림은 하루 한 번
                db.set_kv("toast_yt_expiry", time.strftime("%Y-%m-%d"))
                win_toast("유튜브 인증 필요", f"{msg}. 치지직→유튜브 프로그램에서 '유튜브 인증'을 누르면 7일 연장됩니다.")
    n = scan(cfg, db, cookies)
    log.info("스캔 완료: 새 영상 %d개", n)
    fetch_thumbnails(cfg, db, cookies)
    report.write(db, cfg.data_dir)

    upload_on = cfg["upload"]["enabled"]
    svc = None
    if upload_on:
        if db.get_kv("quota_block") == quota_day():
            log.warning("오늘은 유튜브 쿼터 초과로 업로드를 건너뜁니다.")
            upload_on = False
        else:
            try:
                svc = youtube.service(cfg.data_dir)
            except youtube.AuthError as e:
                log.error("%s", e)
                notify(f"⚠️ {e}", once_per_day="yt_auth")
                upload_on = False

    # 한 편씩 끝까지 (디스크 사용량 최소화). 업로드 대기분 먼저.
    if upload_on and svc is not None:
        try:
            sync_youtube(cfg, db, svc)
            fetch_thumbnails(cfg, db, cookies)  # 동기화로 업로드완료가 된 영상의 썸네일 표시용
        except Exception as e:  # noqa: BLE001
            log.warning("유튜브 동기화 실패: %r", e)
    # 방송일 오래된 순서대로. strict_order 면 앞 영상이 끝나야 다음 영상으로 (유튜브 업로드 순서 보장)
    strict = cfg["download"].get("strict_order", True)
    upload_disabled = not cfg["upload"]["enabled"]  # 설정에서 업로드를 끈 경우: 받기만 계속
    queue = sorted(db.by_status("DOWNLOADED", "NEW", "FAILED"), key=lambda x: (x["publish_ts"] or 0, x["video_no"]))

    def hold(r, why):
        log.warning("순서 유지: '%s' 이(가) %s → 이후 영상은 다음 실행으로 미룹니다.", r["title"], why)

    for r in queue:
        r = db.get(r["video_no"])
        if r["status"] == "FAILED":
            if strict:
                hold(r, "실패 상태 (목록에서 우클릭 → 재시도 또는 제외)")
                notify(f"⏸ 순서 유지 중: '{r['title']}' 실패 — 재시도하거나 제외해야 다음 영상이 진행됩니다",
                       once_per_day=f"hold_failed:{r['video_no']}")
                break
            continue
        if r["status"] == "NEW":
            if not do_download(cfg, db, r, cookies, notify):
                if strict:
                    r2 = db.get(r["video_no"])
                    hold(r2, r2["error"] or "아직 다운로드되지 않음")
                    break
                continue
            r = db.get(r["video_no"])
        if r["status"] == "DOWNLOADED" and not upload_on:
            if strict and not upload_disabled:
                hold(r, "업로드 불가 상태(쿼터/인증)")
                break
            continue
        if r["status"] == "DOWNLOADED" and upload_on:
            try:
                do_upload(cfg, db, r, svc, notify)
                if strict and db.get(r["video_no"])["status"] != "UPLOADED":
                    hold(r, "업로드 실패")
                    break
            except youtube.QuotaError as e:
                log.error("유튜브 쿼터/업로드 한도 초과(%s) — 오늘 업로드 중단", e)
                db.set_kv("quota_block", quota_day())
                notify(f"⚠️ 유튜브 업로드 한도 초과({e}). 내일 이어서 올립니다.", once_per_day="quota")
                upload_on = False
                if strict:
                    break
            except youtube.AuthError as e:
                log.error("%s", e)
                notify(f"⚠️ 유튜브 인증 오류: {e}", once_per_day="yt_auth")
                upload_on = False
                if strict:
                    break
    # 이전에 제한(429) 등으로 빠진 썸네일을 자동으로 채움
    if upload_on and svc is not None and cfg["upload"]["set_thumbnail"] and not _thumb_blocked:
        try:
            # 제한(429)이 나오면 그 자리에서 멈추고 쉬는 시간 뒤 실행에서 이어감
            ok, total = apply_missing_thumbnails(cfg, db, svc, limit=3)
            if total:
                log.info("빠진 썸네일 재적용: %d/%d", ok, total)
        except Exception as e:  # noqa: BLE001
            log.warning("썸네일 재적용 실패: %r", e)
    report.write(db, cfg.data_dir)


# ── 드라이런 ────────────────────────────────────────────
def dry_run(cfg, start_override: str | None = None, limit: int | None = None) -> list[dict]:
    """다운로드·업로드 없이 전체 로직 점검. 실제 state.db 는 건드리지 않는다."""
    import copy
    import tempfile

    print("── 1. 네이버 쿠키")
    cookies = cookie_mod.get_cookies(cfg)
    print("   유효" if cookies else "   없음/만료 → 공개 영상만 가능 (연령제한·멤버십은 실패 예상)")

    print("── 2. 유튜브 인증")
    if not cfg["upload"]["enabled"]:
        print("   업로드 꺼짐")
    else:
        try:
            svc = youtube.service(cfg.data_dir)
            ch = svc.channels().list(part="snippet", mine=True).execute()
            print("   OK →", ch["items"][0]["snippet"]["title"] if ch.get("items") else "(채널 없음!)")
        except Exception as e:
            print(f"   실패: {e}")

    print("── 3. 채널 스캔 (임시 DB)")
    tmp = Path(tempfile.mkdtemp()) / "dry.db"
    db = DB(tmp)
    cfg2 = copy.deepcopy(cfg)
    if start_override:
        for ch in cfg2.raw["channels"]:
            ch["start"] = start_override
    n = scan(cfg2, db, cookies)
    print(f"   대상 {n}개 (start={start_override or '설정값'})")

    print("── 4. 영상별 점검 (조회만, 다운로드 없음)")
    min_age = cfg["download"]["min_age_minutes"]
    rows = db.by_status("NEW")
    rows = rows[::-1][:limit] if limit else rows[::-1]
    out = []
    for r in rows:
        out_dir, stem = _target(cfg, r)
        nparts = _parts(cfg, r["duration"] or 0)
        age_min = (time.time() * 1000 - (r["publish_ts"] or 0)) / 60000
        rec = {"video_no": r["video_no"], "title": r["title"], "hours": round((r["duration"] or 0) / 3600, 2),
               "adult": bool(r["adult"]), "parts": nparts, "yt_titles": _titles(cfg, r, nparts),
               "waiting": age_min < min_age}
        if r["adult"] and not cookies:
            rec["ok"], rec["error"] = False, "연령제한 — 쿠키 없음 (실제 실행에선 재시도 소모 없이 쿠키 복구까지 대기)"
        else:
            try:
                rec.update(downloader.probe(cfg.repo, chzzk.video_url(r["video_no"]), cookies,
                                            out_dir, stem, cfg["download"]["resolution"]))
                rec["ok"] = True
            except Exception as e:
                rec["ok"], rec["error"] = False, str(e)
        out.append(rec)
        flag = "OK " if rec["ok"] else "NG "
        print(f"   {flag}{r['video_no']}  {rec['hours']:>5}h  {'19+' if rec['adult'] else '   '}  "
              f"{r['title'][:36]}")
        if rec["ok"]:
            size = f"{rec['size'] / 1e9:.1f}GB" if rec.get("size") else "크기미상(HLS)"
            print(f"        {rec['type']} · 가능 {rec['resolutions']} → {rec['resolution']}p · {size}"
                  f"{' · 게시 직후라 대기' if rec['waiting'] else ''}")
            print(f"        파일: {rec['path']}")
        else:
            print(f"        ! {rec['error']}")
        if nparts > 1:
            seg = _part_seconds(r["duration"], nparts)
            print(f"        분할: {nparts}개 × 약 {seg // 3600}시간 {seg % 3600 // 60}분 (균등)")
        for t in rec["yt_titles"]:
            print(f"        YT: {t}")
    db.conn.close()
    ok = sum(o["ok"] for o in out)
    print(f"── 결과: {ok}/{len(out)} 정상, 분할 필요 {sum(o['parts'] > 1 for o in out)}개")
    return out
