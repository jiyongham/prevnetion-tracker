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
    planned_end_date_field: str = "customfield_11360"  # 변경 계획 완료일

    # Teams
    teams_webhook: str = ""

    # 엑셀 경로
    excel_path: str = "data/targets.xlsx"


settings = Settings()
