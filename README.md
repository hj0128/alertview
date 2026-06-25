# 체험단 알림 봇 (Telegram)

여러 체험단 사이트에서 **새 캠페인**이 뜨면, 사용자가 설정한 **키워드·지역**에 맞을 때
**텔레그램으로 즉시** 알려주는 24시간 백엔드 서버입니다.

## 동작 구조

```
스케줄러(5~10분) → 사이트 어댑터로 캠페인 수집 → 신규 감지(이전 본 ID와 비교)
→ 사용자별 키워드/지역 필터 → 텔레그램 봇으로 발송
```

- 사용자는 텔레그램에서 봇을 `/start` 하면 가입되고, 명령어로 키워드·지역을 설정합니다.
- 첫 수집 때는 기존 캠페인을 알림 없이 기록만 합니다(첫 실행 알림 폭탄 방지).

## 빠른 시작

### 1) 텔레그램 봇 토큰 발급
1. 텔레그램에서 **@BotFather** 검색 → `/newbot`
2. 이름·아이디 정하면 **토큰**을 줍니다. 복사해 두세요.

### 2) 설정
```bash
cp .env.example .env
# .env 를 열어 TELEGRAM_BOT_TOKEN 에 토큰을 붙여넣기
```

### 3) 설치 & 실행
```bash
pip install -r requirements.txt
python main.py
```

### 4) 사용
텔레그램에서 내 봇을 찾아 `/start` → 끝. 명령어:

| 명령어 | 설명 |
|---|---|
| `/add 키워드` | 키워드 추가 (예: `/add 오마카세`) |
| `/region 지역` | 지역 추가 (예: `/region 서울`) |
| `/del 값` | 키워드·지역 삭제 |
| `/list` | 내 설정 보기 |
| `/clear` | 필터 전체 삭제 |
| `/sites` | 감시 중인 사이트 |
| `/stop` `/resume` | 알림 끄기/켜기 |

> 필터를 하나도 안 넣으면 **모든 신규 캠페인**을 받습니다. 키워드+지역을 함께 넣으면 둘 다 만족해야 알림이 옵니다.

## 데모 모드로 먼저 확인 (사이트 없이 흐름 검증)
`.env` 에서 `DEMO=1`, `POLL_INTERVAL=30` 으로 두고 실행하면,
30초마다 가짜 캠페인이 "새로 떴다"고 신호 → 텔레그램까지 실제로 오는지 바로 확인할 수 있습니다.
확인 후 `DEMO=0` 으로 되돌리세요.

## 도커로 실행 (PostgreSQL 포함)

DB를 프로젝트 폴더가 아닌 **별도 PostgreSQL 컨테이너**에 두고, 데이터는 도커 named volume(`pgdata`)에 보관합니다. 코드 재배포·컨테이너 삭제와 무관하게 데이터가 유지됩니다.

```bash
cp .env.example .env
# .env 에 TELEGRAM_BOT_TOKEN 등 입력 (POSTGRES_* 는 기본값 그대로 두어도 됨)
docker compose up -d --build
```

- `db`: PostgreSQL 16 (데이터는 `pgdata` 볼륨)
- `app`: 봇 + 웹 UI. compose 가 `DATABASE_URL` 을 자동으로 주입해 `db` 에 연결합니다.
- 웹 UI: `http://localhost:8000` (호스트 포트는 `.env` 의 `HOST_PORT` 로 변경)

자주 쓰는 명령:

```bash
docker compose logs -f app      # 앱 로그
docker compose down             # 중지 (데이터는 보존)
docker compose down -v          # 중지 + 데이터 볼륨까지 삭제(초기화)
docker compose exec db psql -U alertview -d alertview   # DB 접속
```

> **백엔드 자동 선택**: 앱은 `DATABASE_URL` 이 있으면 PostgreSQL, 없으면 SQLite 파일(`DB_PATH`)을 씁니다.
> 그래서 `docker compose` 로는 Postgres, 로컬에서 `python main.py` 나 테스트(`python tests/verify.py`)는 별도 서버 없이 SQLite 로 그대로 돌아갑니다.

## 배포 (24시간 가동)

이 서버는 **항상 켜져 있어야** 알림이 끊기지 않습니다. 가벼운 호스팅 예시:

- **Railway / Render / Fly.io**: 깃 저장소 연결 → 환경변수(`TELEGRAM_BOT_TOKEN` 등) 입력 → 배포.
  - Render는 "Background Worker", Railway는 그대로, Fly는 `fly launch` 후 secrets 설정.
  - `Procfile`(worker)과 `Dockerfile` 모두 포함되어 있어 대부분 자동 인식됩니다.
- **VPS(예: 오라클 무료 티어, 라이트세일)**: `pip install -r requirements.txt` 후
  `nohup python main.py &` 또는 systemd 서비스로 등록.

> 무료~월 몇 천 원 수준에서 충분히 돌릴 수 있습니다.

## 사이트 추가하기

`app/adapters/` 에 어댑터 한 개 = 사이트 한 개.

1. `base.BaseAdapter` 를 상속하고 `key`, `name`, `fetch()` 구현
2. `fetch()` 는 현재 노출 중인 `Campaign` 리스트 반환 (각 캠페인의 `cid` 는 사이트 내 고유 ID)
3. `app/adapters/__init__.py` 의 `ALL_ADAPTERS` 에 등록

현재 **디너의여왕**은 동작하는 실제 어댑터입니다.
레뷰·리뷰노트·리뷰플레이스·미블·놀러와체험단은 자리표시(stub) 상태입니다
— 이들은 JavaScript로 목록을 그리는 사이트라, 내부 JSON API를 찾아 호출하거나
헤드리스 브라우저(Playwright)로 렌더링하는 작업이 추가로 필요합니다.

## 참고 / 주의

- 스크래핑은 각 사이트의 이용약관·robots.txt를 확인하고, 과도한 요청을 피하세요(기본 5분 주기).
- 사이트 HTML 구조가 바뀌면 해당 어댑터를 업데이트해야 합니다.
- 텔레그램으로 검증한 뒤, 수익화 단계에서 카카오 알림톡(사업자 등록 필요)을 별도 채널로 붙일 수 있습니다.

## 파일 구조
```
cheheomdan-alert/
├─ main.py              # 진입점 (봇 + 스케줄러)
├─ app/
│  ├─ config.py         # 환경변수
│  ├─ db.py             # 저장소 (PostgreSQL 또는 SQLite 자동선택)
│  ├─ matcher.py        # 키워드·지역 매칭 규칙
│  ├─ notifier.py       # 텔레그램 메시지 포맷·발송
│  ├─ poller.py         # 수집→신규감지→알림
│  ├─ bot.py            # 텔레그램 명령어 핸들러
│  └─ adapters/
│     ├─ base.py        # 어댑터 인터페이스 + Campaign
│     ├─ dinnerqueen.py # 디너의여왕 (동작)
│     ├─ demo.py        # 데모용 가짜 어댑터
│     └─ stubs.py       # 나머지 사이트 자리표시
├─ requirements.txt
├─ Dockerfile / docker-compose.yml / Procfile
└─ .env.example
```

---

## 웹 UI (텔레그램 로그인 + 필터 설정)

명령어 대신 웹페이지에서 키워드·지역을 클릭으로 관리. 봇과 같은 프로세스·같은 DB라 즉시 반영됩니다.
`python main.py` 실행 시 봇과 웹(기본 포트 8000)이 함께 뜹니다.

### 준비
1. `.env` 에 `TELEGRAM_BOT_USERNAME`(봇 username, @ 제외)와 `WEB_SECRET`(임의 긴 문자열) 설정
2. 텔레그램 로그인 버튼은 **HTTPS 도메인**에서만 작동 → @BotFather `/mybots` → 봇 → Bot Settings → Domain 에 도메인 등록

### 로컬에서 UI 테스트 (텔레그램 로그인 없이)
localhost 에서는 텔레그램 로그인이 막혀 "Bot domain invalid" 가 정상입니다.
로컬에서 UI만 보려면 `.env` 에:
```
WEB_DEV_LOGIN=1
WEB_DEV_CHATID=내_텔레그램_숫자ID   # @userinfobot 으로 확인
```
그 후 `python main.py` → `http://localhost:8000` → "🔧 개발용 로그인" 클릭 → 설정 화면.
설정한 필터는 `WEB_DEV_CHATID` 계정으로 실제 알림이 갑니다.
**운영 배포 시 반드시 `WEB_DEV_LOGIN=0`** 으로 끄세요(로그인 우회 방지).

## 배포 (도메인 + HTTPS)

텔레그램 로그인은 HTTPS 필수. 집 PC를 서버로 쓸 경우 **Cloudflare Tunnel** 이 가장 쉽습니다(포트포워딩·인증서 불필요).
- Cloudflare Zero Trust 대시보드 → Networks → Tunnels → Create → Cloudflared
- 표시되는 설치 명령을 PC에서 1회 실행(서비스로 상시 연결)
- Public Hostname: 도메인 선택 + Service `HTTP` → `localhost:8000`
- @BotFather Domain 에 같은 도메인 등록
- 앱(`python main.py`)이 켜져 있으면 `https://내도메인.com` 으로 접속·로그인 가능

## 필터 종류 (웹 UI)
키워드 / 카테고리(맛집·카페 등) / 채널(블로그·릴스 등) / 지역 / 경쟁률 이하 / 마감일 이하.
"당첨확률 UP(경쟁률≤1)", "마감임박(D-3)" 빠른 설정 버튼 제공.
경쟁률·마감일은 디너의여왕 캠페인 카드에서 자동 추출되며, 값이 없는 캠페인은 해당 숫자필터로 제외하지 않습니다(알림 누락 방지).

## 캠페인 피드 + NEW
웹에서 필터를 고르면 아래에 **조건에 맞는 캠페인 전체(최신순)**가 표시되고, 항목을 누르면 새 탭으로 신청 페이지로 이동합니다.
아직 확인하지 않은 캠페인에는 **NEW** 배지가 붙습니다.
- 개별 클릭 → 그 항목만 NEW 해제
- "모두 확인" 버튼 → 전체 NEW 해제
(NEW = 마지막 '모두 확인' 이후 수집됨 + 아직 클릭하지 않음)


## 수집량(페이징)
디너의여왕은 `?page=N` 페이징을 돌며 여러 페이지를 수집합니다. `.env` 의 `DQ_MAX_PAGES`(기본 10, 1페이지≈30건)로 조절하세요. 늘리면 더 많이 모이지만 수집 시간이 늘어납니다.
