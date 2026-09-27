"""치지직 공개 API — 채널 정보 / 다시보기 목록."""
from __future__ import annotations

import re

import requests

API = "https://api.chzzk.naver.com/service/v1"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
_s = requests.Session()
_s.headers["User-Agent"] = UA


def _get(url, cookies=None, **params):
    r = _s.get(url, params=params, cookies=cookies or {}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 200:
        raise RuntimeError(f"chzzk API 오류 {j.get('code')}: {j.get('message')}")
    return j["content"]


def channel_info(channel_id: str, cookies=None) -> dict:
    return _get(f"{API}/channels/{channel_id}", cookies)


def iter_videos(channel_id: str, cookies=None, page_size=30, max_pages=200):
    """최신순으로 채널 영상을 순회 (generator — 호출 측이 원하는 곳에서 멈춤)."""
    for page in range(max_pages):
        c = _get(
            f"{API}/channels/{channel_id}/videos",
            cookies,
            sortType="LATEST",
            pagingType="PAGE",
            page=page,
            size=page_size,
        )
        data = c.get("data") or []
        yield from data
        if page + 1 >= (c.get("totalPages") or 0) or not data:
            return


def video_detail(video_no: int, cookies=None) -> dict:
    return _get(f"https://api.chzzk.naver.com/service/v2/videos/{video_no}", cookies)


def to_row(v: dict) -> dict:
    ch = v.get("channel") or {}
    return {
        "video_no": int(v["videoNo"]),
        "channel_id": ch.get("channelId"),
        "channel_name": ch.get("channelName"),
        "title": v.get("videoTitle") or "",
        "video_type": v.get("videoType"),
        "category": v.get("videoCategoryValue") or "",
        "publish_date": v.get("publishDate"),
        "publish_ts": int(v.get("publishDateAt") or 0),
        "duration": int(v.get("duration") or 0),
        "thumbnail_url": v.get("thumbnailImageUrl") or "",
        "adult": int(bool(v.get("adult"))),
        "paid": int(bool(v.get("paidProductId"))),
    }


def parse_video_no(url_or_no: str) -> int:
    """다시보기 URL(…/video/12345678) 또는 숫자만 허용."""
    t = str(url_or_no).strip()
    if t.isdigit():
        return int(t)
    m = re.search(r"chzzk\.naver\.com/video/(\d+)", t)
    if m:
        return int(m.group(1))
    if re.search(r"chzzk\.naver\.com/[0-9a-f]{32}", t):
        raise ValueError("채널 주소입니다. 다시보기 주소(https://chzzk.naver.com/video/번호)를 넣거나, "
                         "채널 전체를 받으려면 '채널 관리'에서 채널을 추가하세요.")
    if "/clips/" in t:
        raise ValueError("클립은 지원하지 않습니다. 다시보기 주소(…/video/번호)를 넣어주세요.")
    raise ValueError(f"다시보기 주소가 아닙니다: {t}")


def parse_channel_id(url_or_id: str) -> str:
    m = re.search(r"([0-9a-f]{32})", str(url_or_id))
    if not m:
        raise ValueError(f"채널 ID(32자리)를 찾을 수 없습니다: {url_or_id}")
    return m.group(1)


def video_url(video_no: int) -> str:
    return f"https://chzzk.naver.com/video/{video_no}"
