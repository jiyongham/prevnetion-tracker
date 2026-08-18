# prevnetion-tracker
## 프로젝트 배경
인프라팀 장애예방 활동 완료율 자동 집계 및 Teams 리포트 웹서비스 개발

## 기술 스택
- Backend: Python + FastAPI
- DB: SQLite (로컬 개발)
- Scheduler: APScheduler
- 알림: Teams Incoming Webhook
- 개발환경: VS Code + MCP(사내 JIRA 연동)

## 데이터 흐름
엑셀(계획 대상 목록) → 웹서비스(일정 입력/확인) → JSM 티켓 연동 → 완료율 집계 → Teams 리포트

## JIRA 정보
- 프로젝트: IMDC (데이터센터팀 업무 관리)
- 대상 티켓 판별: summary에 [예방1]/[예방2]/[예방3] 포함 여부
  - [예방1]: EoS 대응 (서버 IP 전환/폐기)
  - [예방3]: DR 모의훈련 (실전환) ← 요청 없이 인프라팀이 자체 발행하는 경우 있음
  - [예방4]: 용량 관리 (디스크 증설)
- 완료 판정: '변경 계획 완료일' 필드 기준 (날짜가 기준일 이하면 완료)
- 인증: 추후 결정 (.env로 관리 예정)

## 완료율 계산
- 분모: 엑셀에 미리 정의된 계획 건수
- 분자: 변경 계획 완료일 ≤ 기준일 인 티켓 수
- 기준일: 오늘 or 다음주 (미리 준비용) 파라미터로 변경 가능

## 엑셀 대상 목록
- 인프라팀이 미리 정의한 점검 대상 목록
- jira_ticket_key 컬럼으로 JIRA 티켓과 1:1 매핑 예정

## 미결정 사항
- '변경 계획 완료일' field id (MCP로 확인 예정)
- JIRA 인증 방식 (Cloud API Token or DC PAT)