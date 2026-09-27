"""깃허브에서 최신 버전을 받아 프로그램 파일만 교체한다.

- 설정(config.toml), 기록·인증(data/), 받은 영상(downloads/), 다운로더(vendor/), 파이썬 환경(.venv/)은 건드리지 않는다.
- 바꾸기 전 파일은 data/backup/<이전버전>_<시각>/ 에 보관한다.
- 파이썬 패키지 목록(pyproject.toml, uv.lock)이 바뀌었으면 uv sync 로 맞춘다.
"""
from __future__ import annotations

import hashlib
import io
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

from . import __version__

DEFAULT_REPO = "beubeulling-sudo/chzztube"
DEFAULT_BRANCH = "main"
KEEP = ("config.toml", "data/", "downloads/", "vendor/", ".venv/", ".git")  # 절대 덮어쓰지 않음
DEPS = ("pyproject.toml", "uv.lock")


def _ver(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


def _src(cfg) -> tuple[str, str]:
    u = (cfg.raw.get("update") or {}) if cfg is not None else {}
    return u.get("repo") or DEFAULT_REPO, u.get("branch") or DEFAULT_BRANCH


def remote_version(cfg=None, timeout=15) -> str:
    import requests

    repo, br = _src(cfg)
    r = requests.get(f"https://raw.githubusercontent.com/{repo}/{br}/chzzk2yt/__init__.py",
                     timeout=timeout, headers={"Cache-Control": "no-cache"})
    r.raise_for_status()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', r.text)
    if not m:
        raise RuntimeError("원격 버전 정보를 찾지 못했습니다")
    return m.group(1)


def check(cfg=None) -> tuple[str, str, bool]:
    """(현재, 최신, 업데이트 필요?)"""
    rv = remote_version(cfg)
    return __version__, rv, _ver(rv) > _ver(__version__)


KINDS = ("추가", "개선", "수정")  # VERSION.txt 항목 분류 (보여 주는 순서)


def parse_notes(text: str) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """VERSION.txt → [(버전, 날짜, [(분류, 내용), ...]), ...]"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"##\s*v?(\d[\d.]*)\s*(?:\((.*?)\))?", s)
        if m:
            out.append((m.group(1), m.group(2) or "", []))
        elif out and s.startswith("-"):
            k = re.match(r"\[(.+?)\]\s*(.*)", s[1:].strip())
            out[-1][2].append((k.group(1), k.group(2)) if k else ("", s[1:].strip()))
    return out


def format_notes(entries, max_lines: int = 20) -> str:
    """현재보다 새로운 버전들의 변경 내역을 사람이 읽을 글로 만든다 (최신 버전부터)."""
    lines = []
    for v, d, items in sorted(entries, key=lambda e: _ver(e[0]), reverse=True):
        lines.append(f"■ v{v}" + (f" ({d})" if d else ""))
        order = {k: i for i, k in enumerate(KINDS)}
        for kind, text in sorted(items, key=lambda it: order.get(it[0], len(KINDS))):
            lines.append(f"  [{kind}] {text}" if kind else f"  · {text}")
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + ["  … (나머지는 VERSION.txt 참고)"]
    return "\n".join(lines)


def whats_new(cur: str, new: str, cfg=None, timeout=15) -> str:
    """깃허브의 VERSION.txt에서 cur 초과 ~ new 이하 버전의 변경 내역. 못 가져오면 빈 문자열."""
    try:
        import requests

        repo, br = _src(cfg)
        r = requests.get(f"https://raw.githubusercontent.com/{repo}/{br}/VERSION.txt",
                         timeout=timeout, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        text = r.content.decode("utf-8-sig", errors="replace")
    except Exception:  # noqa: BLE001 — 변경 내역은 없어도 업데이트는 할 수 있다
        return ""
    return format_notes([e for e in parse_notes(text) if _ver(cur) < _ver(e[0]) <= _ver(new)])


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest() if p.exists() else ""


def _uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    for c in (Path.home() / ".local" / "bin" / "uv.exe", Path.home() / ".cargo" / "bin" / "uv.exe",
              Path.home() / ".local" / "bin" / "uv"):
        if c.exists():
            return str(c)
    return None


def apply(root: Path, cfg=None, force: bool = False, log=print) -> bool:
    """업데이트 적용. 바뀐 게 있으면 True."""
    import requests

    cur, new, need = check(cfg)
    if not need and not force:
        log(f"이미 최신 버전입니다 (v{cur}).")
        return False
    repo, br = _src(cfg)
    notes = whats_new(cur, new, cfg) if need else ""
    if notes:
        log("이번 업데이트 내용:\n" + notes)
    log(f"새 버전 v{new} 받는 중… (현재 v{cur}, github.com/{repo})")
    r = requests.get(f"https://codeload.github.com/{repo}/zip/refs/heads/{br}", timeout=120)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = [n for n in z.namelist() if not n.endswith("/")]
    top = names[0].split("/", 1)[0] + "/"
    if not all(n.startswith(top) for n in names) or not any(n == top + "chzzk2yt/__init__.py" for n in names):
        raise RuntimeError("받은 파일 구조가 예상과 다릅니다 — 업데이트를 중단합니다")
    backup = root / "data" / "backup" / f"v{cur}_{time.strftime('%Y%m%d-%H%M%S')}"
    deps_before = {d: _md5(root / d) for d in DEPS}
    changed = []
    for n in names:
        rel = n[len(top):]
        if not rel or any(rel == k or rel.startswith(k) for k in KEEP) or ".." in Path(rel).parts:
            continue
        data = z.read(n)
        dst = root / rel
        if dst.exists() and dst.read_bytes() == data:
            continue
        if dst.exists():
            b = backup / rel
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, b)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".new")
        tmp.write_bytes(data)
        tmp.replace(dst)
        changed.append(rel)
    for rel in changed:
        log(f"  교체: {rel}")
    if not changed:
        log("바뀐 파일이 없습니다.")
        return False
    log(f"파일 {len(changed)}개 교체 완료. 이전 파일 보관: {backup}")
    if any(_md5(root / d) != deps_before[d] for d in DEPS):
        uv = _uv()
        if uv:
            log("파이썬 패키지 목록이 바뀌어 설치를 맞춥니다 (uv sync)…")
            rc = subprocess.call([uv, "sync", "-p", "3.13"], cwd=root)
            log("패키지 설치 완료" if rc == 0 else "⚠ 패키지 설치 실패 — 명령창 메뉴 23번(설치/업데이트)을 실행하세요")
        else:
            log("⚠ 패키지 목록이 바뀌었습니다 — 명령창 메뉴 23번(설치/업데이트)을 한 번 실행하세요")
    log(f"v{new} 업데이트 완료. 프로그램을 다시 켜면 적용됩니다.")
    return True
