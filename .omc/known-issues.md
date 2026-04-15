# Known Issues — Active Monitoring

## Issue 1: Duplicate message delivery (2026-04-11)
- **증상**: 같은 응답이 두 번 Telegram으로 전송됨
- **원인 추정**: `_checkback_deliver`가 이전에 전달된 것과 동일한 내용을 follow-up으로 재전송. `_judge_new_content` (Sonnet)가 "YES" 판정한 것으로 보임
- **수정**: watcher_snapshot 업데이트 로직 추가 완료 — 재시작 후 검증 필요
- **상태**: MONITORING

## Issue 2: Follow-up이 의미없는 반복 (2026-04-11)
- **증상**: "📝 [Follow-up — additional response]" 메시지가 이전 응답과 거의 동일한 내용
- **원인 추정**: `_judge_new_content`의 Sonnet 판별이 raw vs raw 비교에서 false positive
- **수정 방향**: pane watcher 적용 후 _checkback_deliver 자체의 필요성 재평가
- **상태**: MONITORING
