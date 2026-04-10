# LiteClaw

텔레그램으로 Claude Code CLI를 원격 제어. 추가 API 키 불필요.

[English](README.md)

---

## 만든 이유

저는 개발자라기보다는 Claude Code를 매일 쓰는 파워유저입니다. OpenClaw 가 막히면서 핸드폰에서 Claude Code를 에이전틱하게 사용할 수 있는 방법이 필요했습니다.

만든 건 꽤 단순합니다. 텔레그램을 tmux에서 돌아가는 Claude Code 세션에 연결하는 Python 스크립트예요. 터미널에 타이핑하고(`send-keys`), 화면에 나온 걸 읽어옵니다(`capture-pane`). 끝.

API 키는 따로 필요없길 바랐고, 이에 따라 오픈클로와 같이 추가 비용도 없습니다. Claude Code 구독을 이어가고 싶었고, 그게 있으면 그걸로 원격 접속이 되는 겁니다.

필요해서 만들었는데 잘 돌아가길래, 비슷한 상황인 분들한테 도움이 될까 싶어 공유합니다.

## 뭐가 다른가요?

Claude API를 직접 호출하는 도구들과 달리 (= 추가 비용), LiteClaw은 **이미 돌아가고 있는 Claude Code CLI 세션**을 tmux를 통해 조작합니다. Claude Max 구독 중이라면 추가 비용 없이 핸드폰에서 쓸 수 있습니다.

- Python 파일 하나 (~900줄), 프레임워크 아님
- Anthropic API 키 불필요
- Docker나 컨테이너 없음
- 기존 구독으로 전부 커버

## 주요 기능

- **원격 접속** — 텔레그램으로 어디서든 Claude Code 제어
- **AI 요약** — Haiku가 응답을 깔끔하게 정리해서 전달 (토글 가능)
- **작업 감지** — Claude가 작업 중이면 알려주고, 메시지를 큐에 넣음
- **진행 상황** — Claude가 뭘 하고 있는지 실시간 확인
- **파일 전송** — 텔레그램으로 파일 송수신
- **멀티 타겟** — 여러 tmux 세션 간 전환 가능
- **사진 전송** — 비전 작업을 위한 이미지 업로드 지원
- **멀티 에이전트 오케스트레이션** — LiteClaw이 org lead 역할로 독립적인 peer 에이전트들을 별도 tmux 세션에서 관리합니다. 새 명령어: `/agents`, `/agent new|status|remove`, `/assign`. 에이전트 레지스트리는 재시작 후에도 유지됩니다.
- **자동 복구** — API 프록시 다운타임을 자동 감지하고 복구합니다. 401 에러 시 Claude Code 세션을 자동 재인증하고, 복구 완료 시 텔레그램으로 알림을 보냅니다.
- **통합 알림** — 모든 텔레그램 알림이 단일 `notify.py` 모듈을 통해 요약기를 거쳐 전달됩니다. 요약기를 사용할 수 없는 경우 원본 출력으로 자동 전환됩니다.

## 빠른 시작

### 자동 설치 (권장)

```bash
git clone https://github.com/breaktheready/liteclaw.git
cd liteclaw
bash setup.sh
```

setup.sh가 tmux, Python 버전, Claude Code CLI를 확인하고 가상 환경을 구성합니다. `.env`도 자동으로 만들어줍니다.

### 수동 설치

자동 설치 대신 직접 설정하려면:

#### 사전 준비

- Python 3.10+
- tmux 3.0+
- Claude Code CLI 설치됨
- 텔레그램 봇 토큰 ([@BotFather](https://t.me/BotFather)에서 발급)
- (선택) 요약용 OpenAI 호환 API 엔드포인트

#### 설치

```bash
git clone https://github.com/breaktheready/liteclaw.git
cd liteclaw
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

#### 설정

```bash
cp .env.example .env
```

`.env` 편집:

```
BOT_TOKEN=텔레그램-봇-토큰
CHAT_ID=텔레그램-채팅-ID
TMUX_TARGET=claude:1
```

#### 실행

```bash
# 터미널 1: Claude Code 시작
tmux new-session -s claude 'claude --dangerously-skip-permissions'

# 터미널 2: LiteClaw 시작
source .venv/bin/activate
python3 liteclaw.py
```

## 설정

모든 설정은 `.env` 파일에서 환경 변수로 관리합니다.

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `BOT_TOKEN` | (필수) | @BotFather에서 받은 텔레그램 봇 토큰 |
| `CHAT_ID` | (필수) | 본인의 텔레그램 채팅 ID |
| `TMUX_TARGET` | `claude:1` | tmux 타겟 pane (형식: `세션:윈도우.pane`) |
| `SUMMARIZER_URL` | `http://localhost:8080/v1` | OpenAI 호환 API 엔드포인트 |
| `SUMMARIZER_MODEL` | `claude-haiku-4-5` | 요약에 사용할 모델 |
| `SCROLLBACK_LINES` | `500` | tmux에서 캡처할 줄 수 |
| `INTERMEDIATE_INTERVAL` | `10` | 진행 상황 업데이트 간격 (초) |
| `STAGING_DIR` | `~/liteclaw-files` | 파일 업로드 디렉토리 |
| `EXTRA_PROMPT_PATTERNS` | (비어있음) | 커스텀 프롬프트 감지용 정규식 (쉼표 구분) |

## 텔레그램 명령어

| 명령어 | 설명 |
|--------|------|
| 일반 텍스트 | Claude Code에 메시지 전송 |
| `/start` 또는 `/help` | 사용 가능한 명령어와 현재 설정 표시 |
| `/status` | Claude 출력 마지막 30줄 표시 |
| `/target SESSION:WIN.PANE` | tmux 타겟 변경 |
| `/cancel` | Claude에 Ctrl+C 전송 (작업 중단) |
| `/sessions` | 활성 tmux 세션 목록 |
| `/escape` | Escape 키 전송 |
| `/raw` | 원본/요약 모드 전환 |
| `/model MODEL` | 요약 모델 변경 |
| `/get FILEPATH` | 서버에서 파일 다운로드 |
| 파일 전송 | 서버에 업로드 후 Claude에 경로 전달 |
| 사진 전송 | 사진 저장 후 경로를 Claude에 전달 |

### 명령어 상세 설명

**일반 텍스트**

텍스트 메시지를 그대로 Claude Code로 전달합니다. LiteClaw은:
1. Claude가 응답 가능한 상태(idle)인지 확인
2. tmux 세션에 메시지 주입
3. Claude 응답을 폴링
4. 선택적으로 Haiku로 요약
5. 4000자 초과 시 청크로 나눠서 전송

**`/status`**

Claude 현재 상태의 마지막 30줄을 표시합니다. 자리를 비운 동안 무슨 일이 있었는지 확인할 때 유용합니다.

**`/target SESSION:WINDOW.PANE`**

다른 tmux 세션/윈도우로 전환합니다. 예시:

```
/target work:0
/target code:2.1
```

**`/cancel`**

Claude의 현재 작업에 Ctrl+C를 전송하여 중단시킵니다.

**`/escape`**

Escape 키를 전송합니다. Claude의 특정 모드나 다이얼로그를 종료할 때 유용합니다.

**`/raw`**

원본 모드와 요약 모드를 전환합니다. 원본 모드에서는 필터 없이 출력을 그대로 전송합니다. 요약 모드(기본값)에서는 Haiku가 터미널 노이즈를 제거하고 읽기 좋게 정리합니다.

**`/model MODEL_NAME`**

요약에 사용할 모델을 변경합니다. 예시:

```
/model claude-haiku-4-5
/model claude-sonnet-4-6
```

**`/sessions`**

현재 활성화된 모든 tmux 세션 목록을 표시합니다.

**`/get FILEPATH`**

서버에서 파일을 텔레그램으로 다운로드합니다. 상대 경로(tmux pane의 작업 디렉토리 기준) 또는 절대 경로 모두 사용 가능합니다.

```
/get results.txt
/get ~/projects/output.json
```

## 파일 전송

### 업로드 (서버로 보내기)

**문서 전송**

파일을 문서 첨부로 전송합니다 (최대 50 MB). 캡션에 Claude에게 전달할 지시사항을 추가할 수 있습니다.

LiteClaw은:
1. 파일을 `STAGING_DIR`에 저장
2. 파일 경로(소용량 텍스트 파일은 내용 포함)와 캡션을 Claude에 전달
3. Claude의 응답을 텔레그램으로 전송

**사진 전송**

사진을 전송하면 `STAGING_DIR`에 저장하고 경로를 Claude에 전달합니다. 비전 작업이나 이미지 분석에 활용할 수 있습니다.

### 다운로드 (서버에서 받기)

`/get FILEPATH` 명령어로 서버의 파일을 텔레그램으로 다운로드합니다.

## 작동 방식

### 아키텍처

```
사용자 (텔레그램) → LiteClaw → tmux send-keys → Claude Code CLI
                                                        ↓
사용자 (텔레그램) ← Haiku 요약 ← capture-pane ← 응답
```

### 7단계 처리 흐름

1. **메시지 수신** — 텔레그램 메시지가 봇에 도착
2. **상태 확인** — Claude가 프롬프트(idle) 상태인지, 작업 중(busy)인지 판단
3. **메시지 주입** — `tmux send-keys`로 tmux pane에 메시지 전달
4. **폴링** — 1.5초 간격으로 `capture-pane`을 실행하여 응답 감지
5. **안정화 확인** — pane 내용이 3회 연속 동일하고 프롬프트가 나타나면 응답 완료로 판단
6. **요약** (선택) — 로컬 프록시를 통해 Haiku로 응답 정리
7. **전송** — 4000자 단위로 나눠 텔레그램에 전송

### API 키 불필요

LiteClaw은 Claude의 API를 직접 호출하지 않습니다. tmux를 통해 Claude Code를 제어하므로, 기존 Claude Code 구독만으로 충분합니다. 요약 기능은 로컬 프록시를 사용하며 선택 사항입니다 — 프록시가 없으면 원본 출력을 그대로 전송합니다.

## 요약기 설정

LiteClaw은 3단계 요약기를 내장하고 있어 추가 설정 없이 바로 사용 가능합니다.

**Tier 1: API 프록시** (가장 빠름, 2-3초) — OpenAI 호환 API 엔드포인트가 있다면 `SUMMARIZER_URL` 설정:
- [claude-max-api-proxy](https://github.com/1mancrew/claude-max-api-proxy) — Claude Max 구독 활용
- [LiteLLM](https://github.com/BerriAI/litellm) — 다양한 LLM 프로바이더 프록시
- 기타 OpenAI 호환 엔드포인트

**Tier 2: Claude Code 에이전트** (자동 fallback, 10-20초) — API 프록시가 없으면 LiteClaw이 자동으로 숨겨진 Claude Code 세션을 만들어서 응답을 요약합니다. Claude Code가 이미 설치되어 있으므로 추가 설정 불필요. `SUMMARIZER_AGENT_MODEL`로 모델 지정 가능.

**Tier 3: 원본 출력** — 두 단계 모두 실패하면 응답을 그대로 전달합니다. `/raw`로 강제 전환도 가능.

시작 시 LiteClaw이 API 엔드포인트를 자동 확인하고, 연결 불가하면 Tier 2를 미리 준비합니다.

## 봇 토큰 받기

1. 텔레그램에서 [@BotFather](https://t.me/BotFather) 검색 후 대화 시작
2. `/newbot` 입력 후 안내에 따라 진행
3. 봇 이름 설정 (예: "My Claude Bot")
4. 봇 사용자명 설정 (예: `my_claude_bot` — `_bot`으로 끝나야 함)
5. BotFather가 봇 토큰을 발급
6. 토큰을 `.env`의 `BOT_TOKEN`에 입력

## 채팅 ID 확인

1. 텔레그램에서 [@userinfobot](https://t.me/userinfobot) 검색 후 대화 시작
2. 아무 메시지나 전송
3. 응답으로 받은 숫자(User ID)를 `.env`의 `CHAT_ID`에 입력

## 보안

- **봇 토큰**: `.env`에만 저장하세요 (gitignored). 코드에 하드코딩하거나 다른 사람과 공유하지 마세요.
- **인증**: 설정된 `CHAT_ID`의 메시지만 처리합니다. 다른 사용자의 메시지는 무시됩니다.
- **tmux 접근**: LiteClaw은 tmux 세션에 직접 접근합니다. 서버 보안을 적절히 관리하세요.
- **`--dangerously-skip-permissions`**: 이 모드는 Claude의 모든 작업을 자동 승인합니다. 신뢰할 수 있는 환경에서만 사용하세요.
- **네트워크**: 텔레그램 API와 (선택) 로컬 요약기만 연결합니다. 외부 서버로 데이터가 전송되지 않습니다.

## 대시보드

LiteClaw에는 설정 관리를 위한 웹 대시보드가 포함되어 있습니다.

### 접속

LiteClaw 시작 후 브라우저에서: `http://localhost:7777`

### 기능

- **상태**: Claude 작업 중/대기 중, API 프록시 연결 상태
- **모델**: 요약 모델 변경 (Haiku/Sonnet/Opus) 드롭다운으로 선택
- **Raw 모드**: 원클릭 토글
- **타겟**: tmux 타겟 변경 (Telegram 명령 없이)
- **로그**: 최근 활동 확인

### 설정

`.env`에서 포트 지정:

```env
DASHBOARD_PORT=7777
```

`0`으로 설정하면 대시보드가 비활성화됩니다.

---

## 문제 해결

**"Conflict: terminated by other getUpdates request"**

같은 봇 토큰을 사용하는 다른 프로세스가 실행 중입니다. 먼저 해당 프로세스를 중지하세요.

```bash
ps aux | grep liteclaw.py
pkill -f liteclaw.py
```

**Claude에서 응답이 오지 않음**

Claude가 `❯` 프롬프트 상태인지 확인하세요: `/status`

프롬프트가 보이지 않으면 `EXTRA_PROMPT_PATTERNS`에 커스텀 프롬프트 패턴을 추가해 보세요.

**메시지가 깨져서 전달됨**

`/raw` 모드가 아닌 경우, 요약기가 터미널 노이즈를 정리합니다. `/raw`로 전환하면 원본 출력을 확인할 수 있습니다. 문제가 지속되면 `.env`에서 `SCROLLBACK_LINES`를 늘려보세요.

**"tmux session not found"**

먼저 tmux에서 Claude Code를 시작하세요:

```bash
tmux new-session -s claude 'claude --dangerously-skip-permissions'
```

그 다음 `.env`의 `TMUX_TARGET`이 실제 세션 이름과 일치하는지 확인하세요.

**Claude가 바빠서 메시지가 큐에 쌓임**

Claude가 작업 중이면 LiteClaw이 경고를 보내고 메시지를 큐에 넣습니다. `/cancel`로 현재 작업을 중단하거나, 작업이 끝날 때까지 기다리면 됩니다.

**요약기 타임아웃**

Haiku 요약이 너무 오래 걸리는 경우:

1. `/raw`로 전환하여 요약 없이 사용
2. `SUMMARIZER_URL`이 올바르게 설정되어 있는지, 프록시가 실행 중인지 확인
3. 요약기가 없어도 LiteClaw은 계속 작동합니다

## 프로덕션 배포

장기 운영을 위해 LiteClaw 자체를 tmux 세션에서 실행하세요:

```bash
tmux new-session -d -s liteclaw -c /path/to/liteclaw \
  '.venv/bin/python3 liteclaw.py'
```

상태 확인:

```bash
tmux attach -t liteclaw
```

또는 systemd로 자동 재시작을 설정하세요:

```ini
[Unit]
Description=LiteClaw Telegram-Claude Bridge
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/liteclaw
ExecStart=/path/to/liteclaw/.venv/bin/python3 liteclaw.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

설치:

```bash
sudo cp liteclaw.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now liteclaw
```

## 면책 조항

LiteClaw은 개인 프로젝트로, 있는 그대로(as-is) 커뮤니티와 공유합니다.

- **사용에 따른 모든 책임은 사용자 본인에게 있습니다.** 이 소프트웨어 사용으로 인한 손해, 데이터 손실, 보안 문제에 대해 제작자는 책임지지 않습니다.
- 이 도구는 tmux를 통해 Claude Code를 제어합니다. 서버와 tmux 세션의 보안은 사용자가 관리해야 합니다.
- 봇 토큰과 채팅 ID 보안은 사용자 책임입니다. `.env` 파일을 절대 공유하지 마세요.
- 이 프로젝트는 Anthropic과 무관하며, Anthropic의 보증이나 후원을 받지 않습니다.

## 라이선스

MIT
