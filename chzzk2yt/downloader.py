"""chzzk-vod-downloader-v2 의 헤드리스 파이프라인을 라이브러리처럼 호출.

새 다운로드 로직을 쓰지 않고 upstream scripts/headless_download.py 의
_fetch/_build_item/_HeadlessRunner 흐름을 그대로 재사용한다.
upstream 내부 함수에 의존하므로 setup.ps1 에서 커밋을 고정(pin)한다.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("chzzk2yt.download")
# 자동 실행(pythonw, 콘솔 없음)에서 ffmpeg 같은 콘솔 프로그램을 부르면 명령 프롬프트 창이 잠깐 떴다 꺼진다 → 창 숨김
_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
_hd = None


class DownloadError(Exception):
    pass


def _load(repo: Path):
    global _hd
    if _hd is not None:
        return _hd
    script = repo / "scripts" / "headless_download.py"
    if not script.exists():
        raise SystemExit(f"다운로더 저장소가 없습니다: {repo}\n  setup.ps1 을 먼저 실행하세요.")
    sys.path.insert(0, str(repo))
    spec = importlib.util.spec_from_file_location("cvd_headless", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _hd = mod
    return mod


def _pick_resolution(reps: list, preferred: int) -> int:
    res = sorted({r[0] for r in reps})
    below = [r for r in res if r <= preferred]
    return below[-1] if below else res[0]


def download(repo: Path, url: str, cookies: dict, out_dir: Path, file_stem: str,
             preferred_res: int, timeout_s: int, max_threads: int = 16) -> tuple[Path, int]:
    """영상을 받아 (파일경로, 해상도)를 반환. 실패 시 DownloadError."""
    hd = _load(repo)
    from core.utils.paths import build_output_path  # upstream

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        result, ctype = hd.metadata_service.fetch_content(
            url, cookies, str(out_dir), api=hd.NetworkManager
        )
    except hd.MetadataError as e:
        raise DownloadError(f"조회 실패: {str(e).replace(chr(10), ' | ')}") from e
    except Exception as e:  # 네트워크 등
        raise DownloadError(f"조회 실패: {e!r}") from e

    res = _pick_resolution(result[2], preferred_res)
    item = hd._build_item(result, ctype, res)
    if item is None:
        raise DownloadError("해상도 선택 실패")
    # 파일명을 우리 규칙으로 (날짜_번호_제목) — 중복/식별성 확보
    item.output_path = build_output_path(str(out_dir), file_stem, res)

    # upstream 은 다운로드 시작 직전(m3u8 주소·AES 키 조회)에 자기 설정 파일의 쿠키를 다시 읽는다.
    # 그 쿠키가 비어 있으면 연령제한 영상이 여기서 실패하므로 우리 쿠키를 쓰도록 바꿔 끼운다.
    import app.download_resolvers as _res

    _res._load_cookies = lambda: dict(cookies)

    # 동시 연결 상한. upstream 기본 48은 치지직 CDN이 연결을 강제로 끊는(10054) 일이 잦고,
    # 끊긴 조각이 병합을 망가뜨린 사례가 있어 낮춘다 (16개로도 40~60MB/s 나옴).
    import core.downloaders.base as _base

    _base._TARGET_CAP = max(4, int(max_threads))

    class Runner(hd._HeadlessRunner):
        _last = 0.0

        def _on_progress(self, event, data):  # 30초마다만 로그
            t = time.monotonic()
            if t - self._last >= 30:
                self._last = t
                super()._on_progress(event, data)

    log.info("다운로드: %s (%sp, type=%s)", url, res, ctype)
    code = Runner(item, timeout_s).run()
    path = Path(item.output_path)
    if code != 0 or not path.exists() or path.stat().st_size == 0:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        raise DownloadError(f"다운로드 실패 (exit={code})")
    return path, res


def probe(repo: Path, url: str, cookies: dict, out_dir: Path, file_stem: str,
          preferred_res: int) -> dict:
    """다운로드 없이 조회만: 스트림 종류·해상도·저장 경로·(가능하면) 파일 크기."""
    hd = _load(repo)
    from core.utils.paths import build_output_path

    try:
        result, ctype = hd.metadata_service.fetch_content(
            url, cookies, str(out_dir), api=hd.NetworkManager
        )
    except hd.MetadataError as e:
        raise DownloadError(f"조회 실패: {str(e).replace(chr(10), ' | ')}") from e
    except Exception as e:
        raise DownloadError(f"조회 실패: {e!r}") from e
    reps = sorted({r[0] for r in result[2]})
    res = _pick_resolution(result[2], preferred_res)
    base_url = next((r[1] for r in result[2] if r[0] == res), None)
    size = None
    if base_url and ctype not in ("m3u8", "hls_aes"):
        try:
            import requests

            h = requests.head(base_url, timeout=15, allow_redirects=True)
            size = int(h.headers.get("Content-Length", 0)) or None
        except Exception:
            pass
    # 존재 여부와 무관하게 경로 규칙만 확인 (폴더는 만들지 않음)
    path = build_output_path(str(out_dir), file_stem, res)
    return {"type": ctype, "resolutions": reps, "resolution": res, "path": path, "size": size}


def media_duration(path: Path) -> float:
    """ffmpeg 로 파일의 실제 재생 길이(초)를 읽는다. 실패 시 0."""
    import re

    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **_NO_WINDOW)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


class VerifyError(DownloadError):
    pass


def verify_media(path: Path, expected_s: float | None = None, tol: float = 0.02) -> float:
    """파일 검증: ① 영상·음성 스트림 존재 ② 길이가 치지직 길이의 ±tol 이내
    ③ 앞·중간·끝 3곳을 실제로 디코딩해 오류가 없는지. 통과하면 실제 길이(초)를 반환."""
    import re

    ff = ffmpeg_exe()
    r = subprocess.run([ff, "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **_NO_WINDOW)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr)
    if not m:
        raise VerifyError("길이를 읽을 수 없는 파일")
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    if " Video:" not in r.stderr:
        raise VerifyError("영상 스트림 없음")
    if " Audio:" not in r.stderr:
        raise VerifyError("음성 스트림 없음")
    if expected_s:
        ratio = dur / expected_s
        if not (1 - tol) <= ratio <= (1 + tol):
            raise VerifyError(f"길이 불일치: 파일 {_hms(dur)} / 치지직 {_hms(expected_s)} ({ratio:.1%})")
    for t in (0, max(0, dur / 2), max(0, dur - 15)):
        d = subprocess.run([ff, "-v", "error", "-ss", f"{t:.1f}", "-i", str(path), "-t", "5", "-map", "0",
                            "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", **_NO_WINDOW)
        if d.returncode != 0 or d.stderr.strip():
            raise VerifyError(f"{_hms(t)} 지점 디코딩 오류: {d.stderr.strip()[:200] or d.returncode}")
    return dur


def _hms(s) -> str:
    s = int(s or 0)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


class _SegmentsBad(DownloadError):
    pass


def fmp4_fix(b: bytes) -> tuple[bytes | None, str]:
    """HLS fMP4 조각 구조 검사. (쓸 바이트, 상태) 반환.
    상태: ok / salvaged(앞부분이 깨져 뒤쪽 온전한 부분만 살림) / bad(버림).
    치지직 서버에 앞부분이 깨진 조각이 실제로 있고(예: 14519815 의 3555번),
    이걸 그대로 이어 붙이면 ffmpeg 가 그 지점에서 '파일 끝'으로 판단해 뒤가 통째로 잘린다."""
    import struct

    def chain(o):
        n_moof = n_mdat = 0
        while o < len(b):
            if o + 8 > len(b):
                return False
            sz, t = struct.unpack(">I4s", b[o:o + 8])
            if sz == 1:
                if o + 16 > len(b):
                    return False
                sz = struct.unpack(">Q", b[o + 8:o + 16])[0]
            if sz < 8 or o + sz > len(b) or not t.isalpha():
                return False
            n_moof += t == b"moof"
            n_mdat += t == b"mdat"
            o += sz
        return n_moof > 0 and n_mdat > 0

    if len(b) >= 8 and b[4:8] in (b"styp", b"moof") and chain(0):
        return b, "ok"
    i = b.find(b"moof", 4)
    while i != -1:
        if chain(i - 4):
            return b[i - 4:], "salvaged"
        i = b.find(b"moof", i + 1)
    return None, "bad"


def _pick_variant(master_text: str, base: str, preferred: int) -> tuple[str, int]:
    import re
    from urllib.parse import urljoin

    lines = master_text.splitlines()
    vars_ = []
    for i, ln in enumerate(lines):
        m = re.search(r"RESOLUTION=\d+x(\d+)", ln)
        if m and i + 1 < len(lines):
            vars_.append((int(m.group(1)), urljoin(base, lines[i + 1].strip())))
    if not vars_:
        raise DownloadError("화질 목록이 비어 있습니다")
    below = [v for v in vars_ if v[0] <= preferred]
    return max(below) if below else min(vars_)


def download_direct(repo: Path, video_no: int, cookies: dict, out_dir: Path, file_stem: str,
                    preferred_res: int, timeout_s: int, threads: int = 16, on_progress=None) -> tuple[Path, int]:
    """m3u8 조각을 직접 받아 순서대로 이어 붙이고 ffmpeg 로 mp4 변환 (재인코딩 없음).
    조각마다: 크기(Content-Length) 확인 → fMP4 구조 확인 → 깨졌으면 재시도 →
    그래도 깨진 조각(서버 원본 손상)은 살릴 수 있는 부분만 쓰고 기록한다."""
    import concurrent.futures as cf
    import json
    from urllib.parse import urljoin

    import requests

    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0"
    if cookies:
        sess.cookies.update({k: v for k, v in cookies.items() if k.startswith("NID")})
    a = requests.adapters.HTTPAdapter(pool_connections=threads, pool_maxsize=threads * 2)
    sess.mount("https://", a)
    r = sess.get(f"https://api.chzzk.naver.com/service/v2/videos/{video_no}", timeout=30)
    r.raise_for_status()
    c = (r.json() or {}).get("content") or {}
    if not c.get("liveRewindPlaybackJson"):
        raise _NoHls("m3u8(다시보기) 정보가 없는 영상")
    master = json.loads(c["liveRewindPlaybackJson"])["media"][0]["path"]
    res, url = _pick_variant(sess.get(master, timeout=30).text, master, preferred_res)
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from core.utils.paths import build_output_path  # upstream 과 같은 파일명 규칙

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(build_output_path(str(out_dir), file_stem, res))
    lines = sess.get(url, timeout=30).text.splitlines()
    maps = {ln.split('URI="')[1].split('"')[0] for ln in lines if ln.startswith("#EXT-X-MAP")}
    if len(maps) > 1:
        raise _NoHls("초기화 조각이 여러 개인 스트림")
    segs, durs, cur = [], [], 2.0
    for ln in lines:
        if ln.startswith("#EXTINF"):
            cur = float(ln.split(":")[1].split(",")[0])
        elif ln and not ln.startswith("#"):
            segs.append(urljoin(url, ln.strip()))
            durs.append(cur)
    if not segs:
        raise DownloadError("m3u8 에 조각이 없습니다")

    def fetch(u, check=True):
        """조각 1개 받기. 깨진 조각은 CDN 노드별로 다를 수 있어(같은 조각이 다른 시점엔 정상이었음)
        새 연결로 여러 번 다시 받아 보고, 끝까지 깨져 있을 때만 살릴 수 있는 부분을 쓴다."""
        last = None
        bad_tries = 0
        for i in range(12):
            try:
                if bad_tries:  # 다른 서버(엣지)에 붙도록 새 연결 사용
                    rr = requests.get(u, timeout=60, cookies=sess.cookies,
                                      headers={"User-Agent": "Mozilla/5.0", "Connection": "close",
                                               "Cache-Control": "no-cache"})
                else:
                    rr = sess.get(u, timeout=60)
                rr.raise_for_status()
                cl = rr.headers.get("Content-Length")
                if cl and int(cl) != len(rr.content):
                    raise OSError("크기 불일치")
                if not check:
                    return rr.content, "ok"
                data, st = fmp4_fix(rr.content)
                if st == "ok":
                    return data, "ok" if not bad_tries else "retried"
                last = (data, st)
                bad_tries += 1
                if bad_tries >= 6:
                    return last
                time.sleep(min(30, 2 ** bad_tries))
                continue
            except Exception:  # noqa: BLE001
                if i >= 5 and last is None:
                    raise
                if last is not None and i >= 11:
                    return last
            time.sleep(1 + min(i, 5) * 3)
        if last is None:
            raise DownloadError("조각 다운로드 실패")
        return last

    raw = out_path.with_name(out_path.stem + ".direct.part")
    log.info("다운로드(직접): %sp, 조각 %d개(%s), %s", res, len(segs), _hms(sum(durs)), out_path.name)
    t0 = last_log = time.monotonic()
    done_bytes = 0
    lost = []
    try:
        with open(raw, "wb") as f, cf.ThreadPoolExecutor(threads) as ex:
            if maps:
                f.write(fetch(urljoin(url, maps.pop()), check=False)[0])
            win = threads * 4
            for i in range(0, len(segs), win):
                for k, (data, st) in enumerate(ex.map(fetch, segs[i:i + win])):
                    if st == "retried":
                        log.info("깨진 조각 %d번 → 다시 받아 정상본 확보", i + k)
                        st = "ok"
                    if st != "ok":
                        lost.append((i + k, st, durs[i + k]))
                        log.warning("치지직 원본 조각 손상: %d번 (%s 지점) → %s", i + k,
                                    _hms(sum(durs[:i + k])), "앞부분 버리고 살림" if data else "건너뜀")
                    if data:
                        f.write(data)
                        done_bytes += len(data)
                now_ = time.monotonic()
                if on_progress:  # 조각 수 기준 진행률, 속도는 받은 조각 수/초 (남은 시간 계산용)
                    got_n = min(i + win, len(segs))
                    on_progress("다운로드", got_n, len(segs), got_n / max(now_ - t0, 1e-6))
                if now_ - last_log > 30:
                    last_log = now_
                    pct = min(100, (i + win) * 100 // len(segs))
                    spd = done_bytes / 1e6 / (now_ - t0)
                    log.info("다운로드 %d%% | %.2f GB | %.1f MB/s", pct, done_bytes / 1e9, spd)
                if now_ - t0 > timeout_s:
                    raise DownloadError("다운로드 제한 시간 초과")
        lost_s = sum(x[2] for x in lost if x[1] == "bad")
        if lost_s > max(30, sum(durs) * 0.01):
            raise DownloadError(f"손상된 조각이 너무 많습니다 ({len(lost)}개, {_hms(lost_s)})")
        log.info("조각 수신 완료 %.2f GB (%.0f분)%s → mp4 변환", done_bytes / 1e9, (time.monotonic() - t0) / 60,
                 f", 원본 손상 조각 {len(lost)}개 처리" if lost else "")
        if on_progress:
            on_progress("변환")
        tmp = out_path.with_name(out_path.stem + ".remux.part.mp4")
        subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
                        "-map", "0", "-c", "copy", str(tmp)], check=True, **_NO_WINDOW)
        os.replace(tmp, out_path)
    finally:
        raw.unlink(missing_ok=True)
    log.info("다운로드 완료(직접): %.2f GB, %.0f분", out_path.stat().st_size / 1e9, (time.monotonic() - t0) / 60)
    return out_path, res


class _NoHls(DownloadError):
    pass


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def split(path: Path, part_seconds: int) -> list[Path]:
    """무재인코딩(-c copy)으로 part_seconds 단위 분할. 원본은 삭제."""
    pattern = path.with_name(f"{path.stem} part%02d{path.suffix}")
    cmd = [
        ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        "-map", "0", "-c", "copy", "-f", "segment", "-segment_time", str(part_seconds),
        "-reset_timestamps", "1", str(pattern),
    ]
    log.info("분할: %s (%d초 단위)", path.name, part_seconds)
    subprocess.run(cmd, check=True, **_NO_WINDOW)
    import re as _re

    pat = _re.compile(_re.escape(path.stem) + r" part\d\d" + _re.escape(path.suffix) + "$")
    parts = sorted(p for p in path.parent.iterdir() if pat.match(p.name))  # 제목의 [ ] 가 glob 패턴으로 해석되지 않게
    if len(parts) < 1:
        raise DownloadError("분할 결과가 없습니다")
    os.remove(path)
    return parts
