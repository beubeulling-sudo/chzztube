"""YouTube Data API v3 업로드 (재개 가능 업로드 + 재시도)."""
from __future__ import annotations

import logging
import random
import time
from pathlib import Path

log = logging.getLogger("chzzk2yt.youtube")
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",  # 재생목록 추가용
]
RETRY_STATUS = {500, 502, 503, 504}
CHUNK = 64 * 1024 * 1024


class QuotaError(Exception):
    """일일 쿼터/업로드 한도 초과 — 오늘은 업로드 중단."""


class ThumbnailForbidden(Exception):
    """채널에 맞춤 썸네일 권한 없음 — youtube.com/verify 전화 인증 필요."""


class ThumbnailRateLimited(Exception):
    """맞춤 썸네일 업로드 횟수 제한(429) — 시간이 지나면 풀림."""


class AuthError(Exception):
    """토큰 만료/취소 — auth-youtube 재실행 필요."""


def _paths(data_dir: Path):
    return data_dir / "client_secret.json", data_dir / "youtube_token.json"


TESTING_TOKEN_DAYS = 7  # 구글 앱이 '테스트 중'이면 인증(동의) 시점부터 7일 뒤 토큰 만료


def _meta_path(data_dir: Path) -> Path:
    return data_dir / "youtube_token_meta.json"


def token_expiry(data_dir: Path) -> float | None:
    """'테스트 중' 앱 기준 토큰 만료 시각(epoch). 인증 시각 기록이 없으면 None.
    다시 인증하면 그 시점부터 새로 7일 — 일찍 해도 연장된다."""
    import json

    try:
        issued = json.loads(_meta_path(data_dir).read_text(encoding="utf-8"))["issued"]
    except Exception:  # noqa: BLE001
        return None
    return issued + TESTING_TOKEN_DAYS * 86400


def expiry_text(data_dir: Path, testing: bool = True) -> tuple[str, str]:
    """(표시 문구, 상태 ok/warn/bad/info)."""
    import time as _t

    if not testing:
        return "인증 만료 없음 (앱 게시됨)", "ok"
    exp = token_expiry(data_dir)
    if exp is None:
        return "인증 만료일 모름 — '유튜브 인증'을 다시 하면 표시", "warn"
    left = exp - _t.time()
    when = _t.strftime("%m-%d %H:%M", _t.localtime(exp))
    if left <= 0:
        return f"인증 만료됨 ({when}) — '유튜브 인증' 필요", "bad"
    d, h = int(left // 86400), int(left % 86400 // 3600)
    return f"인증 만료 {when} ({d}일 {h}시간 남음)", "warn" if left < 86400 else "ok"


def authorize(data_dir: Path, testing: bool = True):
    """브라우저로 구글 로그인 → 토큰 저장."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret, token = _paths(data_dir)
    if not secret.exists():
        raise SystemExit("구글 API 파일(client_secret.json)이 아직 없습니다.\n"
                         "프로그램의 '시작 가이드' 2단계에서 넣거나, 사용설명서 3장을 보고 만드세요.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    token.write_text(creds.to_json(), encoding="utf-8")
    import json
    import time as _t

    _meta_path(data_dir).write_text(json.dumps({"issued": int(_t.time())}), encoding="utf-8")
    svc = _build(creds)
    ch = svc.channels().list(part="snippet", mine=True).execute()
    if not ch.get("items"):
        print("⚠ 인증은 됐지만 이 구글 계정에는 유튜브 채널이 없습니다.\n"
              "  youtube.com 에서 채널을 만든 뒤(오른쪽 위 프로필 → 채널 만들기) '유튜브 인증'을 다시 하세요.\n"
              "  여러 채널이 있는 계정이면 로그인 화면에서 올릴 채널(브랜드 계정)을 골라야 합니다.")
        return
    name = ch["items"][0]["snippet"]["title"]
    print(f"유튜브 인증 완료: {name}")
    if testing:
        exp = token_expiry(data_dir)
        print(f"만료 예정: {_t.strftime('%Y-%m-%d %H:%M', _t.localtime(exp))} "
              f"(구글 앱 '테스트 중' 상태 → {TESTING_TOKEN_DAYS}일). 그 전에 다시 인증하면 그때부터 {TESTING_TOKEN_DAYS}일로 연장됩니다.")
    else:
        print("구글 앱이 게시(프로덕션) 상태 → 만료 없음")


def service(data_dir: Path):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    _, token = _paths(data_dir)
    if not token.exists():
        raise AuthError("유튜브 인증 전입니다 — 프로그램에서 '유튜브 인증'을 누르세요 (다운로드까지만 진행)")
    creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise AuthError(f"유튜브 인증 만료({e}) — 프로그램에서 '유튜브 인증'을 다시 누르세요") from e
        token.write_text(creds.to_json(), encoding="utf-8")
    return _build(creds)


def _build(creds):
    from googleapiclient.discovery import build

    svc = build("youtube", "v3", credentials=creds, cache_discovery=False)
    svc._c2y_creds = creds  # 직접 업로드(requests)용
    return svc


def fresh(svc):
    """새 연결을 쓰는 서비스 객체.
    긴 업로드 동안 놀던 기존 연결은 서버가 이미 끊어 둔 상태라, 재사용하면 윈도우에서
    ConnectionAbortedError(10053)가 난다 (업로드 직후 썸네일·재생목록 요청이 실패하던 원인)."""
    return _build(svc._c2y_creds)


def _reason(err) -> str:
    try:
        import json

        d = json.loads(err.content)
        return d["error"]["errors"][0]["reason"]
    except Exception:
        return ""


def _is_quota(err) -> bool:
    return _reason(err) in {"quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded",
                            "dailyLimitExceeded"}


def clean_title(t: str) -> str:
    t = t.replace("<", "(").replace(">", ")").strip() or "untitled"
    return t[:100]


def _snippet(title, description, tags, category_id, privacy) -> dict:
    tags_ok, total = [], 0
    for t in tags:
        t = t.replace("<", "").replace(">", "")
        if total + len(t) + 2 > 450:
            break
        tags_ok.append(t)
        total += len(t) + 2
    return {
        "snippet": {
            "title": clean_title(title),
            "description": description.replace("<", "(").replace(">", ")")[:4900],
            "tags": tags_ok,
            "categoryId": str(category_id),
            "defaultLanguage": "ko",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }


UPLOAD_URL = ("https://www.googleapis.com/upload/youtube/v3/videos"
              "?uploadType=resumable&part=snippet,status&notifySubscribers=false")


def _http():
    """송신 버퍼를 키운 세션 (윈도우 기본 버퍼가 작아 고속 회선에서 속도가 안 나오는 문제 대응)."""
    import socket

    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPConnection

    class Big(HTTPAdapter):
        def init_poolmanager(self, *a, **kw):
            kw["socket_options"] = HTTPConnection.default_socket_options + [
                (socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)]
            kw["blocksize"] = 1024 * 1024
            super().init_poolmanager(*a, **kw)

    s = requests.Session()
    s.mount("https://", Big())
    return s


class _Slice:
    """파일의 [start, start+length) 구간만 읽히는 file-like (메모리에 통째로 올리지 않음)."""

    def __init__(self, f, start, length):
        self.f, self.left = f, length
        f.seek(start)

    def __len__(self):
        return self.left

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        n = self.left if n is None or n < 0 else min(n, self.left)
        b = self.f.read(n)
        self.left -= len(b)
        return b


def _raise_api(r):
    class _E:  # _is_quota/_reason 재사용
        content = r.content
    if r.status_code in (400, 403, 429) and _is_quota(_E):  # 일일 업로드 개수 한도는 400 으로 옴
        raise QuotaError(_reason(_E))
    if r.status_code == 401:
        raise AuthError("유튜브 인증 만료 (401)")
    raise RuntimeError(f"업로드 오류 {r.status_code}: {r.text[:300]}")


def upload(svc, path: Path, title: str, description: str, tags: list[str],
           category_id: str, privacy: str, session=None, chunk_mb: int = 256) -> str:
    """재개 가능한 업로드. session=(get, set) 을 주면 세션 주소를 저장해 프로그램을 껐다 켜도 이어 올린다."""
    import time as _t

    from google.auth.transport.requests import Request

    creds = svc._c2y_creds
    http = _http()
    size = path.stat().st_size
    chunk = max(1, chunk_mb) * 1024 * 1024
    key = f"{path}|{size}|{int(path.stat().st_mtime)}"
    get, put = session if session else (lambda: None, lambda v: None)

    def auth():
        if not creds.valid:
            from google.auth.exceptions import RefreshError

            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise AuthError(f"유튜브 인증 만료({e}) — '유튜브 인증'을 다시 하세요") from e
        return {"Authorization": f"Bearer {creds.token}"}

    def start_session() -> str:
        r = http.post(UPLOAD_URL, json=_snippet(title, description, tags, category_id, privacy),
                      headers={**auth(), "X-Upload-Content-Length": str(size),
                               "X-Upload-Content-Type": "video/mp4"}, timeout=60)
        if r.status_code != 200 or "Location" not in r.headers:
            _raise_api(r)
        put({"key": key, "uri": r.headers["Location"], "at": int(_t.time())})
        return r.headers["Location"]

    def query_offset(uri):
        r = http.put(uri, headers={**auth(), "Content-Range": f"bytes */{size}"}, data=b"", timeout=60)
        if r.status_code in (200, 201):
            return None, r.json()
        if r.status_code == 308:
            rng = r.headers.get("Range")
            return (int(rng.split("-")[1]) + 1 if rng else 0), None
        if r.status_code in (404, 410):
            return -1, None  # 세션 만료
        _raise_api(r)

    saved = get()
    uri = saved["uri"] if saved and saved.get("key") == key else None
    offset = 0
    if uri:
        try:
            offset, done = query_offset(uri)
        except (QuotaError, AuthError):
            raise
        except Exception:  # noqa: BLE001
            offset, done = -1, None
        if done:
            put(None)
            return done["id"]
        if offset == -1:
            uri, offset = None, 0
        else:
            log.info("이전 업로드 이어서: %.1f%% 지점부터", offset / size * 100)
    if not uri:
        uri = start_session()

    log.info("업로드 시작: %s (%.2f GB, 조각 %dMB)", path.name, size / 1e9, chunk // 1048576)
    retry, t_begin, sent_begin = 0, _t.monotonic(), offset
    resp = None
    with open(path, "rb") as f:
        while resp is None:
            end = min(offset + chunk, size) - 1
            n = end - offset + 1
            t0 = _t.monotonic()
            try:
                r = http.put(uri, data=_Slice(f, offset, n),
                             headers={**auth(), "Content-Range": f"bytes {offset}-{end}/{size}",
                                      "Content-Length": str(n)}, timeout=(30, 900))
            except (QuotaError, AuthError):
                raise
            except Exception as e:  # noqa: BLE001  연결 끊김 등 → 서버가 받은 위치 확인 후 계속
                retry = _backoff(retry, e)
                try:
                    o, resp = query_offset(uri)
                except (QuotaError, AuthError):
                    raise
                except Exception:  # noqa: BLE001  확인 요청도 끊김 → 다시 기다렸다 시도
                    continue
                if resp is None and o == -1:
                    put(None)
                    raise RuntimeError("업로드 세션이 만료됐습니다 (다음 실행 때 처음부터)") from e
                offset = o if o is not None else offset
                continue
            if r.status_code in (200, 201):
                resp = r.json()
            elif r.status_code == 308:
                rng = r.headers.get("Range")
                offset = int(rng.split("-")[1]) + 1 if rng else 0
                retry = 0
                el = _t.monotonic() - t_begin
                avg = (offset - sent_begin) / el if el > 0 else 0
                eta = int((size - offset) / avg) if avg > 0 else 0
                log.info("업로드 %.1f%% | 이번 조각 %.1f MB/s | 평균 %.1f MB/s | 남은시간 %d:%02d:%02d",
                         offset / size * 100, n / 1e6 / max(_t.monotonic() - t0, 1e-6), avg / 1e6,
                         eta // 3600, eta % 3600 // 60, eta % 60)
            elif r.status_code in RETRY_STATUS:
                retry = _backoff(retry, RuntimeError(f"HTTP {r.status_code}"))
                try:
                    o, resp = query_offset(uri)
                except (QuotaError, AuthError):
                    raise
                except Exception:  # noqa: BLE001
                    continue
                if resp is None:
                    offset = max(o or 0, 0)
            elif r.status_code in (404, 410):
                put(None)
                raise RuntimeError("업로드 세션이 만료됐습니다 (다음 실행 때 처음부터)")
            else:
                _raise_api(r)
    put(None)
    if "id" not in resp:
        raise RuntimeError(f"업로드 응답 이상: {resp}")
    el = _t.monotonic() - t_begin
    log.info("업로드 완료: https://youtu.be/%s (평균 %.1f MB/s)", resp["id"],
             (size - sent_begin) / 1e6 / max(el, 1e-6))
    return resp["id"]


def _backoff(retry: int, err) -> int:
    retry += 1
    if retry > 10:
        raise RuntimeError(f"업로드 재시도 초과: {err!r}")
    wait = min(2 ** retry, 300) + random.random()
    log.warning("업로드 오류 %r → %.0f초 후 재시도 (%d/10)", err, wait, retry)
    time.sleep(wait)
    return retry


def list_my_uploads(svc) -> list[dict]:
    """내 채널 업로드 목록 전체 (비공개 포함). 50개당 쿼터 1."""
    ch = svc.channels().list(part="contentDetails", mine=True).execute()
    if not ch.get("items"):
        return []
    pl = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    out, token = [], None
    while True:
        r = svc.playlistItems().list(part="snippet", playlistId=pl, maxResults=50, pageToken=token).execute()
        for it in r.get("items", []):
            sn = it["snippet"]
            out.append({"id": sn["resourceId"]["videoId"], "title": sn.get("title", ""),
                        "description": sn.get("description", "")})
        token = r.get("nextPageToken")
        if not token:
            return out


def video_states(svc, ids: list[str]) -> dict[str, str]:
    """{영상ID: uploadStatus} — processed 만 '업로드 완료'로 믿을 수 있다.
    (업로드 중이거나 도중에 끊긴 영상도 목록엔 uploaded/processing 으로 보인다)"""
    out = {}
    for i in range(0, len(ids), 50):
        r = svc.videos().list(part="status", id=",".join(ids[i:i + 50])).execute()
        for it in r.get("items", []):
            out[it["id"]] = it["status"].get("uploadStatus", "")
    return out


def playlist_items(svc, playlist_id: str) -> list[str]:
    """재생목록의 영상 ID (현재 순서대로)."""
    out, token = [], None
    while True:
        r = svc.playlistItems().list(part="snippet", playlistId=playlist_id, maxResults=50,
                                     pageToken=token).execute()
        out += [i["snippet"]["resourceId"]["videoId"] for i in r.get("items", [])]
        token = r.get("nextPageToken")
        if not token:
            return out


def video_durations(svc, ids: list[str]) -> dict[str, tuple[str, int]]:
    """{영상ID: (uploadStatus, 길이초)}"""
    import re as _re

    out = {}
    for i in range(0, len(ids), 50):
        r = svc.videos().list(part="status,contentDetails", id=",".join(ids[i:i + 50])).execute()
        for it in r.get("items", []):
            m = _re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
                          it["contentDetails"].get("duration", "P0D"))
            d, h, mi, s = [int(x or 0) for x in m.groups()] if m else (0, 0, 0, 0)
            out[it["id"]] = (it["status"].get("uploadStatus", ""), d * 86400 + h * 3600 + mi * 60 + s)
    return out


def add_to_playlist(svc, playlist_id: str, video_id: str, top: bool = False, position: int | None = None):
    """재생목록에 추가. position 을 주면 그 위치로, 아니면 top=True 면 맨 위로 옮긴다.
    유튜브 재생목록 API는 연속 요청 시 409(SERVICE_UNAVAILABLE)를 자주 내서 재시도한다."""
    import time as _t

    def retry(fn):
        for a in range(6):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                if a == 5 or "409" not in str(e) and "SERVICE_UNAVAILABLE" not in str(e):
                    raise
                _t.sleep(3 + a * 2)

    res = {"kind": "youtube#video", "videoId": video_id}
    it = retry(lambda: svc.playlistItems().insert(
        part="snippet", body={"snippet": {"playlistId": playlist_id, "resourceId": res}}).execute())
    pos = position if position is not None else (0 if top else None)
    if pos is None:
        return "added"
    try:
        retry(lambda: svc.playlistItems().update(part="snippet", body={
            "id": it["id"], "snippet": {"playlistId": playlist_id, "resourceId": res, "position": pos}}).execute())
    except Exception as e:  # noqa: BLE001
        # 재생목록 정렬이 '수동'이 아니면 위치 지정만 거부됨 — 영상은 이미 추가된 상태
        if "manualSortRequired" in str(e):
            return "autosort"
        raise
    return "placed"


def thumb_candidates(url: str) -> list[str]:
    """치지직 썸네일은 목록 API가 500x280 축소본(?type=o500x280_blur)을 준다.
    같은 이미지의 1280x720(유튜브 권장 크기) → 원본 순으로 시도한다."""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if host == "pstatic.net" or host.endswith(".pstatic.net"):
        base = url.split("?")[0]
        return [base + "?type=w1280", base, url]
    return [url]


def download_thumbnail(image_url: str) -> bytes:
    """치지직 썸네일을 1280x720 우선으로 받아 bytes 로 반환 (유튜브 한도 2MB 이하)."""
    import time as _t

    import requests

    for u in thumb_candidates(image_url):
        r = None
        for _ in range(3):  # 네이버 이미지 서버가 가끔 연결을 끊음
            try:
                r = requests.get(u, timeout=30)
                r.raise_for_status()
                break
            except Exception:  # noqa: BLE001
                r = None
                _t.sleep(2)
        if r is not None and 0 < len(r.content) <= 2 * 1024 * 1024:
            return r.content
    raise RuntimeError("썸네일 이미지를 받지 못했습니다")


def set_thumbnail(svc, video_id: str, image_url: str, tmp_dir: Path):
    set_thumbnail_file(svc, video_id, None, tmp_dir, download_thumbnail(image_url))


def set_thumbnail_file(svc, video_id: str, path: Path | None, tmp_dir: Path, data: bytes | None = None):
    """로컬에 받아 둔 썸네일 파일(또는 bytes)을 유튜브 영상에 적용."""
    import time as _t

    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    if data is None:
        data = Path(path).read_bytes()
    mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    p = tmp_dir / f"thumb_{video_id}.img"
    p.write_bytes(data)
    try:
        for attempt in range(3):
            try:
                svc.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(p), mimetype=mime)).execute()
                break
            except (ConnectionError, OSError) as ce:  # 끊긴 연결 재사용 등 → 새 연결로 재시도
                if attempt == 2:
                    raise
                log.info("썸네일 요청 연결 끊김(%r) → 새 연결로 재시도", ce)
                svc = fresh(svc)
                _t.sleep(2)
    except HttpError as e:
        if _is_quota(e):
            raise QuotaError(_reason(e)) from e
        if e.resp.status == 403:
            raise ThumbnailForbidden("이 유튜브 채널은 맞춤 썸네일 권한이 없습니다. "
                                     "https://www.youtube.com/verify 에서 전화 인증 후 '썸네일 일괄 적용'을 실행하세요.") from e
        if e.resp.status == 429 or _reason(e) == "uploadRateLimitExceeded":
            raise ThumbnailRateLimited("썸네일 업로드 횟수 제한(채널 단위) — 잠시 쉬었다가 자동으로 이어서 적용합니다.") from e
        if _is_quota(e):
            raise QuotaError(_reason(e)) from e
        raise
    finally:
        p.unlink(missing_ok=True)
