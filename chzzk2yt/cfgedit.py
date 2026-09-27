"""config.toml 부분 수정 — 주석을 보존하기 위해 전체 재생성 대신 해당 줄만 바꾼다."""
from __future__ import annotations

import json
import re
from pathlib import Path


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def set_value(path: Path, section: str, key: str, value) -> None:
    """[section] 안의 `key = ...` 한 줄을 교체(없으면 섹션 끝에 추가). 줄 끝 주석은 유지."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_sec, sec_end, done, found = False, None, False, False
    pat = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)(\"(?:[^\"\\]|\\.)*\"|\[[^\]]*\]|[^#\s]+)(.*)$", re.S)
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("["):
            if in_sec and sec_end is None:
                sec_end = i
            in_sec = s == f"[{section}]"
            found = found or in_sec
            continue
        if in_sec:
            m = pat.match(ln.rstrip("\r\n"))
            if m:
                nl = "\r\n" if ln.endswith("\r\n") else "\n"
                lines[i] = m.group(1) + _toml_value(value) + m.group(3) + nl
                done = True
                break
    if not done:
        ins = f"{key} = {_toml_value(value)}\n"
        if sec_end is None and found:  # 섹션이 파일 맨 끝 → 끝에 추가 (같은 섹션을 또 만들면 TOML 오류)
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(ins)
        elif sec_end is None:
            lines.append(f"\n[{section}]\n{ins}")
        else:
            lines.insert(sec_end, ins)
    path.write_text("".join(lines), encoding="utf-8")


CH_HEADER = """# ── 감시할 채널 (여러 개 가능) ─────────────────────────────
# id: 채널 URL https://chzzk.naver.com/<여기 32자리> 부분
# start: "new"   = 처음 등록한 시점 이후 올라오는 것만 (기본)
#        "all"   = 과거 다시보기 전부
#        "2026-09-01" = 이 날짜 이후 게시분
"""


def write_channels(path: Path, channels: list[dict]) -> None:
    """파일 끝의 [[channels]] 블록 전체를 다시 쓴다 (채널은 항상 파일 마지막에 둔다)."""
    text = path.read_text(encoding="utf-8")
    cut = text.find("# ── 감시할 채널")
    if cut < 0:
        cut = text.find("[[channels]]")
    head = (text if cut < 0 else text[:cut]).rstrip() + "\n\n"
    out = [CH_HEADER]
    for ch in channels:
        name = ch.get("name", "")
        out.append("[[channels]]\n")
        out.append(f'id    = "{ch["id"]}"' + (f"   # {name}" if name else "") + "\n")
        if name:
            out.append(f"name  = {_toml_value(name)}\n")
        out.append(f"start = {_toml_value(ch.get('start', 'new'))}\n")
        out.append(f"include_keywords = {_toml_value(ch.get('include_keywords', []))}\n")
        out.append(f"exclude_keywords = {_toml_value(ch.get('exclude_keywords', []))}\n")
        out.append(f"playlist_id = {_toml_value(ch.get('playlist_id', ''))}\n\n")
    path.write_text(head + "".join(out), encoding="utf-8")
