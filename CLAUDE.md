# CLAUDE.md — chzzk2yt 작업 안내

치지직(CHZZK) 다시보기를 받아 검증한 뒤, 방송일 순서대로 유튜브에 올리는 Windows용 도구다.
사용자는 비개발자 지인들에게 zip/깃허브로 배포한다. 대답과 UI 문구는 한국어로 쓴다.

## 구조
| 파일 | 역할 |
|---|---|
| `chzzk2yt/__main__.py` | CLI (`run`, `scan`, `dry-run`, `update`, `thumb`, `thumbs`, `thumb-done`, `reupload`, `queue`, `retry`, `skip`, `add` …) |
| `chzzk2yt/pipeline.py` | 스캔 → 다운로드 → 검증 → (분할) → 업로드 → 재생목록 → 썸네일. `run_once`가 본체 |
| `chzzk2yt/downloader.py` | `download_direct`(자체 HLS 다운로더, 기본) + upstream 다운로더 호출(대체용), `verify_media`, `split` |
| `chzzk2yt/youtube.py` | 재개 가능 업로드, 재생목록, 썸네일, 토큰 만료 계산 |
| `chzzk2yt/db.py` | SQLite 상태 DB (`data/state.db`). 컬럼 추가는 `DB.__init__`의 마이그레이션에 |
| `chzzk2yt/gui.py` | PySide6 UI (틀 없는 창 + 직접 그린 제목 표시줄, 트레이, 채널 탭, 시작 가이드) |
| `chzzk2yt/updater.py` | 깃허브 zip을 받아 파일 교체 (`config.toml`, `data/`, `downloads/`, `vendor/`, `.venv/`는 제외) |
| `chzzk2yt/menu.py` | `menu.bat`이 여는 번호 메뉴 |
| `chzzk2yt/cookies.py` | 네이버 쿠키 (Playwright 전용 프로필) |
| `setup.ps1` | uv 설치 → upstream 다운로더(커밋 고정) → `uv sync -p 3.13` → config 생성 |

- upstream: honey720/chzzk-vod-downloader-v2. 내부 함수를 재사용하므로 `setup.ps1`에서 커밋을 고정한다. 바꾸려면 테스트한 뒤 올린다.

## 상태 흐름
`NEW → DOWNLOADING → DOWNLOADED → UPLOADING → UPLOADED`
- 그 밖의 상태: `FAILED`, `SKIPPED`, `OUT`(채널 등록 전이거나 필터로 빠진 방송 = "보류"), `YT_DELETED`, `YT_SHORT`.

## 지켜야 할 규칙 (이유가 있어서 이렇게 됨)
- **엄격한 방송일 순서**
  - `FAILED`가 있으면 그 뒤 영상은 진행하지 않는다.
  - 쿼터 초과나 인증 오류가 나도 멈춘다.
  - 설정에서 업로드를 꺼 둔 경우에만 다운로드는 계속한다.
- **다운로드 검증**
  - 조각마다 Content-Length와 fMP4 박스 구조(`fmp4_fix`)를 검사한다. 깨진 조각은 새 연결로 최대 6번 다시 받는다.
  - 그래도 깨진 조각은 그 자리에서 파일을 끊어 따로 두고, 다운로드가 끝난 뒤 1·3·10분 간격으로 그 조각만 다시 받는다(`RETRY_LATER`). CDN 캐시가 몇 분간 깨진 복사본을 주는 경우가 실제로 있었고, 살려 붙인 파일은 유튜브 처리가 멈출 수 있다. 조각마다 절대 시각(tfdt)이 있어 제자리에 끼우면 싱크 문제가 없다. 주소가 만료됐으면 조각 목록을 새로 받는다.
  - 끝까지 깨져 있으면 살릴 수 있는 부분만 쓰고 `videos.salvaged`에 개수를 남긴다. 손실이 1%(최소 30초)를 넘으면 실패로 처리한다.
  - 치지직 CDN에 앞부분이 깨진 조각이 실제로 있다. 그대로 이어 붙이면 ffmpeg가 그 지점을 파일 끝으로 보고 뒤를 통째로 잘라 버린다.
  - `verify_media`: 길이가 치지직 길이의 ±2% 안이어야 하고, 앞·중간·끝 세 곳을 디코딩해 본다. 분할하면 조각별로 다시 검증하고 합계 길이도 확인한다.
- **12시간 초과 방송**
  - `max_part_hours`(11.5) 단위로 균등 분할한다(`-c copy`).
  - 제목 끝에 ` (n/m)`을 붙이고, 모든 파트에 같은 썸네일을 적용한다.
- **썸네일 429는 채널 단위 제한이다** (영상별 아님)
  - kv의 `thumb_pause_until`/`thumb_pause_streak`으로 15→30→60→120→180분 쉰다.
  - 쉬는 동안에는 시도하지 않는다(시도하면 제한이 늘어남).
  - 빠진 썸네일은 업로드 후 1개, 실행 끝에 3개씩 따라잡는다.
- **썸네일 403**은 채널 전화 인증이 안 된 것이다. 이번 실행에서는 더 시도하지 않는다.
- **유튜브에 이미 있던 영상** (`sync_youtube`가 찾아낸 것)
  - `thumb_unknown=1`로 두고 자동으로 다시 적용하지 않는다.
  - UI에는 "기존 영상 · 확인 안 함"으로 표시한다. 우클릭 메뉴로 적용하거나 '적용됨'으로 표시할 수 있다.
- **재생목록**
  - 채널별 `playlist_id`가 기본 재생목록보다 우선한다.
  - 위치 지정이 `manualSortRequired`(400)로 거부돼도 영상은 이미 들어가 있으므로 성공으로 본다. 그 재생목록은 `playlist_autosort:<id>`에 24시간 기록해 위치 지정을 건너뛴다.
- **공개 범위** (`pipeline.privacy_of`)
  - 우선순위: 영상별 값(`videos.privacy`, 올라가기 전) > 채널별 `privacy` > `[upload] privacy`.
  - `videos.privacy`는 올라간 영상이면 유튜브의 실제 값(`sync_youtube`가 갱신), 아니면 그 영상만 따로 정한 값이다.
  - `youtube.set_privacy`는 현재 status를 읽어 privacyStatus만 바꿔 보낸다. `videos.update`는 빠진 항목을 기본값으로 되돌리기 때문이다.
- **업로드 한도**
  - 일일 업로드 개수 한도는 400(`uploadLimitExceeded`)으로 온다. 400/403/429 모두 쿼터로 판정한다.
  - 태평양 시간 자정까지 `quota_block`을 건다.
- **7일 인증 만료**
  - 구글 앱이 '테스트 중'이면 토큰이 7일 뒤 만료된다.
  - 인증한 시각을 `data/youtube_token_meta.json`에 기록한다. 만료 하루 전부터 트레이 알림과 윈도우 토스트(하루 1회)를 띄운다.
- **업로드 후 다른 요청** (썸네일·재생목록)
  - 반드시 `youtube.fresh(svc)`로 새 연결을 쓴다. 긴 업로드 뒤 기존 연결을 재사용하면 윈도우 10053 오류가 난다.
- **동시 실행 방지**: 작업 스케줄러와 GUI가 `data/.lock`을 공유한다. GUI는 CLI를 별도 프로세스로 띄운다.

## 배포 / 버전
- 버전은 `chzzk2yt/__init__.py`의 `__version__`에 적는다. `VERSION.txt`에는 변경 내역을 적는다.
- 업데이트 버튼은 `main` 브랜치의 `chzzk2yt/__init__.py` 버전을 보고 판단한다.
  - **여러 커밋으로 나눠 올릴 때는 `__init__.py` 버전 변경을 반드시 마지막에 올린다.** 중간 상태의 저장소를 받는 사람이 생기지 않게 하기 위해서다.
- 공유용 zip에서 빼는 것: `config.toml`, `data/`, `downloads/`, `vendor/`, `.venv/`, 테스트 파일.
- `사용설명서.html`은 이미지가 base64로 들어 있다(약 600KB). 원본은 이미지 자리표시자가 들어 있는 별도 파일이다.

## 절대 금지
- `data/`(유튜브 토큰, 네이버 쿠키, client_secret.json, 상태 DB)와 `config.toml`을 커밋하거나 공유하지 않는다.
- 사용자 개인 채널 ID, 재생목록 ID, 이메일, OAuth 클라이언트 시크릿을 코드, 예시, 문서에 넣지 않는다.
- 사용자가 채팅에 계정 비밀번호를 붙여 넣어도 사용하거나 저장하지 않는다.

## 테스트
- 윈도우 전용 부분(작업 스케줄러, 토스트, taskkill)은 `IS_WIN`으로 분기한다. 리눅스에서는 로직만 확인한다.
- GUI 렌더링 확인: `QT_QPA_PLATFORM=offscreen python -m chzzk2yt.gui --screenshot out.png`. offscreen 테스트 스크립트를 따로 짤 때는 `app.quit` 대신 `app.exit(0)`으로 끝내야 멈추지 않는다.
- 문법 확인: `python -m py_compile chzzk2yt/*.py`
