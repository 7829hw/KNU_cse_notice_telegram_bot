# KNU CSE Notice Telegram Bot

경북대학교 컴퓨터학부의 공지 게시판을 주기적으로 확인해, 구독한 사용자에게
텔레그램 DM으로 알려 주는 봇입니다. 로컬 서버에서 Docker Compose로 상시
실행합니다.

```text
Docker Compose
    │
    ▼
Telegram Bot Container
    ├─ Telegram Long Polling  (24시간)
    ├─ 사용자 구독 설정 처리
    ├─ 주기적 KNU 공지 크롤링 (평일 업무시간)
    ├─ 사용자별 맞춤 DM 발송
    └─ SQLite 상태 저장 (./data/bot.db)
```

## 대상 게시판

| board_key                   | 게시판       |
| --------------------------- | ------------ |
| `undergraduate`             | 학부 공지     |
| `graduate`                  | 대학원 공지   |
| `undergraduate_recruitment` | 학부인재모집  |

게시판별로 마지막 글 번호를 따로 관리하므로, 한 게시판의 글 번호가 더 크더라도
다른 게시판의 새 글을 빠뜨리지 않습니다.

## 실행 방법

```bash
cp .env.example .env      # TELEGRAM_TOKEN 을 채워 넣습니다
docker compose up -d --build
```

로그 확인과 종료는 다음과 같습니다.

```bash
docker compose logs -f
docker compose down
```

`restart: unless-stopped` 이므로 서버나 도커 데몬을 다시 시작해도 봇이 자동으로
살아납니다. `docker compose stop/down` 시에는 SIGTERM을 받아 Long Polling과
스케줄러가 정상 종료되고, SQLite 연결도 닫힙니다.

### 환경변수

`.env` 파일로 전달하며, Git에는 커밋하지 않습니다 (`.env.example`만 제공).

| 변수                     | 기본값             | 설명                        |
| ------------------------ | ------------------ | --------------------------- |
| `TELEGRAM_TOKEN`         | (필수)             | @BotFather 발급 토큰        |
| `DB_PATH`                | `/app/data/bot.db` | SQLite 파일 위치            |
| `CHECK_INTERVAL_SECONDS` | `600`              | 공지 확인 주기(초)          |
| `TIMEZONE`               | `Asia/Seoul`       | 크롤링 시간대 판정 기준     |
| `CRAWL_START_HOUR`       | `8`                | 크롤링 시작 시각            |
| `CRAWL_END_HOUR`         | `20`               | 크롤링 종료 시각            |

## 사용자 명령

| 명령        | 동작                                                             |
| ----------- | ---------------------------------------------------------------- |
| `/start`    | 알림 시작. 신규 사용자는 모든 항목 ON, 기존 사용자는 설정 유지    |
| `/settings` | 인라인 키보드로 게시판·본문·첨부 설정 토글                        |
| `/stop`     | `active = false` 로만 변경. 설정과 사용자 데이터는 그대로 보관    |
| `/help`     | 명령 안내                                                        |

`/settings` 에서 켜고 끌 수 있는 항목입니다.

```text
☑ 학부 공지          ☑ 본문 포함
☑ 대학원 공지        ☑ 첨부파일·본문 이미지 포함
☑ 학부인재모집
```

`본문 포함`과 `첨부파일·이미지 포함`은 서로 독립적입니다.

- 본문 ON  + 첨부 OFF → 제목 / 본문 / 원문 링크
- 본문 OFF + 첨부 ON  → 게시판·제목 / 원문 링크 + 이미지·첨부파일
- 본문 OFF + 첨부 OFF → 게시판·제목 / 원문 링크

본문을 받는 사용자에게는 기존과 동일하게 원문의 굵게·기울임·밑줄·취소선·링크·목록
서식이 유지되고, 표는 한글 폭과 병합 셀을 반영한 고정폭 코드 블록으로, 긴 글은
서식이 깨지지 않도록 여러 메시지로 나뉘어 전송됩니다.

## 크롤링 시간

Telegram 명령은 24시간 즉시 처리하지만, 공지 크롤링은 공지가 실제로 올라오는
시간대에만 수행합니다.

```text
Asia/Seoul · 평일(월~금) 08:00 ~ 20:00 · 10분 간격
```

크롤링은 동기 `requests` 코드를 `asyncio.to_thread` 로 돌려 Telegram 이벤트
루프를 막지 않으며, 앞선 크롤링이 끝나지 않았으면 다음 주기를 건너뜁니다.

## 저장 구조

상태는 모두 `./data/bot.db` (SQLite)에 있고, `./data` 는 컨테이너에 바인드
마운트되어 컨테이너를 다시 만들어도 유지됩니다.

| 테이블          | 내용                                                       |
| --------------- | ---------------------------------------------------------- |
| `users`         | `chat_id`, `active`, `created_at`                          |
| `subscriptions` | 게시판 3종 + `include_content`, `include_attachments`      |
| `board_state`   | 게시판별 마지막 처리 공지 ID                                |
| `pending_media` | 전송 실패 후 재시도가 필요한 이미지·첨부파일 (사용자별)     |

공지를 한 건 보낼 때마다 커서를 저장하므로, 재시작해도 이미 보낸 공지를 다시
보내지 않습니다. DB가 비어 있는 첫 실행에서는 과거 공지를 쏟아내지 않고 현재
최신 글 번호부터 시작합니다.

전송 실패는 원인을 구분합니다.

- 차단·탈퇴 등 영구 오류 → 해당 사용자만 `active = false` (데이터는 유지)
- 네트워크 오류·타임아웃 등 일시 오류 → 구독 상태를 유지하고 다음 주기에 재시도

### 기존 상태 이어받기

GitHub Actions 시절의 `last_num.txt` 를 `data/last_num.txt` 에 두면, DB가 비어
있는 최초 1회에 한해 게시판별 마지막 글 번호를 가져옵니다.

## 개발

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover
```

| 파일          | 역할                                       |
| ------------- | ------------------------------------------ |
| `bot.py`      | Telegram 명령/콜백 처리, 주기 작업, 실행    |
| `crawler.py`  | KNU 홈페이지 크롤링·파싱·Markdown 변환      |
| `notifier.py` | 사용자 설정에 따른 Telegram 전송            |
| `database.py` | SQLite 초기화, 사용자 설정, 공지 상태 관리  |
| `config.py`   | 환경변수 및 공통 설정                       |
