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
    planned_end_date_field: str = "customfield_00003"  # 변경 계획 완료일

    # 매칭용 추가 필드 (변경작업 대상/완료보고 - 호스트명·IP 포함)
    match_fields: str = "customfield_00005,customfield_00006"

    @property
    def match_field_list(self) -> list[str]:
        return [f.strip() for f in self.match_fields.split(",") if f.strip()]

    # Teams
    teams_webhook: str = ""           # 채널 발송(주간 리포트)용 Incoming Webhook
    teams_flow_url: str = ""          # 개인 DM용 Power Automate HTTP 트리거 URL

    # 엑셀 경로
    excel_path: str = "data/targets.xlsx"

    db_path: str = "data/tracker.db"

    # app/config.py 에 필드 추가
    # 스케줄러
    scheduler_enabled: bool = True
    report_cron_day: str = "mon"      # 요일 (mon,tue,wed,thu,fri,sat,sun)
    report_cron_hour: int = 9         # 시
    report_cron_minute: int = 0       # 분
    timezone: str = "Asia/Seoul"

    # 미계획 리마인드 발신자 (초안 서명)
    sender_team: str = "OO팀"
    sender_name: str = "홍길동"

    # 완료(체크) 처리 가능한 관리자 (입력자명 기준, 쉼표 구분)
    admin_users: str = "홍길동"

    @property
    def admin_set(self) -> set[str]:
        return {a.strip() for a in self.admin_users.split(",") if a.strip()}


settings = Settings()
