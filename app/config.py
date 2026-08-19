# app/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # JIRA
    jira_url: str                    # 예: https://jira.회사.com
    jira_user: str                   # 로그인 ID
    jira_password: str               # 비밀번호
    jira_project: str = "IMDC"

    # 완료 판정 필드
    planned_end_date_field: str = "customfield_11360"    # 변경 계획 완료일
    planned_start_date_field: str = "customfield_11359"  # 변경 계획 시작일 (AI 매칭 진단용)

    # 매칭용 추가 필드 (변경작업 대상/완료보고 - 호스트명·IP 포함)
    match_fields: str = "customfield_11302,customfield_10848"

    # JSM요청자 (담당자 불일치 후보 판별용 - '이름(비고) - 팀명' 형식)
    jsm_requester_field: str = "customfield_11500"

    @property
    def match_field_list(self) -> list[str]:
        return [f.strip() for f in self.match_fields.split(",") if f.strip()]

    # Teams
    teams_webhook: str = ""              # 채널 발송(주간 리포트)용 Incoming Webhook
    teams_dm_trigger_webhook: str = ""   # 개인 DM 트리거 전용(비공개) 채널 Incoming Webhook
    dm_marker: str = "##DRDM##"          # DM 트리거 메시지 식별 마커 (Flow 조건에서 사용)

    # 엑셀 경로
    excel_path: str = "data/targets.xlsx"

    db_path: str = "data/tracker.db"

    # app/config.py 에 필드 추가
    # 스케줄러
    scheduler_enabled: bool = True
    report_cron_day: str = "thu"      # 요일 (mon,tue,wed,thu,fri,sat,sun)
    report_cron_hour: int = 9         # 시
    report_cron_minute: int = 0       # 분
    timezone: str = "Asia/Seoul"

    # 미계획 리마인드 발신자 (초안 서명)
    sender_team: str = "데이터센터팀"
    sender_name: str = "함지용"

    # 대시보드 접속 주소 (리마인드 메시지 하단 안내용, 동적 IP라 바뀌면 .env만 수정)
    dashboard_url: str = "http://10.100.14.141:8000"

    # 완료(체크) 처리 가능한 관리자 (입력자명 기준, 쉼표 구분)
    admin_users: str = "함지용"

    # 사내 LLM Agent 게이트웨이 (조회 챗봇용)
    agent_token_url: str = ""
    agent_gateway_url: str = ""
    agent_client_id: str = ""
    agent_client_secret: str = ""
    agent_id: str = ""
    agent_code: str = ""

    # 진단 전용 에이전트 (매칭 미확인 사유 / 담당자 불일치 판단) - 조회 챗봇과 시스템 프롬프트가 달라 별도 에이전트 필요
    diagnose_agent_id: str = ""
    diagnose_agent_code: str = ""

    # 주간 리포트 "이번 주 특이사항" 한 줄 요약 전용 에이전트
    summary_agent_id: str = ""
    summary_agent_code: str = ""

    @property
    def admin_set(self) -> set[str]:
        return {a.strip() for a in self.admin_users.split(",") if a.strip()}


settings = Settings()
