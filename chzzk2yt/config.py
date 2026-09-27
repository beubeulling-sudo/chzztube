"""config.toml 로드 + 기본값 병합."""
from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULTS = {
    "paths": {
        "download_dir": "downloads",
        "data_dir": "data",
        "downloader_repo": "vendor/chzzk-vod-downloader-v2",
    },
    "download": {
        "resolution": 1080,
        "min_age_minutes": 30,
        "max_attempts": 3,
        "timeout_hours": 12,
        "video_types": ["REPLAY"],
        "strict_order": True,
        "max_threads": 16,
        "engine": "direct",
    },
    "upload": {
        "enabled": True,
        "privacy": "public",
        "category_id": "20",
        "delete_after_upload": True,
        "playlist_id": "",
        "playlist_position": "bottom",
        "set_thumbnail": True,
        "oauth_testing": True,
        "max_part_hours": 11.5,
        "chunk_mb": 256,
        "title_template": "[{date}] {title}",
        "description_template": "{channel} 치지직 다시보기\n방송일: {date}\n원본: {url}\n",
    },
    "cookies": {
        "mode": "browser",
        "browser_channel": "msedge",
        "headless": True,
        "NID_AUT": "",
        "NID_SES": "",
    },
    "update": {"repo": "beubeulling-sudo/chzztube", "branch": "main"},
    "channels": [],
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    raw: dict
    root: Path

    def path(self, key: str) -> Path:
        p = Path(self.raw["paths"][key])
        return p if p.is_absolute() else (self.root / p)

    @property
    def download_dir(self) -> Path:
        return self.path("download_dir")

    @property
    def data_dir(self) -> Path:
        return self.path("data_dir")

    @property
    def repo(self) -> Path:
        return self.path("downloader_repo")

    def __getitem__(self, k):
        return self.raw[k]


def load(path: str | Path) -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise SystemExit(f"설정 파일이 없습니다: {path}\n  config.example.toml 을 config.toml 로 복사해 수정하세요.")
    with open(path, "rb") as f:
        raw = _merge(DEFAULTS, tomllib.load(f))
    valid = []
    for ch in raw["channels"]:
        ch.setdefault("start", "new")
        ch.setdefault("include_keywords", [])
        ch.setdefault("exclude_keywords", [])
        ch.setdefault("playlist_id", "")
        ch.setdefault("privacy", "")  # 비우면 [upload] privacy 따름
        cid = str(ch.get("id", ""))
        if len(cid) != 32 or set(cid) == {"0"}:
            print(f"[경고] 채널 ID가 비어 있거나 잘못돼 건너뜁니다: {cid!r} — 메뉴 '채널 관리'에서 추가하세요.")
            continue
        valid.append(ch)
    raw["channels"] = valid
    cfg = Config(raw, path.parent)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.download_dir.mkdir(parents=True, exist_ok=True)
    return cfg
