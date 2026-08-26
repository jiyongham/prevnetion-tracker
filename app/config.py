# app/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # JIRA
    jira_url: str                    # 예: https://jira.회사.com
    jira_pat: str                    # Personal Access Token (Bearer 인증)
    jira_project: str = "IMDC"

    # Confluence (EoS 차주 계획 - 별도 계획서 페이지 파싱용)
    confluence_url: str = ""         # 예: https://confluence.회사.com
    confluence_pat: str = ""         # Personal Access Token (Bearer 인증)
    confluence_eos_parent_page_id: str = "486488355"   # 주간 작업계획 페이지들의 부모('YYYY年') 페이지

    # Polestar (NKIA) - EoS 실제 전환 완료 판정용.
    # 작업이 정상 완료되면 CI명에서 TO-BE의 '_NEW'가 빠지고 AS-IS에 '_OLD'가 붙는데,
    # CMDB(Insight)는 작업자가 늦게 반영하는 경우가 있어 Polestar를 기준으로 삼는다.
    polestar_url: str = ""
    polestar_token: str = ""

    # Polestar (NKIA) - EoS 실제 전환 완료 판정용.
    # 작업이 정상 완료되면 CI명에서 TO-BE의 '_NEW'가 빠지고 AS-IS에 '_OLD'가 붙는데,
    # CMDB(Insight)는 작업자가 늦게 반영하는 경우가 있어 Polestar를 기준으로 삼는다.
    polestar_url: str = ""
    polestar_user: str = ""
    polestar_password: str = ""

    # 완료 판정 필드
    planned_end_date_field: str = "customfield_00003"    # 변경 계획 완료일
    planned_start_date_field: str = "customfield_00004"  # 변경 계획 시작일 (AI 매칭 진단용)

    # 매칭용 추가 필드 (변경작업 대상/완료보고 - 호스트명·IP 포함)
    match_fields: str = "customfield_00005,customfield_00006"

    # JSM요청자 (담당자 불일치 후보 판별용 - '이름(비고) - 팀명' 형식)
    jsm_requester_field: str = "customfield_00001"

    # 작업 구분 (체크박스형 커스텀필드) - DR훈련 완료 판정 보조 기준 ("DR훈련" 값)
    dr_work_type_field: str = "customfield_00002"

    # 작업 완료(CMDB) - EoS 티켓에서 실제로 전환 완료된 CMDB 대상(Insight Key 포함)을 담는 필드.
    # "[시스템명]_OLD (SINCASN-xxxxx)" 형태 - 변경작업내용 텍스트에 호스트명/IP가 없어도 이 필드로 정확히 매칭 가능
    eos_cmdb_done_field: str = "customfield_00007"

    @property
    def match_field_list(self) -> list[str]:
        return [f.strip() for f in self.match_fields.split(",") if f.strip()]

    # Teams
    teams_webhook: str = ""              # 채널 발송(주간 리포트)용 Incoming Webhook
    teams_dm_trigger_webhook: str = ""   # 개인 DM 트리거 전용(비공개) 채널 Incoming Webhook
    dm_marker: str = "##DRDM##"          # DM 트리거 메시지 식별 마커 (Flow 조건에서 사용)

    # 엑셀 경로
    excel_path: str = "data/targets.xlsx"
    capacity_excel_path: str = "data/capacity.xlsx"   # 용량관리(ASM/파일시스템 증설) - DATA/ARCH 시트
    eos_excel_path: str = "data/eos.xlsx"             # EoS(노후 OS/DB 전환) 대상

    db_path: str = "data/tracker.db"

    # app/config.py 에 필드 추가
    # 스케줄러
    scheduler_enabled: bool = True
    report_cron_day: str = "thu"      # 요일 (mon,tue,wed,thu,fri,sat,sun)
    report_cron_hour: int = 9         # 시
    report_cron_minute: int = 0       # 분
    timezone: str = "Asia/Seoul"

    # 미계획 리마인드 발신자 (초안 서명) - 실명/부서명이라 기본값 없이 .env에서만 설정
    sender_team: str
    sender_name: str
    capacity_sender_name: str   # 용량관리 리마인드는 발신자가 다름

    # 대시보드 접속 주소 (리마인드 메시지 하단 안내용, 사내 IP라 .env에서만 설정)
    dashboard_url: str

    # 완료(체크) 처리 가능한 관리자 (입력자명 기준, 쉼표 구분) - 실명이라 기본값 없이 .env에서만 설정
    admin_users: str

    # 용량관리 전용 관리자 (DR훈련과 별도 - 입력자명 기준, 쉼표 구분)
    capacity_admin_users: str

    # EoS 전용 관리자
    eos_admin_users: str

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

    # EoS 차주 계획 챗봇 전용 에이전트 (자유 텍스트에서 언급된 대상 시스템 추출)
    eos_plan_agent_id: str = ""
    eos_plan_agent_code: str = ""

    # 용량관리 챗봇 - 목적이 달라 에이전트를 2개로 분리 (시스템 프롬프트가 서로 배타적)
    capacity_calc_agent_id: str = ""     # 증설 산정 기준/계산식 설명
    capacity_calc_agent_code: str = ""
    capacity_status_agent_id: str = ""   # 대상 진척(완료/일정/JIRA) 조회
    capacity_status_agent_code: str = ""

    @property
    def admin_set(self) -> set[str]:
        return {a.strip() for a in self.admin_users.split(",") if a.strip()}

    @property
    def capacity_admin_set(self) -> set[str]:
        return {a.strip() for a in self.capacity_admin_users.split(",") if a.strip()}

    @property
    def eos_admin_set(self) -> set[str]:
        return {a.strip() for a in self.eos_admin_users.split(",") if a.strip()}


settings = Settings()
