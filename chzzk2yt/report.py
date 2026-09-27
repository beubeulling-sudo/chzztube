"""상태표 자동 갱신 — data/status.csv (엑셀용), data/status.html (브라우저용)."""
from __future__ import annotations

import csv
import html
import time
from pathlib import Path

from .db import DB, jload

LABEL = {
    "NEW": "대기", "DOWNLOADING": "다운로드중", "DOWNLOADED": "다운완료·업로드대기",
    "UPLOADING": "업로드중", "UPLOADED": "업로드완료", "FAILED": "실패", "SKIPPED": "제외",
    "OUT": "보류(자동 대상 아님)", "YT_DELETED": "유튜브에서 삭제됨", "YT_SHORT": "유튜브 영상 잘림",
}
COLOR = {
    "UPLOADED": "#1a7f37", "FAILED": "#cf222e", "SKIPPED": "#8c959f", "OUT": "#8c959f",
    "YT_DELETED": "#9a6700", "YT_SHORT": "#cf222e",
    "DOWNLOADING": "#bf8700", "UPLOADING": "#bf8700", "DOWNLOADED": "#0969da", "NEW": "#57606a",
}


def _fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _dur(s):
    s = s or 0
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _thumb_state(r) -> str:
    ids = jload(r["youtube_ids"])
    if not ids:
        return ""
    done = set(jload(r["thumb_ids"])) if "thumb_ids" in r.keys() else set()
    n = sum(1 for v in ids if v in done)
    return f"적용 {n}/{len(ids)}" if n < len(ids) or len(ids) > 1 else "적용"


def write(db: DB, data_dir: Path):
    rows = db.all()
    headers = ["영상번호", "채널", "제목", "방송일", "길이", "상태", "해상도", "유튜브",
               "오류", "다운완료", "업로드완료", "썸네일"]
    recs = []
    for r in rows:
        yt = " ".join(f"https://youtu.be/{i}" for i in jload(r["youtube_ids"]))
        recs.append([r["video_no"], r["channel_name"], r["title"], r["publish_date"],
                     _dur(r["duration"]), LABEL.get(r["status"], r["status"]),
                     f'{r["resolution"]}p' if r["resolution"] else "", yt,
                     (r["error"] or "")[:200], _fmt_ts(r["downloaded_at"]), _fmt_ts(r["uploaded_at"]),
                     _thumb_state(r)])

    tmp = data_dir / "status.csv.tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(recs)
    try:
        tmp.replace(data_dir / "status.csv")
    except PermissionError:  # 엑셀에서 열어둔 경우
        pass

    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = " · ".join(f"{LABEL[k]} {counts[k]}" for k in LABEL if k in counts)
    trs = []
    for r, rec in zip(rows, recs):
        c = COLOR.get(r["status"], "#000")
        yt = " ".join(f'<a href="https://youtu.be/{i}">{i}</a>' for i in jload(r["youtube_ids"]))
        trs.append(
            "<tr>"
            f'<td><a href="https://chzzk.naver.com/video/{r["video_no"]}">{r["video_no"]}</a></td>'
            f"<td>{html.escape(r['channel_name'] or '')}</td>"
            f"<td>{html.escape(r['title'] or '')}</td><td>{rec[3] or ''}</td><td>{rec[4]}</td>"
            f'<td style="color:{c};font-weight:600">{rec[5]}</td><td>{rec[6]}</td><td>{yt}</td><td>{rec[11]}</td>'
            f'<td class="err">{html.escape(rec[8])}</td><td>{rec[10]}</td></tr>'
        )
    page = f"""<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>치지직→유튜브 상태</title>
<style>body{{font:14px system-ui,sans-serif;margin:20px}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f6f8fa;position:sticky;top:0}}.err{{color:#cf222e;font-size:12px;max-width:320px}}</style>
<h2>치지직 → 유튜브 상태</h2><p>갱신: {_fmt_ts(time.time())} · {summary}</p>
<table><tr><th>번호</th><th>채널</th><th>제목</th><th>방송일</th><th>길이</th><th>상태</th>
<th>화질</th><th>유튜브</th><th>썸네일</th><th>오류</th><th>업로드</th></tr>{''.join(trs)}</table>"""
    (data_dir / "status.html").write_text(page, encoding="utf-8")
