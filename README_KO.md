# LiteClaw

텔레그램으로 Claude Code CLI를 원격 제어합니다. 추가 API 키 불필요.

[English](README.md)

## LiteClaw이란?

LiteClaw은 텔레그램과 Claude Code CLI를 연결하는 경량 브릿지입니다. 핸드폰에서 Claude Code와 대화하고, AI가 정리한 응답을 받고, 파일을 주고받고, 작업 진행 상황을 모니터링할 수 있습니다.

추가 API 키나 구독이 필요 없습니다. tmux에서 Claude Code가 돌아가고 있으면, LiteClaw이 텔레그램과 연결해줍니다.

## 주요 기능

- **원격 접속** — 텔레그램으로 어디서든 Claude Code 제어
- **AI 요약** — Haiku가 응답을 깔끔하게 정리해서 전달 (토글 가능)
- **작업 감지** — Claude가 작업 중이면 알려주고, 메시지를 큐에 넣음
- **진행 상황** — Claude가 뭘 하고 있는지 실시간 확인
- **파일 전송** — 텔레그램으로 파일 송수신
- **멀티 타겟** — 여러 tmux 세션 간 전환 가능

## 빠른 시작

### 사전 준비

- Python 3.10+
- tmux 3.0+
- Claude Code CLI 설치됨
- 텔레그램 봇 토큰 ([@BotFather](https://t.me/BotFather)에서 발급)
- (선택) 요약용 OpenAI 호환 API 엔드포인트

### 설치

```bash
git clone https://github.com/breaktheready/liteclaw.git
cd liteclaw
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 설정

```bash
cp .env.example .env
```

`.env` 편집:
```
BOT_TOKEN=텔레그램-봇-토큰
CHAT_ID=텔레그램-채팅-ID
TMUX_TARGET=claude:1
```

### 실행

```bash
# 터미널 1: Claude Code 시작
tmux new-session -s claude 'claude --dangerously-skip-permissions'

# 터미널 2: LiteClaw 시작
source .venv/bin/activate
python3 liteclaw.py
```

## 텔레그램 명령어

| 명령어 | 설명 |
|--------|------|
| 일반 텍스트 | Claude Code에 전송 |
| `/status` | Claude 출력 마지막 30줄 표시 |
| `/target SESSION:WIN.PANE` | tmux 타겟 변경 |
| `/cancel` | Claude에 Ctrl+C 전송 |
| `/sessions` | tmux 세션 목록 |
| `/escape` | Escape 키 전송 |
| `/raw` | 원본/요약 모드 전환 |
| `/model MODEL` | 요약 모델 변경 |
| `/get FILEPATH` | 서버에서 파일 다운로드 |
| 파일 전송 | 서버에 업로드 후 Claude에 전달 |
| 사진 전송 | 사진 저장 후 경로를 Claude에 전달 |

## 작동 방식

```
사용자 (텔레그램) → LiteClaw → tmux send-keys → Claude Code CLI
                                                       ↓
사용자 (텔레그램) ← Haiku 요약 ← capture-pane ← 응답
```

1. 텔레그램에서 메시지 전송
2. LiteClaw이 Claude가 idle인지 busy인지 확인
3. `send-keys`로 tmux pane에 메시지 주입
4. 1.5초 간격으로 `capture-pane` 폴링하여 응답 안정화 감지
5. Haiku (또는 OpenAI 호환 API)로 응답 요약 (선택)
6. 깨끗한 응답을 텔레그램으로 전송

## 설정 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `BOT_TOKEN` | (필수) | @BotFather에서 받은 텔레그램 봇 토큰 |
| `CHAT_ID` | (필수) | 본인의 텔레그램 채팅 ID |
| `TMUX_TARGET` | `claude:1` | tmux 타겟 pane |
| `SUMMARIZER_URL` | `http://localhost:8080/v1` | OpenAI 호환 API 엔드포인트 |
| `SUMMARIZER_MODEL` | `claude-haiku-4-5` | 요약에 사용할 모델 |
| `SCROLLBACK_LINES` | `500` | tmux에서 캡처할 줄 수 |
| `INTERMEDIATE_INTERVAL` | `10` | 진행 상황 업데이트 간격 (초) |
| `STAGING_DIR` | `~/liteclaw-files` | 파일 업로드 디렉토리 |
| `EXTRA_PROMPT_PATTERNS` | (비어있음) | 커스텀 프롬프트 감지용 정규식 (쉼표 구분) |

## 요약기 설정

LiteClaw은 요약기 없이도 작동합니다 (`/raw` 모드). AI 응답 정리를 위해 `SUMMARIZER_URL`을 OpenAI 호환 API로 설정:

- [claude-max-api-proxy](https://github.com/1mancrew/claude-max-api-proxy) — Claude Max 구독 활용
- [LiteLLM](https://github.com/BerriAI/litellm) — 다양한 LLM 프로바이더 프록시
- 기타 OpenAI 호환 엔드포인트

## 문제 해결

**"Conflict: terminated by other getUpdates request"**
다른 프로세스가 같은 봇 토큰을 사용 중. 먼저 중지하세요.

**Claude에서 응답이 안 옴**
Claude가 `❯` 프롬프트 상태인지 확인: `/status`

**메시지가 깨져서 옴**
`/raw` 모드가 아닌지 확인. 요약기가 터미널 노이즈를 정리합니다.

**"tmux session not found"**
먼저 tmux에서 Claude Code를 시작하세요: `tmux new-session -s claude`

## 봇 토큰 받기

1. 텔레그램에서 [@BotFather](https://t.me/BotFather)에게 메시지
2. `/newbot` → 안내에 따라 진행
3. 받은 토큰을 `.env`에 입력

## 채팅 ID 확인

1. 텔레그램에서 [@userinfobot](https://t.me/userinfobot)에게 메시지
2. 받은 숫자를 `.env`에 입력

## 라이선스

MIT
