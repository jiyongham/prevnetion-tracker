# app/core/scheduler.py
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from pytz import timezone as tz

from app.config import settings
from app.services.capacity_report import send_capacity_report
from app.services.eos_report import send_eos_report
from app.services.report import send_report

logger = logging.getLogger(__name__)

scheduler: BackgroundScheduler | None = None


def job_weekly_report():
    """주간 진척 리포트 (DR훈련)"""
    logger.info("[스케줄] 주간 리포트 실행")
    try:
        warning = send_report()
        if warning:
            logger.warning(f"[스케줄] 주간 리포트 이상 징후\n{warning}")
        logger.info("[스케줄] 주간 리포트 완료")
    except Exception as e:
        logger.exception(f"[스케줄] 주간 리포트 실패: {e}")


def job_weekly_capacity_report():
    """주간 용량관리 현황 리포트"""
    logger.info("[스케줄] 용량관리 리포트 실행")
    try:
        warning = send_capacity_report()
        if warning:
            logger.warning(f"[스케줄] 용량관리 리포트 이상 징후\n{warning}")
        logger.info("[스케줄] 용량관리 리포트 완료")
    except Exception as e:
        logger.exception(f"[스케줄] 용량관리 리포트 실패: {e}")


def job_weekly_eos_report():
    """주간 EoS 현황 리포트"""
    logger.info("[스케줄] EoS 리포트 실행")
    try:
        send_eos_report()
        logger.info("[스케줄] EoS 리포트 완료")
    except Exception as e:
        logger.exception(f"[스케줄] EoS 리포트 실패: {e}")


def start_scheduler():
    """스케줄러 시작"""
    global scheduler

    if not settings.scheduler_enabled:
        logger.info("스케줄러 비활성화 (SCHEDULER_ENABLED=false)")
        return None

    if scheduler and scheduler.running:
        logger.info("스케줄러 이미 실행 중")
        return scheduler

    seoul = tz(settings.timezone)
    scheduler = BackgroundScheduler(timezone=seoul)

    # 주간 리포트
    scheduler.add_job(
        job_weekly_report,
        CronTrigger(
            day_of_week=settings.report_cron_day,
            hour=settings.report_cron_hour,
            minute=settings.report_cron_minute,
            timezone=seoul,
        ),
        id="weekly_report",
        name="주간 진척 리포트",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # 용량관리 주간 리포트 (DR훈련과 같은 요일/시간)
    scheduler.add_job(
        job_weekly_capacity_report,
        CronTrigger(
            day_of_week=settings.report_cron_day,
            hour=settings.report_cron_hour,
            minute=settings.report_cron_minute,
            timezone=seoul,
        ),
        id="weekly_capacity_report",
        name="주간 용량관리 리포트",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # EoS 주간 리포트 (같은 요일이지만 시간은 별도 - 담당자들이 Confluence 주간 작업계획을
    # 목요일 중에 작성하므로, 그게 채워진 뒤인 오후에 발송해야 '금주 실적'이 집계된다)
    scheduler.add_job(
        job_weekly_eos_report,
        CronTrigger(
            day_of_week=settings.report_cron_day,
            hour=settings.eos_report_cron_hour,
            minute=settings.eos_report_cron_minute,
            timezone=seoul,
        ),
        id="weekly_eos_report",
        name="주간 EoS 리포트",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.start()

    for job in scheduler.get_jobs():
        logger.info(f"✅ 등록: {job.name} → 다음 실행 {job.next_run_time}")

    return scheduler


def stop_scheduler():
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("스케줄러 종료")


def get_jobs_info() -> list[dict]:
    """등록된 job 목록 (대시보드 표시용)"""
    if not scheduler or not scheduler.running:
        return []
    return [
        {
            "id": j.id,
            "name": j.name,
            "next_run": j.next_run_time.strftime("%Y-%m-%d %H:%M") if j.next_run_time else "-",
        }
        for j in scheduler.get_jobs()
    ]
