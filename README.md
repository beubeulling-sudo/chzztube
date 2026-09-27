# chzzk2yt — 치지직 다시보기 → 유튜브 자동 업로드

[![보안 검사 (CodeQL)](https://github.com/beubeulling-sudo/chzztube/actions/workflows/codeql.yml/badge.svg)](https://github.com/beubeulling-sudo/chzztube/actions/workflows/codeql.yml)

**처음 쓰는 분은 `사용설명서.html`을 여세요.** 설치부터 구글 API 파일 만들기, 문제 해결까지 순서대로 정리돼 있습니다.

## 빠른 시작
0. 이 페이지 위쪽 초록색 **Code → Download ZIP**으로 받아 압축을 풉니다.
1. 이 폴더를 OneDrive 밖에 둡니다. 예: `C:\chzzk2yt`
2. `menu.bat`을 더블클릭합니다. 처음 실행하면 자동으로 설치하고, 바탕화면에 바로가기 두 개를 만듭니다. 평소에는 **'치지직→유튜브 (프로그램·권장)'**(프로그램 화면)을 쓰고, **'(명령창 메뉴)'**는 고급용입니다.
3. 아이콘으로 프로그램을 열고 **시작 가이드**를 따라 합니다.
   - 채널 추가 → 구글 API 파일 → 유튜브 인증 → (재생목록·공개 범위) → (네이버 로그인) → 드라이런 → 자동 실행 → (과거 방송 가져오기)

## 동작
```
[작업 스케줄러 30분마다] run
  ├ 네이버 쿠키 갱신 (전용 브라우저 프로필)
  ├ 채널 스캔 → 새 다시보기를 목록(data/state.db)에 등록
  ├ 유튜브 동기화 (이미 올라감 / 삭제됨 / 잘림 / 처리 실패 감지)
  └ 방송일 순서대로 한 편씩:
       직접 다운로드 (조각마다 크기·fMP4 구조 검사, 깨진 조각은 재수신 또는 복구)
       → 검증 (스트림 · 치지직 길이 ±2% · 앞/중간/끝 디코딩)
       → 12시간 초과 시 균등 분할
       → 이어 올리기 가능한 업로드 → 재생목록 → 썸네일 → 로컬 삭제
```

## 폴더
| 경로 | 내용 |
|---|---|
| `config.toml` | 설정 (프로그램의 관리 → 설정 / 채널 관리에서 수정) |
| `data/` | **개인 정보** — 유튜브 토큰, 네이버 쿠키, 상태 DB, 로그. 공유 금지 |
| `downloads/` | 받은 영상 (업로드 후 자동 삭제) |
| `vendor/` | 설치 때 받는 upstream 다운로더 ([chzzk-vod-downloader-v2](https://github.com/honey720/chzzk-vod-downloader-v2), 커밋 고정) |

## 업데이트
프로그램의 **업데이트** 버튼(또는 명령창 메뉴 28번)이 이 저장소에서 최신 파일을 받아 교체합니다. `config.toml`과 `data/`(설정·기록·인증)는 그대로 유지됩니다.

v1.1 이하를 쓰던 분은 한 번만 수동으로: Download ZIP → 압축 푼 폴더의 `chzzk2yt` 폴더(코드)를 설치 폴더의 같은 폴더에 덮어쓰기 → 프로그램 재시작. 이후로는 버튼으로 업데이트됩니다.

## 안전성
실행 파일(.exe) 없이 이 저장소의 파이썬·PowerShell 코드가 전부입니다. 누구나 코드를 읽어 볼 수 있고, 코드를 올릴 때마다 GitHub가 자동으로 보안 검사(CodeQL)를 합니다. 결과는 위 배지와 [Actions 탭](https://github.com/beubeulling-sudo/chzztube/actions/workflows/codeql.yml)에서 볼 수 있습니다.

**접속하는 곳** — 아래 말고는 없습니다. 사용 통계나 원격 전송 기능도 없습니다.
| 주소 | 하는 일 |
|---|---|
| `chzzk.naver.com`, `api.chzzk.naver.com`, 치지직 영상 서버 | 다시보기 목록 조회, 영상·썸네일 받기 |
| `nid.naver.com`, `comm-api.game.naver.com` | 네이버 로그인, 로그인 상태 확인 (19+ 영상을 받을 때만) |
| `googleapis.com` (유튜브 API) | 영상 업로드, 재생목록 추가, 썸네일 적용 |
| `github.com`, `api.github.com`, `raw.githubusercontent.com`, `codeload.github.com` | 업데이트 확인·받기, 설치 때 다운로더 받기 |
| `astral.sh` | 설치 때 파이썬 설치 도구(uv)가 없으면 공식 사이트에서 받기 |
| 사용자가 직접 넣은 디스코드 웹훅 | 알림 (설정에서 넣었을 때만. 기본값은 꺼짐) |

**계정 정보는 내 PC에만 저장됩니다**
- 유튜브 인증은 **사용자 본인이 만든 구글 API 파일**(client_secret.json)로 합니다. 로그인 토큰은 개발자를 거치지 않고, 개발자는 여러분의 유튜브 계정에 접근할 수 없습니다.
- 유튜브 토큰, 네이버 로그인 정보, 기록은 모두 설치 폴더의 `data/`에만 있습니다.
- 요청하는 유튜브 권한은 두 가지입니다. 영상 업로드, 그리고 재생목록 추가에 필요한 유튜브 관리 권한입니다.

**받는 프로그램은 버전이 고정돼 있습니다**
- 함께 받는 다운로더는 `setup.ps1`에 적힌 특정 커밋만 받습니다.
- 파이썬 패키지 버전은 `uv.lock`으로 고정돼 있습니다.

**무서워 보일 수 있는 부분**
| 보이는 것 | 이유 |
|---|---|
| `powershell -ExecutionPolicy Bypass` | 윈도우는 기본 설정으로 스크립트(.ps1) 실행을 막아 둡니다. 설치·바로가기 스크립트를 **이번 실행에만** 허용하는 옵션이고, 윈도우 설정을 바꾸지 않습니다. |
| `irm https://astral.sh/uv/install.ps1 \| iex` | 파이썬 도구 uv의 [공식 설치 명령](https://docs.astral.sh/uv/getting-started/installation/)을 그대로 쓴 것입니다. |
| 작업 스케줄러 등록 | '자동 실행'을 켰을 때만, 30분마다 새 다시보기를 확인하려고 등록합니다. 프로그램에서 끌 수 있습니다. |
| 숨겨진 PowerShell 창 | 윈도우 알림(인증 만료 안내)을 띄우거나 자동 실행 상태를 확인할 때 씁니다. |

## 명령어 (고급)
```powershell
.venv\Scripts\python -m chzzk2yt run             # 1회 실행
.venv\Scripts\python -m chzzk2yt dry-run         # 받거나 올리지 않고 점검
.venv\Scripts\python -m chzzk2yt status -a       # 전체 상태
.venv\Scripts\python -m chzzk2yt add <영상URL>    # 특정 영상 추가
.venv\Scripts\python -m chzzk2yt retry all       # 실패 재시도
```
`menu.bat`은 같은 기능을 번호로 고를 수 있는 메뉴입니다.

## 주의
- 권리자(스트리머)의 허락을 받은 채널만 올리세요. Content ID 소유권 주장이나 저작권 신고 대상이 될 수 있습니다.
- 구글 앱은 '테스트 중' 상태로 씁니다. 유튜브 인증이 7일마다 만료되니 일주일에 한 번 '유튜브 인증'을 누르세요. 만료일은 프로그램에 표시되고, 만료 전에 다시 인증하면 그때부터 7일로 연장됩니다.
- 구글 심사를 받지 않은 API 프로젝트로 올린 영상은 유튜브가 비공개로 잠글 수 있습니다.
