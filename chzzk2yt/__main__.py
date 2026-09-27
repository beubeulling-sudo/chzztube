"""치지직 다시보기 → 유튜브 비공개 업로드 자동화 CLI.

  uv run python -m chzzk2yt <명령> [-c config.toml]

명령:
  login-naver        네이버 로그인(최초 1회) — 이후 쿠키 자동 갱신
  auth-youtube       유튜브 계정 인증(최초 1회)
  dry-run [--start all]  다운로드·업로드 없이 전체 로직 점검
  run                한 번 실행 (스캔→다운로드→업로드)   ← 작업 스케줄러가 이걸 호출
  watch [-i 분]      계속 실행 (기본 30분 간격)
  status [-a]        상태표 출력 (기본: 미완료만, -a: 전체)
  add <URL...>       특정 다시보기 수동 추가
  retry <번호|all>   실패 항목 재시도
  skip <번호...>     처리 대상에서 제외
  cookies            현재 쿠키 상태 확인
  channel <URL>      채널 ID·이름 확인 (config에 넣을 값)
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from . import chzzk, config as config_mod, cookies as cookie_mod, pipeline, report, youtube
from .db import DB, jload

log = logging.getLogger("chzzk2yt")


def _setup_logging(data_dir: Path):
    (data_dir / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    if sys.stdout is not None:  # pythonw(창 없는 실행)에서는 stdout이 없음
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    fh = logging.handlers.TimedRotatingFileHandler(
        data_dir / "logs" / "chzzk2yt.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    # upstream 다운로더는 자체 로그 파일을 남기므로 콘솔엔 경고 이상만
    for noisy in ("googleapiclient", "urllib3", "DownloadLogger"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Lock:
    """동시 실행 방지 (스케줄러가 이전 실행이 안 끝났는데 또 부를 때)."""

    def __init__(self, path: Path):
        self.path = path
        self.f = None

    def __enter__(self):
        self.f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            log.info("이미 다른 실행이 진행 중입니다. 종료합니다.")
            sys.exit(0)
        return self

    def __exit__(self, *a):
        try:
            if os.name == "nt":
                import msvcrt

                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.f.close()


def _lock_free(path: Path) -> bool:
    """실행 중인 작업이 없으면 True (잠금을 잠깐 잡았다 놓아 확인)."""
    try:
        f = open(path, "a+")
    except OSError:
        return False
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        f.close()


def _print_status(db: DB, show_all: bool):
    rows = db.all()
    if not show_all:
        rows = [r for r in rows if r["status"] not in ("UPLOADED", "SKIPPED", "OUT")]
    if not rows:
        print("표시할 항목이 없습니다. (-a 로 전체 보기)")
    for r in rows:
        yt = ",".join(jload(r["youtube_ids"]))
        err = f"  ! {r['error'][:80]}" if r["error"] and r["status"] != "UPLOADED" else ""
        print(f"{r['video_no']:>10}  {report.LABEL.get(r['status'], r['status']):<10} "
              f"{(r['publish_date'] or '')[:10]}  {r['channel_name'] or '':<10.10} "
              f"{(r['title'] or '')[:40]:<40}  {yt}{err}")
    counts = {}
    for r in db.all():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n" + " · ".join(f"{report.LABEL[k]} {v}" for k, v in counts.items()))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="chzzk2yt", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login-naver")
    sub.add_parser("auth-youtube")
    sub.add_parser("run")
    ru = sub.add_parser("reupload", help="업로드완료로 잘못 표시된 영상을 다시 업로드 대기로 (로컬 파일 필요)")
    ru.add_argument("targets", nargs="+")
    t1 = sub.add_parser("thumb", help="지정한 영상에 썸네일 적용 (다시 적용 포함)")
    t1.add_argument("targets", nargs="+")
    td = sub.add_parser("thumb-done", help="썸네일이 이미 적용된 것으로 표시 (다시 올리지 않음)")
    td.add_argument("targets", nargs="+")
    th = sub.add_parser("thumbs", help="업로드된 영상 중 썸네일 없는 것에 일괄 적용")
    th.add_argument("--redo", action="store_true", help="이미 붙인 것도 전부 다시 (고화질로 교체)")
    sub.add_parser("scan", help="채널 목록만 새로고침 (다운로드·업로드 없음)")
    q = sub.add_parser("queue", help="'대상 아님'/제외 항목을 대기열에 추가")
    q.add_argument("targets", nargs="+")
    d = sub.add_parser("dry-run", help="다운로드·업로드 없이 로직 점검")
    d.add_argument("--start", default=None, help='채널 start 임시 변경 (예: all, 2026-09-01)')
    d.add_argument("--limit", type=int, default=None, help="점검할 영상 수")
    w = sub.add_parser("watch")
    w.add_argument("-i", "--interval", type=int, default=30, help="분")
    s = sub.add_parser("status")
    s.add_argument("-a", "--all", action="store_true")
    a = sub.add_parser("add")
    a.add_argument("urls", nargs="+")
    r = sub.add_parser("retry")
    r.add_argument("targets", nargs="+")
    k = sub.add_parser("skip")
    k.add_argument("targets", nargs="+")
    sub.add_parser("cookies")
    c = sub.add_parser("channel")
    c.add_argument("url")
    up = sub.add_parser("update", help="깃허브에서 최신 버전 받기 (설정·기록은 그대로)")
    up.add_argument("--check", action="store_true", help="확인만")
    up.add_argument("--force", action="store_true", help="버전이 같아도 다시 받기")
    args = ap.parse_args(argv)

    if args.cmd == "channel":  # 설정 파일 없이도 동작
        cid = chzzk.parse_channel_id(args.url)
        info = chzzk.channel_info(cid)
        print(f'id = "{cid}"   # {info.get("channelName")}')
        return

    cfg = config_mod.load(args.config)
    _setup_logging(cfg.data_dir)
    db = DB(cfg.data_dir / "state.db")

    if args.cmd == "update":
        from . import updater

        root = Path(args.config).resolve().parent
        try:
            if args.check:
                cur, new, need = updater.check(cfg)
                print(f"현재 v{cur} / 최신 v{new}" + (" → 업데이트 있음" if need else " (최신)"))
                notes = updater.whats_new(cur, new, cfg) if need else ""
                if notes:
                    print("\n이번 업데이트 내용:\n" + notes)
                return
            updater.apply(root, cfg, force=args.force)
        except Exception as e:  # noqa: BLE001
            print(f"업데이트 실패: {e}\n인터넷 연결을 확인하고 잠시 후 다시 해 보세요. (기존 파일은 그대로입니다)")
            sys.exit(1)
        return
    if args.cmd == "login-naver":
        cookie_mod.interactive_login(cfg.data_dir, cfg["cookies"]["browser_channel"])
    elif args.cmd == "auth-youtube":
        youtube.authorize(cfg.data_dir, bool(cfg["upload"].get("oauth_testing", True)))
    elif args.cmd == "cookies":
        ck = cookie_mod.get_cookies(cfg)
        print("유효" if ck else "없음/만료")
    elif args.cmd == "dry-run":
        pipeline.dry_run(cfg, args.start, args.limit)
    elif args.cmd == "reupload":
        if args.targets == ["all"]:
            rows_ = db.by_status("UPLOADED", "UPLOADING", "FAILED", "YT_DELETED", "YT_SHORT")
            targets = [str(x["video_no"]) for x in rows_]
            for k in [r_[0] for r_ in db.conn.execute("SELECT k FROM kv WHERE k LIKE 'upload_session:%'")]:
                db.conn.execute("DELETE FROM kv WHERE k=?", (k,))
            print(f"대상 {len(targets)}개 (업로드완료·업로드중·실패·유튜브에서 삭제됨) — 이어 올리기 세션도 초기화")
        else:
            targets = args.targets
        for t in targets:
            row = db.get(chzzk.parse_video_no(t))
            if row is None:
                print(f"[없음] {t}")
                continue
            files = [f for f in jload(row["files"]) if Path(f).exists()]
            if files:
                db.update(row["video_no"], status="DOWNLOADED", youtube_ids=None, thumb_ids=None,
                          ul_attempts=0, dl_attempts=0, error=None, files=files, yt_verified=None)
                for i in range(len(files) + 1):
                    db.conn.execute("DELETE FROM kv WHERE k=?", (f"upload_session:{row['video_no']}:{i}",))
                print(f"다시 업로드 대기: {row['video_no']} {row['title']} (파일 있음)")
            else:
                db.update(row["video_no"], status="NEW", youtube_ids=None, thumb_ids=None,
                          dl_attempts=0, ul_attempts=0, error=None, files=None,
                          local_duration=None, yt_verified=None)
                print(f"파일이 없어 다시 다운로드부터: {row['video_no']} {row['title']}")
        report.write(db, cfg.data_dir)
    elif args.cmd == "thumb":
        svc = youtube.service(cfg.data_dir)
        notify = pipeline.Notifier(cfg, db)
        for t in args.targets:
            row = db.get(chzzk.parse_video_no(t))
            ids = jload(row["youtube_ids"]) if row else []
            if not ids:
                print(f"[건너뜀] {t} — 아직 유튜브에 올라가지 않은 영상")
                continue
            stop = False
            for vid in ids:
                left = pipeline.thumb_pause_left(db)
                if left:
                    print(f"유튜브 썸네일 제한으로 쉬는 중 — {-(-left // 60)}분 뒤에 다시 하세요. "
                          "(자동 실행이 그 뒤 알아서 이어서 적용합니다. 쉬는 중 재시도하면 제한이 길어질 수 있어 시도하지 않음)")
                    stop = True
                    break
                ok = pipeline.apply_thumbnail(cfg, db, row["video_no"], vid, svc, notify)
                print(f"{'적용' if ok else '실패'}: {row['title']} → https://youtu.be/{vid}")
                if not ok and pipeline.thumb_pause_left(db):
                    print("  → 유튜브가 이 채널의 썸네일 변경을 잠시 제한했습니다 (영상 문제가 아님). "
                          "자동 실행이 쉬는 시간 뒤 이어서 적용합니다.")
                if pipeline._thumb_blocked:
                    print("썸네일 권한 문제로 나머지는 중단합니다 — youtube.com/verify 전화 인증을 확인하세요.")
                    stop = True
                    break
            if stop:
                break
        report.write(db, cfg.data_dir)
    elif args.cmd == "thumb-done":
        for t in args.targets:
            row = db.get(chzzk.parse_video_no(t))
            if row and jload(row["youtube_ids"]):
                db.update(row["video_no"], thumb_ids=jload(row["youtube_ids"]), thumb_unknown=0)
                print(f"썸네일 적용됨으로 표시: {row['title']}")
        report.write(db, cfg.data_dir)
    elif args.cmd == "thumbs":
        if args.redo:
            db.conn.execute("UPDATE videos SET thumb_ids=NULL WHERE status='UPLOADED'")
            print("모든 업로드 영상의 썸네일을 다시 적용 대상으로 표시했습니다. (제한에 걸리면 이후 실행에서 자동으로 이어감)")
        ok, total = pipeline.apply_missing_thumbnails(cfg, db)
        left = pipeline.thumb_pause_left(db)
        if total:
            print(f"썸네일 적용: {ok}/{total}")
        elif not left:
            print("썸네일이 빠진 업로드 영상이 없습니다.")
        if left:
            print(f"유튜브 썸네일 제한으로 쉬는 중 — 나머지는 {-(-left // 60)}분 뒤 자동 실행이 이어서 적용합니다.")
    elif args.cmd == "scan":
        if _lock_free(cfg.data_dir / ".lock"):
            db.recover_interrupted()  # 중지된 작업이 남긴 '다운로드중/업로드중' 표시 정리
        ck = cookie_mod._load_cache(cfg.data_dir)  # 목록 조회엔 저장된 쿠키로 충분 (브라우저 안 띄움)
        n = pipeline.scan(cfg, db, ck)
        pipeline.fetch_thumbnails(cfg, db, ck)
        if cfg["upload"]["enabled"]:
            try:
                pipeline.sync_youtube(cfg, db, youtube.service(cfg.data_dir))
                pipeline.fetch_thumbnails(cfg, db, ck)
            except Exception as e:  # noqa: BLE001
                print(f"[유튜브 동기화 생략] {e}")
        report.write(db, cfg.data_dir)
        counts = {}
        for r in db.all():
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        print(f"새 대상 {n}개 · " + " · ".join(f"{report.LABEL[k]} {v}" for k, v in counts.items()))
    elif args.cmd == "queue":
        for t in args.targets:
            row = db.get(chzzk.parse_video_no(t))
            if row is None:
                print(f"[없음] {t} — 목록에 없는 영상은 add 로 추가하세요")
            elif row["status"] in ("OUT", "SKIPPED"):
                db.update(row["video_no"], status="NEW", dl_attempts=0, ul_attempts=0, error=None)
                print(f"대기열 추가: {row['video_no']} {row['title']}")
            else:
                print(f"[그대로] {row['video_no']} 상태: {report.LABEL.get(row['status'])}")
        report.write(db, cfg.data_dir)
    elif args.cmd == "run":
        with Lock(cfg.data_dir / ".lock"):
            try:
                pipeline.run_once(cfg, db)
            except Exception:
                log.exception("실행 중 오류")  # 자동 실행(창 없음)에서도 로그 파일에 남도록
                raise
    elif args.cmd == "watch":
        while True:
            with Lock(cfg.data_dir / ".lock"):
                try:
                    pipeline.run_once(cfg, db)
                except Exception:
                    log.exception("실행 중 오류")
            log.info("%d분 후 다시 확인합니다.", args.interval)
            time.sleep(args.interval * 60)
    elif args.cmd == "status":
        report.write(db, cfg.data_dir)
        _print_status(db, args.all)
        print(f"\n상태표: {cfg.data_dir / 'status.html'}  /  {cfg.data_dir / 'status.csv'}")
    elif args.cmd == "add":
        ck = cookie_mod.get_cookies(cfg)
        for u in args.urls:
            try:
                no = pipeline.add_manual(cfg, db, u, ck)
                print(f"추가: {no} ({db.get(no)['title']})")
            except ValueError as e:
                print(f"[건너뜀] {e}")
            except Exception as e:  # noqa: BLE001
                code = getattr(getattr(e, "response", None), "status_code", None)
                msg = "영상을 찾을 수 없습니다 (삭제됐거나 번호가 틀림)" if code == 404 else repr(e)
                print(f"[실패] {u} — {msg}")
        report.write(db, cfg.data_dir)
    elif args.cmd in ("retry", "skip"):
        rows = db.by_status("FAILED") if args.targets == ["all"] else [
            db.get(chzzk.parse_video_no(t)) for t in args.targets]
        for row in filter(None, rows):
            if args.cmd == "skip":
                db.update(row["video_no"], status="SKIPPED")
            else:
                back = "DOWNLOADED" if row["failed_stage"] == "upload" and jload(row["files"]) else "NEW"
                db.update(row["video_no"], status=back, dl_attempts=0, ul_attempts=0, error=None)
            print(f"{args.cmd}: {row['video_no']} {row['title']}")
        report.write(db, cfg.data_dir)


if __name__ == "__main__":
    main()
