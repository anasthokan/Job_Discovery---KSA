import html
import json
import logging
import os
import re
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from jobscraper.ats_score import AtsScoreError, score_resume
from jobscraper.career_portal import lookup_company_jobs
from jobscraper.candidates import (
    CandidateError,
    clean_candidate_id,
    filter_jobs_for_candidate,
    load_candidate_file,
    normalize_profile,
    save_candidate_file,
    career_years,
    certifications_from_profile,
    experience_titles_from_profile,
    skills_from_profile,
    applied_filters,
)
from jobscraper.document_convert import (
    ConvertError,
    DOCX_TYPE,
    PDF_TYPE,
    cv_text_to_docx,
    cv_text_to_pdf,
    docx_to_pdf,
    pdf_to_docx,
    preview_text_from_docx,
)
from jobscraper.jd_to_cv import JobToCvError, build_cv_from_job
from jobscraper.ats_boards import ats_boards_status, interval_minutes as ats_boards_interval, sync_ats_boards
from jobscraper.globaljobs import globaljobs_status, interval_minutes as globaljobs_interval, sync_global_jobs
from jobscraper.gulfdevjobs import gulfdevjobs_status, sync_gulf_jobs
from jobscraper.headcount import headcount_status, interval_minutes as headcount_interval, scheduled as headcount_scheduled, sync_headcount
from jobscraper.jobspy_source import board_scrape_block
from jobscraper.sources import (
    envelope as source_envelope,
    get_source,
    inactive_source_ids,
    list_sources,
    set_source_status,
)
from jobscraper.job_fields import (
    EXPERIENCE_LEVELS,
    WORK_MODELS,
    apply_listing_filters,
    compensation_text,
    infer_listing_fields,
)
from jobscraper.locations import (
    DROPDOWN_STATES,
    KSA_SCOPED_PLATFORMS,
    is_ksa_job,
    matches_city_filter,
    matches_state_filter,
)
from jobscraper.recommend import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    _posted_age_seconds,
    normalize_skills,
    parse_skill_query,
    recommend_jobs,
    search_phrase,
)
from jobscraper.score_config import score_config
from jobscraper.linkedin_profile import LinkedInProfileError, autofill_from_linkedin
from jobscraper.profile_autofill import autofill_from_parsed
from jobscraper.resume_enhance import enhance_profile_for_job
from jobscraper.resume_parser import ResumeParseError, parse_resume_bytes, parse_resume_text
from jobscraper.db import (
    close_abandoned_scrape_runs,
    close_conn as close_mysql,
    finish_scrape_run,
    health as mysql_health,
    import_json_if_empty,
    init_db as init_mysql,
    job_count as mysql_job_count,
    last_scrape_meta,
    latest_finished_scrape,
    catalog_search_terms,
    load_app_user,
    load_candidate as load_candidate_mysql,
    attach_descriptions,
    attach_missing_career_urls,
    attach_raw_descriptions,
    load_job as load_job_mysql,
    load_resume_file,
    load_resume_file_text,
    load_jobs as load_jobs_mysql,
    mysql_configured,
    save_candidate as save_candidate_mysql,
    note_scrape_progress,
    read_scrape_progress,
    read_scrape_progress_stats,
    reset_scrape_progress,
    scrape_run_save_stats,
    start_scrape_run,
    start_dice_description_backfill,
    start_startup_maintenance,
    upsert_jobs,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DATA_FILE = ROOT / "data" / "jobs.json"
INDEX_FILE = ROOT / "index.html"
DASHBOARD_FILE = ROOT / "dashboard.html"
SCRAPE_LOCK = threading.Lock()
# Set only after this process has zeroed data/scrape_progress.json for the
# scrape that currently holds SCRAPE_LOCK. Until then the file still has the
# previous run, and status must not report those counts.
_SCRAPE_COUNTS_READY = threading.Event()
SCRAPE_TIMEOUT = 720
_LAST_SCRAPE: dict = {"ok": None, "error": None, "count": None, "db_total": None}
log = logging.getLogger("job-discovery")
_SCHEDULER: BackgroundScheduler | None = None
_BOOT_STOP = threading.Event()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _truthy(name: str, default: str = "true") -> bool:
    return _env(name, default).lower() not in {"0", "false", "no", "off"}


def _schedule_hours() -> list[int]:
    hours = []
    for part in (_env("SCRAPE_HOURS", "8,20") or "8,20").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hour = int(part)
        except ValueError:
            continue
        if 0 <= hour <= 23:
            hours.append(hour)
    return hours or [8, 20]


def _scrape_target() -> int:
    """Jobs the twice-daily run aims for. 1000+ searches every field, up to 100 listings each."""
    raw = _env("SCRAPE_TARGET", "5000") or "5000"
    try:
        target = int(raw)
    except ValueError:
        target = 5000
    return max(0, min(target, 20000))


def _kept_scrape_count() -> int:
    return sum(
        1
        for job in load_jobs_json()
        if is_ksa_job(
            job.get("state") or "",
            job.get("location") or "",
            allow_bare_remote=(job.get("platform") or "") in KSA_SCOPED_PLATFORMS,
        )
    )


def _store_last_scrape(*, ok: bool, error: str | None, count: int, db_total: int | None) -> None:
    global _LAST_SCRAPE
    _LAST_SCRAPE = {"ok": ok, "error": error, "count": count, "db_total": db_total}


def _claim_scrape() -> bool:
    """Take the scrape lock and hide the previous run's progress counts."""
    if not SCRAPE_LOCK.acquire(blocking=False):
        return False
    _SCRAPE_COUNTS_READY.clear()
    return True


def _arm_scrape_counts() -> None:
    """Keep a killed run's totals, then zero the progress file for this run.

    The lock is acquired before keyword lookup, which can take a while.
    Status reads the progress file while the lock is held. Clearing that
    file here stops the previous run's saved/new/duplicate totals from
    showing up as this run.
    """
    if _SCRAPE_COUNTS_READY.is_set():
        return
    partial = read_scrape_progress_stats()
    close_abandoned_scrape_runs(
        job_count_value=partial["saved"],
        new_count=partial["new"],
        duplicate_count=partial["duplicates"],
    )
    reset_scrape_progress()
    _SCRAPE_COUNTS_READY.set()


def _keywords_for_scrape(keywords: str) -> str:
    """Ignore an empty SCRAPE_KEYWORDS value and search the database instead.

    Skills, certifications, and past job titles already stored in MySQL are used.
    The old fixed role list is not a fallback.
    """
    text = (keywords or "").strip()
    if text:
        return text
    terms = catalog_search_terms(limit=120)
    if terms:
        log.info("SCRAPE_KEYWORDS is empty. Searching %s skills, certifications, and titles from the database", len(terms))
        return ",".join(terms)
    log.warning("SCRAPE_KEYWORDS is empty and the database has no skills, certifications, or job titles. Scrape skipped.")
    return ""


def scheduled_scrape() -> None:
    if not _claim_scrape():
        log.warning("Scheduled scrape skipped: another scrape is running")
        return
    try:
        _arm_scrape_counts()
        keywords = _keywords_for_scrape(_env("SCRAPE_KEYWORDS", ""))
        state = _env("SCRAPE_STATE", "All") or "All"
        platform = _env("SCRAPE_PLATFORM", "All") or "All"
        city = _env("SCRAPE_CITY", "")
        target = _scrape_target()
        if not keywords:
            _store_last_scrape(
                ok=False,
                error="SCRAPE_KEYWORDS is empty and the database has no skills, certifications, or job titles.",
                count=0,
                db_total=mysql_job_count() if mysql_configured() else 0,
            )
            return
        blocked = board_scrape_block(platform)
        if blocked:
            log.info("Scheduled board scrape skipped: %s", blocked)
            _store_last_scrape(
                ok=True,
                error=None,
                count=0,
                db_total=mysql_job_count() if mysql_configured() else 0,
            )
            return
        log.info("Scheduled scrape starting keywords=%s target=%s", keywords, target)
        count = run_spider(keywords, state, platform, city, target) or _kept_scrape_count()
        db_total = mysql_job_count() if mysql_configured() else count
        _store_last_scrape(ok=True, error=None, count=count, db_total=db_total)
        log.info("Scheduled scrape finished count=%s db_total=%s", count, db_total)
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        saved = read_scrape_progress()
        db_total = mysql_job_count() if mysql_configured() else None
        _store_last_scrape(ok=saved > 0, error=str(detail)[:500], count=saved, db_total=db_total)
        log.exception("Scheduled scrape failed")
    finally:
        SCRAPE_LOCK.release()


def _start_scheduler() -> BackgroundScheduler | None:
    if not _truthy("SCRAPE_SCHEDULE_ENABLED", "true"):
        log.info("Scrape schedule disabled")
        return None
    tz_name = _env("SCRAPE_TZ", "Asia/Riyadh") or "Asia/Riyadh"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        log.warning("Invalid SCRAPE_TZ=%s, using UTC", tz_name)
        tz_name = "UTC"
        tz = ZoneInfo("UTC")
    hours = _schedule_hours()
    scheduler = BackgroundScheduler(timezone=tz)
    for hour in hours:
        scheduler.add_job(
            scheduled_scrape,
            "cron",
            hour=hour,
            minute=0,
            id=f"scrape-{hour}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
    scheduler.add_job(
        start_dice_description_backfill,
        "interval",
        minutes=10,
        id="posting-descriptions",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    if _truthy("GLOBALJOBS_ENABLED", "true"):
        scheduler.add_job(
            sync_global_jobs,
            "interval",
            minutes=globaljobs_interval(),
            id="dev-global-jobs",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
            next_run_time=datetime.now(tz),
        )
    if headcount_scheduled():
        scheduler.add_job(
            sync_headcount,
            "interval",
            minutes=headcount_interval(),
            id="headcount-labs",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            next_run_time=datetime.now(tz),
        )
    if _truthy("ATS_BOARDS_ENABLED", "true"):
        scheduler.add_job(
            sync_ats_boards,
            "interval",
            minutes=ats_boards_interval(),
            id="ats-boards",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
            next_run_time=datetime.now(tz),
        )
    scheduler.start()
    log.info("Scrape schedule: %s:00 %s", hours, tz_name)
    return scheduler


def _boot() -> None:
    """MySQL setup stays off the accept path. IIS returns 502 until this port is open."""
    global _SCHEDULER
    try:
        init_mysql()
        partial = read_scrape_progress_stats()
        closed = close_abandoned_scrape_runs(
            job_count_value=partial["saved"],
            new_count=partial["new"],
            duplicate_count=partial["duplicates"],
        )
        if closed:
            log.info("Closed %s scrape runs left on running", closed)
        import_json_if_empty(DATA_FILE)
        start_startup_maintenance()
    except Exception:
        log.exception("API boot failed")
    if _BOOT_STOP.is_set():
        return
    try:
        scheduler = _start_scheduler()
    except Exception:
        log.exception("Scrape scheduler failed")
        return
    if _BOOT_STOP.is_set():
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        return
    _SCHEDULER = scheduler


@asynccontextmanager
async def lifespan(_app):
    global _SCHEDULER
    _BOOT_STOP.clear()
    threading.Thread(target=_boot, name="api-boot", daemon=True).start()
    yield
    _BOOT_STOP.set()
    if _SCHEDULER is not None:
        _SCHEDULER.shutdown(wait=False)
        _SCHEDULER = None
    close_mysql()


app = FastAPI(
    title="Job Discovery",
    description="Job listings API, skill-based job recommendations, resume/CV parse, profile autofill from a resume, ATS resume score, job-description-to-CV, resume enhance download, PDF/DOCX conversion, and MySQL job storage.",
    version="1.7.0",
    lifespan=lifespan,
    redirect_slashes=False,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*", "null"],
    allow_origin_regex=".*",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def strip_trailing_slash(request: Request, call_next):
    """IIS follows a slash redirect to 127.0.0.1 and the browser then times out."""
    path = request.scope.get("path") or ""
    if len(path) > 1 and path.endswith("/"):
        trimmed = path.rstrip("/") or "/"
        request.scope["path"] = trimmed
        raw = request.scope.get("raw_path")
        if isinstance(raw, (bytes, bytearray)):
            request.scope["raw_path"] = trimmed.encode("utf-8")
    return await call_next(request)


class ResumeTextRequest(BaseModel):
    text: str = Field(..., min_length=40, description="Raw resume / CV text")


class ResumeSource(BaseModel):
    filename: str | None = None
    content_type: str | None = None
    pages: int | None = None
    char_count: int


class ResumeProfile(BaseModel):
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    phones: list[str] = []
    location: str | None = None
    linkedin: str | None = None
    github: str | None = None
    website: str | None = None
    summary: str | None = None


class ResumeExperience(BaseModel):
    title: str | None = None
    company: str | None = None
    location: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False
    bullets: list[str] = []


class ResumeEducation(BaseModel):
    degree: str | None = None
    field: str | None = None
    institution: str | None = None
    start_year: str | None = None
    end_year: str | None = None
    gpa: str | None = None


class ResumeProject(BaseModel):
    name: str
    description: str | None = None


class ResumeParseResponse(BaseModel):
    ok: bool = True
    source: ResumeSource
    profile: ResumeProfile
    skills: list[str]
    experience: list[ResumeExperience]
    education: list[ResumeEducation]
    projects: list[ResumeProject]
    certifications: list[str]
    languages: list[str]
    raw_text: str | None = None


class ResumeInput(BaseModel):
    profile: ResumeProfile | None = None
    skills: list[str] = []
    experience: list[ResumeExperience] = []
    education: list[ResumeEducation] = []
    projects: list[ResumeProject] = []
    certifications: list[str] = []
    languages: list[str] = []


class ResumeEnhanceRequest(BaseModel):
    user_id: str = Field(..., min_length=1, description="App user id: numeric users.id or public_id")
    job_id: str = Field(..., min_length=1, description="Job id, public_id, or source id from /api/jobs")


class CvFromJobRequest(BaseModel):
    job_description: str = Field("", description="Full job posting text")
    job_title: str | None = Field(None, description="Optional override if the posting title is known")
    company: str | None = None
    location: str | None = None
    job_id: str | None = Field(None, description="Optional scraped job id from /api/jobs")
    resume_text: str | None = Field(None, description="Optional raw resume text to tailor")
    resume: ResumeInput | None = Field(None, description="Optional parsed resume JSON from /api/resume/parse")


class JobParse(BaseModel):
    title: str | None = None
    company: str | None = None
    location: str | None = None
    employment_type: str | None = None
    workplace_type: str | None = None
    required_skills: list[str] = []
    preferred_skills: list[str] = []
    responsibilities: list[str] = []
    qualifications: list[str] = []
    keywords: list[str] = []


class MatchReport(BaseModel):
    score: int | None = None
    matched_skills: list[str] = []
    missing_skills: list[str] = []
    extra_skills: list[str] = []


class TailoredCv(BaseModel):
    profile: ResumeProfile
    skills: list[str]
    experience: list[ResumeExperience]
    education: list[ResumeEducation]
    projects: list[ResumeProject]
    certifications: list[str]
    languages: list[str]


class CvFromJobResponse(BaseModel):
    ok: bool = True
    mode: str
    job: JobParse
    match: MatchReport
    cv: TailoredCv
    cv_text: str | None = None


class CandidateJob(BaseModel):
    company: str = ""
    job_title: str = ""
    start_date: str = ""
    end_date: str = ""
    description: str = ""


class CandidateSchool(BaseModel):
    school: str = ""
    degree: str = ""
    field_of_study: str = ""
    graduation_year: str = ""
    gpa: str | None = None


class CandidateAnswer(BaseModel):
    question: str = ""
    answer: str = ""


class CandidateProfile(BaseModel):
    """Fields collected on the registration screens. Password and EEO answers are ignored."""

    first_name: str = ""
    last_name: str = ""
    preferred_name: str = ""
    pronouns: str = ""
    email: str = ""
    phone_code: str = ""
    phone: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    country: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    personal_website: str = ""
    work_history: list[CandidateJob] = Field(default_factory=list)
    education: list[CandidateSchool] = Field(default_factory=list)
    custom_answers: list[CandidateAnswer] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    resume_text: str = ""
    cover_letter_text: str = ""
    authorized_to_work: str = ""
    need_visa_sponsorship: str = ""
    willing_to_relocate: str = ""
    open_to_remote: str = ""
    is_18_or_older: str = ""
    desired_salary: str = ""
    notice_period: str = ""
    how_heard: str = ""


class SourceStatusBody(BaseModel):
    status: str = Field(..., description="active keeps scraping this source. inactive stops it.")


class RecommendRequest(BaseModel):
    candidate_id: str | None = Field(
        None,
        description="Candidate API id from registration. Loads the saved profile.",
    )
    skills: list[str] = Field(default_factory=list, description="Candidate skills, e.g. Python, FastAPI, AWS")
    certifications: list[str] = Field(default_factory=list, description="Candidate certifications, e.g. PMP, AWS Certified")
    resume_text: str | None = Field(None, description="Optional raw resume text; skills are extracted from it")
    resume: ResumeInput | None = Field(None, description="Optional parsed resume JSON from /api/resume/parse")
    state: str = Field("All", description="US state filter, Remote, or All")
    platform: str = Field("All", description="Job board name, or All")
    days: int = Field(
        0,
        description="0 returns every job in the database. 1, 3, or 7 keep a shorter window.",
    )
    city: str = ""
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    refresh: bool = Field(False, description="If true, scrape boards with these skills before ranking")


class AtsScoreRequest(BaseModel):
    resume_text: str | None = Field(None, description="Raw resume / CV text")
    resume: ResumeInput | None = Field(None, description="Optional parsed resume JSON from /api/resume/parse")
    job_description: str = Field("", description="Optional job posting text to score keyword match")
    job_title: str | None = None
    company: str | None = None
    location: str | None = None
    job_id: str | None = Field(None, description="Optional scraped job id from /api/jobs")


class AtsCheck(BaseModel):
    id: str
    ok: bool
    message: str


class AtsBreakdown(BaseModel):
    parseability: int
    contact: int
    structure: int
    content: int
    job_match: int | None = None


class AtsMatch(BaseModel):
    score: int | None = None
    matched_skills: list[str] = []
    missing_skills: list[str] = []
    extra_skills: list[str] = []
    job_title: str | None = None
    company: str | None = None


class AtsProfile(BaseModel):
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin: str | None = None
    github: str | None = None


class AtsScoreResponse(BaseModel):
    ok: bool = True
    score: int
    grade: str
    label: str
    job_title: str | None = None
    matched_keywords: list[str] = []
    suggested_additions: list[str] = []
    breakdown: AtsBreakdown
    checks: list[AtsCheck]
    suggestions: list[str]
    match: AtsMatch | None = None
    job: JobParse | None = None
    skills: list[str] = []
    profile: AtsProfile
    source: ResumeSource


def load_jobs_json() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    try:
        text = DATA_FILE.read_text(encoding="utf-8").replace("\x00", "").strip()
        if not text:
            return []
        payload, _ = json.JSONDecoder().raw_decode(text)
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def load_jobs(description_chars: int = 800) -> list[dict]:
    if mysql_configured():
        rows = load_jobs_mysql(description_chars=description_chars)
        if rows:
            return rows
    annotated = []
    for job in load_jobs_json():
        fields = infer_listing_fields(
            title=job.get("title") or "",
            location=job.get("location") or "",
            state=job.get("state") or "",
            description=job.get("description") or job.get("search_text") or "",
            job_type=job.get("job_type") or "",
        )
        row = dict(job)
        row.update(fields)
        row["compensation"] = compensation_text(
            row,
            row.get("description") or row.get("search_text") or "",
        )
        annotated.append(row)
    return annotated


def cache_meta() -> dict:
    if mysql_configured():
        meta = last_scrape_meta()
        if meta.get("scraped_at"):
            return {"scraped_at": meta["scraped_at"], "age_seconds": meta["age_seconds"]}
    if not DATA_FILE.exists():
        return {"scraped_at": None, "age_seconds": None}
    mtime = DATA_FILE.stat().st_mtime
    scraped_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    age = datetime.now(timezone.utc).timestamp() - mtime
    return {"scraped_at": scraped_at, "age_seconds": int(age)}


def parse_keywords(raw: str) -> list[str]:
    return [part.strip().lower() for part in (raw or "").split(",") if part.strip()]


def format_time_ago(posted_at, fallback_days: int = 0, *, date_only: bool = False) -> str:
    """Relative label for the job card: '2 min ago', '1 hour ago', '7 days ago', '1 month ago'."""
    posted = _as_utc(posted_at)
    if posted is None:
        return _ago_from_days(fallback_days)
    # Calendar-day stamps (JobSpy LinkedIn) are midnight UTC. Measuring hours from
    # midnight makes "posted today" look like "14 hours ago" instead of LinkedIn's
    # real "6 hours ago". Keep day-granularity until a precise stamp is saved.
    if date_only or _is_midnight(posted):
        days = (datetime.now(timezone.utc).date() - posted.date()).days
        return _ago_from_days(max(days, 0))
    seconds = int((datetime.now(timezone.utc) - posted).total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = seconds // 3600
    if hours < 24:
        return "1 hour ago" if hours == 1 else f"{hours} hours ago"
    days = seconds // 86400
    if days < 30:
        return "1 day ago" if days == 1 else f"{days} days ago"
    months = days // 30
    if months < 12:
        return "1 month ago" if months == 1 else f"{months} months ago"
    years = days // 365
    return "1 year ago" if years == 1 else f"{years} years ago"


def _as_utc(value) -> datetime | None:
    if isinstance(value, datetime):
        posted = value
    elif value:
        cleaned = str(value).strip().replace("Z", "+00:00")
        try:
            posted = datetime.fromisoformat(cleaned)
        except ValueError:
            return None
    else:
        return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return posted


def _is_midnight(posted: datetime) -> bool:
    return posted.hour == 0 and posted.minute == 0 and posted.second == 0 and posted.microsecond == 0


def _ago_from_days(days: int) -> str:
    try:
        count = max(int(days or 0), 0)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return "just now"
    if count == 1:
        return "1 day ago"
    if count < 30:
        return f"{count} days ago"
    months = count // 30
    if months < 12:
        return "1 month ago" if months == 1 else f"{months} months ago"
    years = count // 365
    return "1 year ago" if years == 1 else f"{years} years ago"


def _with_time_ago(jobs: list[dict]) -> list[dict]:
    presented = []
    for job in jobs:
        row = dict(job)
        try:
            fallback = int(job.get("daysAgo") or 0)
        except (TypeError, ValueError):
            fallback = 0
        precise = job.get("platform_posted_at") or job.get("platformPostedAt")
        posted = precise or job.get("posted_at")
        stamp = _as_utc(posted)
        date_only = stamp is not None and _is_midnight(stamp) and not (
            precise and not _is_midnight(_as_utc(precise) or stamp)
        )
        row["daysAgo"] = format_time_ago(posted, fallback, date_only=date_only)
        presented.append(row)
    attach_descriptions(presented)
    if len(presented) <= 200:
        attach_raw_descriptions(presented)
    from jobscraper.agent_reach import company_career_url_for_job

    for row in presented:
        # Column value, else the offsite apply link saved on the scrape, else the
        # listing URL when that listing is already the employer career page.
        row["company_career_url"] = company_career_url_for_job(row) or None
    attach_missing_career_urls(presented)
    return presented


def _listing_max_age(days: int) -> int | None:
    """None keeps every stored job.

    Explore Jobs calls /api/jobs?days=30. That window used to drop anything
    older than 30 days, so the page showed ~6,000 jobs while ksajob.jobs held
    9,000+. days=0 and days>=30 both return the full table. 1, 3, and 7 still limit it.
    """
    try:
        window = int(days)
    except (TypeError, ValueError):
        return None
    if window <= 0 or window >= 30:
        return None
    return window


def filter_jobs(
    jobs: list[dict],
    keywords: list[str],
    state: str,
    platform: str,
    days: int,
    city: str = "",
    ksa_only: bool = True,
) -> list[dict]:
    max_age = _listing_max_age(days)
    filtered = []
    for job in jobs:
        if ksa_only and not is_ksa_job(
            job.get("state") or "",
            job.get("location") or "",
            allow_bare_remote=(job.get("platform") or "") in KSA_SCOPED_PLATFORMS,
        ):
            continue
        if platform != "All" and job.get("platform") != platform:
            continue
        if max_age is not None and int(job.get("daysAgo") or 0) > max_age:
            continue
        if not matches_state_filter(job.get("state") or "", job.get("location") or "", state):
            continue
        if not matches_city_filter(job.get("location") or "", city):
            continue
        blob = " ".join(
            [
                str(job.get("title") or ""),
                str(job.get("company") or ""),
                str(job.get("location") or ""),
                str(job.get("description") or ""),
                " ".join(job.get("skills") or []),
            ]
        ).lower()
        if keywords and not all(keyword in blob for keyword in keywords):
            continue
        filtered.append(job)
    filtered.sort(key=lambda row: (row.get("daysAgo") or 0, row.get("title") or ""))
    return filtered


DEFAULT_JOBS_PAGE_SIZE = 20


def _page_size(limit: int) -> int:
    """Page size for recommend. `limit` is not a cap on how many jobs can match."""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _page(jobs: list[dict], limit: int | None, offset: int) -> list[dict]:
    start = max(offset, 0)
    if limit is None:
        return jobs[start:]
    return jobs[start : start + max(limit, 0)]


def _resolve_jobs_window(limit: int | None, offset: int, page: int | None) -> tuple[int | None, int]:
    """Page wins over offset. A bare page uses the default page size."""
    if page is not None:
        size = DEFAULT_JOBS_PAGE_SIZE if limit is None else limit
        return size, (page - 1) * size
    return limit, max(offset, 0)


def _pagination(total: int, limit: int | None, offset: int) -> dict:
    page_size = total if limit is None else limit
    if total <= 0 or page_size <= 0:
        current = 1
        total_pages = 0
    else:
        current = (offset // page_size) + 1
        total_pages = (total + page_size - 1) // page_size
    return {
        "page": current,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_next": current < total_pages,
        "has_previous": current > 1,
    }


def _empty_cache_hint() -> str:
    return (
        "No listings in cache. POST /api/scrape?keywords=python,fastapi first, "
        "wait for the twice-daily scrape, or call /api/recommend with refresh=true."
    )


def _skills_from_inputs(
    skills: list[str] | None = None,
    resume_text: str | None = None,
    resume: dict | None = None,
) -> list[str]:
    found = normalize_skills(skills)
    if found:
        return found
    if resume:
        found = normalize_skills(resume.get("skills") or [])
        if found:
            return found
    if resume_text and resume_text.strip():
        try:
            parsed = parse_resume_text(resume_text)
        except ResumeParseError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        found = normalize_skills(parsed.get("skills") or [])
    if not found:
        raise HTTPException(
            status_code=400,
            detail="Send at least one skill, or a resume / resume_text that contains a skills section.",
        )
    return found


def _store_candidate(candidate_id: str, profile: dict) -> dict:
    saved = normalize_profile(profile)
    if mysql_configured():
        try:
            save_candidate_mysql(candidate_id, saved)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not save candidate: {exc}") from exc
        return saved
    save_candidate_file(candidate_id, saved)
    return saved


def _stored_candidate(candidate_id: str) -> dict | None:
    if mysql_configured():
        try:
            return load_candidate_mysql(candidate_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not load candidate: {exc}") from exc
    return load_candidate_file(candidate_id)


def _candidate_id_or_http(value: str) -> str:
    try:
        return clean_candidate_id(value)
    except CandidateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _years_from_resume_jobs(jobs: list[dict] | None) -> int | None:
    history = []
    for item in jobs or []:
        if not isinstance(item, dict):
            continue
        end = str(item.get("end_date") or "")
        if item.get("is_current") and not end:
            end = "Present"
        history.append({"start_date": item.get("start_date") or "", "end_date": end})
    return career_years({"work_history": history})


def _profile_match_inputs(
    profile: dict | None,
    skills: list[str],
    certifications: list[str] | None = None,
    experience_titles: list[str] | None = None,
    years: int | None = None,
) -> tuple[list[str], list[str], list[str], int | None]:
    certs = list(certifications or [])
    titles = list(experience_titles or [])
    if profile is not None:
        if not certs:
            certs = certifications_from_profile(profile)
        if not titles:
            titles = experience_titles_from_profile(profile)
        if years is None:
            years = career_years(profile)
    return skills, normalize_skills(certs), titles, years


def _scrape_terms(
    skills: list[str],
    certifications: list[str],
    experience_titles: list[str] | None = None,
) -> list[str]:
    """Titles first, then certifications and skills. Related names fill leftover slots."""
    from jobscraper.jd_to_cv import SKILL_RELATED

    terms: list[str] = []
    seen: set[str] = set()

    def add(raw) -> None:
        text = search_phrase(str(raw or ""))
        if not text or text.lower() in seen or len(terms) >= 40:
            return
        seen.add(text.lower())
        terms.append(text)

    for term in [*(experience_titles or []), *certifications, *skills]:
        add(term)
    for term in list(terms):
        for related in SKILL_RELATED.get(term.lower(), ()):
            add(related)
    return terms


def _score_settings() -> dict:
    """Weights and cutoff currently saved on the admin configuration screen."""
    cfg = score_config()
    return {
        "weights": {
            "skills": cfg["skills"],
            "certifications": cfg["certifications"],
            "title": cfg["title"],
            "years_level": cfg["years_level"],
        },
        "min_score": cfg["min_score"],
    }


def _no_match_hint(*, refresh: bool = False) -> str:
    cutoff = int(score_config()["min_score"])
    hint = (
        f"Listings were found, but none scored {cutoff} or higher against these skills, "
        "certifications, or experience."
    )
    if refresh:
        hint += " Try refresh=true."
    return hint


def _recommend_payload(
    *,
    skills: list[str],
    state: str,
    platform: str,
    days: int,
    city: str,
    limit: int,
    refresh: bool,
    page: int = 1,
    candidate_id: str | None = None,
    profile: dict | None = None,
    certifications: list[str] | None = None,
    experience_titles: list[str] | None = None,
    years: int | None = None,
) -> dict:
    skills, certifications, experience_titles, years = _profile_match_inputs(
        profile, skills, certifications, experience_titles, years
    )
    if refresh:
        terms = _scrape_terms(skills, certifications, experience_titles)
        if terms:
            if not _claim_scrape():
                raise HTTPException(status_code=409, detail="A scrape is already running. Try again in a moment.")
            try:
                _arm_scrape_counts()
                run_spider(",".join(terms), state, platform, city, _scrape_target())
            except subprocess.TimeoutExpired as exc:
                raise HTTPException(status_code=504, detail="Job scrape timed out.") from exc
            finally:
                SCRAPE_LOCK.release()
    list_state = "All" if profile is not None and (not state or state == "All") else state
    list_city = "" if profile is not None and not city else city
    pool = load_jobs(description_chars=4000)
    filtered = filter_jobs(pool, [], list_state, platform, days, list_city)
    if profile is not None:
        filtered = filter_jobs_for_candidate(filtered, profile, state=state, city=city)
    ranked_all = recommend_jobs(
        filtered,
        skills,
        certifications=certifications,
        experience_titles=experience_titles,
        years=years,
        limit=None,
    )
    page_num = max(page, 1)
    page_size = _page_size(limit)
    start = (page_num - 1) * page_size
    ranked = ranked_all[start : start + page_size]
    hint = None
    if not pool:
        hint = _empty_cache_hint()
    elif not filtered and profile is not None:
        hint = "No listings matched this candidate's location, remote, or visa preferences."
    elif not filtered:
        hint = _empty_cache_hint()
    elif not ranked_all:
        hint = _no_match_hint(refresh=True)
    payload = {
        "ok": True,
        "count": len(ranked),
        "total": len(ranked_all),
        "skills": skills,
        "certifications": certifications,
        "experience_titles": experience_titles,
        "years": years,
        **_score_settings(),
        "jobs": _with_time_ago(ranked),
        "pagination": _pagination(len(ranked_all), page_size, start),
        "hint": hint,
        **cache_meta(),
    }
    if candidate_id:
        payload["candidate_id"] = candidate_id
    if profile is not None:
        payload["filters"] = applied_filters(profile, state=state, city=city)
    return payload


def _saved_before_stop() -> dict:
    """Jobs already written by the spider, plus any feed file left after a kill."""
    stats = read_scrape_progress_stats()
    partial = load_jobs_json()
    if not partial:
        return stats
    try:
        # Mid-scrape rows are usually already in MySQL. Only count listings that
        # were not part of the running progress total.
        result = upsert_jobs(partial)
        if stats["saved"] <= 0:
            stats = {
                "saved": result.saved,
                "new": result.new,
                "duplicates": result.duplicates,
            }
        else:
            stats = {
                "saved": max(stats["saved"], len(partial)),
                "new": stats["new"],
                "duplicates": stats["duplicates"],
            }
        note_scrape_progress(
            stats["saved"],
            new=stats["new"],
            duplicates=stats["duplicates"],
        )
    except Exception:
        log.exception("Could not save the jobs collected before the scrape stopped")
    return stats


def _finish_scrape_stats(
    run_id: int | None,
    stats: dict | None = None,
    *,
    error: str | None = None,
) -> int:
    """Store the spider's own saved/new/duplicate totals on the run row.

    Those three numbers are what saved_so_far shows. A timestamp scan of the
    jobs table also counts Headcount, ATS, and Global Jobs rows touched while
    this run was open, so that scan is only a fallback.
    """
    stats = dict(stats or read_scrape_progress_stats())
    saved = int(stats.get("saved") or 0)
    new = int(stats.get("new") or 0)
    duplicates = int(stats.get("duplicates") or 0)
    if saved and new + duplicates == 0:
        db_stats = scrape_run_save_stats(run_id)
        if db_stats is not None:
            saved = int(db_stats.get("saved") or 0)
            new = int(db_stats.get("new") or 0)
            duplicates = int(db_stats.get("duplicates") or 0)
    finish_scrape_run(
        run_id,
        job_count_value=saved,
        new_count=new,
        duplicate_count=duplicates,
        error=error,
    )
    note_scrape_progress(saved, new=new, duplicates=duplicates)
    return saved


def run_spider(keywords: str, location: str, platform: str, city: str = "", target: int = 0) -> int:
    DATA_FILE.parent.mkdir(exist_ok=True)
    if DATA_FILE.exists():
        try:
            DATA_FILE.unlink()
        except OSError:
            log.warning("jobs.json is open in another process. This scrape still saves each search to MySQL.")
    _arm_scrape_counts()
    # The keyword list is long (every database skill and certification).
    # Passing it on the Windows command line gets cut, so the spider starts
    # with no terms and exits in a couple of seconds with zero jobs.
    keyword_file = DATA_FILE.parent / "scrape_keywords.txt"
    keyword_file.write_text(keywords, encoding="utf-8")
    run_id = start_scrape_run(keywords, location, platform)
    try:
        result = sync_gulf_jobs()
        log.info("Gulf Dev Jobs sync during scrape: %s", result)
    except Exception:
        log.exception("Gulf Dev Jobs sync failed during scrape")
    cmd = [
        sys.executable,
        "-m",
        "scrapy",
        "crawl",
        "jobs",
        "-a",
        f"keywords_file={keyword_file}",
        "-a",
        f"location={location}",
        "-a",
        f"city={city}",
        "-a",
        f"platform={platform}",
        "-a",
        f"target={max(0, int(target or 0))}",
        "-s",
        "LOG_LEVEL=INFO",
    ]
    # Each term now reads three pages and four cities. A full run can take a few hours.
    # Jobs are saved after every page, so a timeout still keeps what was found.
    timeout = 14400 if target >= 1000 else SCRAPE_TIMEOUT
    try:
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        stats = _saved_before_stop()
        if stats["saved"]:
            saved = _finish_scrape_stats(run_id, stats)
            start_dice_description_backfill()
            log.warning(
                "Scrape hit the time limit after saving %s jobs (new=%s duplicates=%s)",
                saved,
                stats["new"],
                stats["duplicates"],
            )
            return saved
        finish_scrape_run(run_id, job_count_value=0, error="timeout")
        raise
    except Exception as exc:
        finish_scrape_run(run_id, job_count_value=0, error=str(exc)[:2000])
        raise
    if result.returncode != 0:
        stats = _saved_before_stop()
        if stats["saved"]:
            saved = _finish_scrape_stats(run_id, stats)
            start_dice_description_backfill()
            return saved
        detail = (result.stderr or result.stdout or "Scrapy crawl failed").strip()
        finish_scrape_run(run_id, job_count_value=0, error=detail[-2000:])
        raise HTTPException(status_code=502, detail=detail[-2000:])
    this_run = load_jobs_json()
    progress = read_scrape_progress_stats()
    if not this_run:
        if progress["saved"]:
            saved = _finish_scrape_stats(run_id, progress)
            start_dice_description_backfill()
            return saved
        detail = "\n".join(part.strip() for part in (result.stderr, result.stdout) if part and part.strip())
        detail = (detail or "Scrape ended with 0 jobs. The boards were not searched.").strip()
        finish_scrape_run(run_id, job_count_value=0, error=detail[-2000:])
        raise HTTPException(status_code=502, detail=detail[-2000:])
    try:
        # Prefer mid-scrape progress for new/duplicate counts. A final upsert of
        # the same feed would mark almost every row as a duplicate.
        if progress["saved"] <= 0:
            result_stats = upsert_jobs(this_run)
            progress = {
                "saved": result_stats.saved or len(this_run),
                "new": result_stats.new,
                "duplicates": result_stats.duplicates,
            }
            note_scrape_progress(
                progress["saved"],
                new=progress["new"],
                duplicates=progress["duplicates"],
            )
        else:
            upsert_jobs(this_run, notify=False)
            # Keep the spider totals. len(jobs.json) can differ, and that
            # second number was what made the run row disagree with saved_so_far.
            saved_count = int(progress["saved"])
            if target > 0:
                saved_count = min(saved_count, int(target))
            progress = {
                "saved": saved_count,
                "new": progress["new"],
                "duplicates": progress["duplicates"],
            }
    except Exception as exc:
        if progress["saved"]:
            saved = _finish_scrape_stats(run_id, progress)
            start_dice_description_backfill()
            return saved
        detail = f"MySQL save failed: {exc}"
        finish_scrape_run(run_id, job_count_value=0, error=detail[:2000])
        raise HTTPException(status_code=502, detail=detail[:2000]) from exc
    kept = _finish_scrape_stats(run_id, progress)
    # The startup refill has already finished by the time a scheduled scrape
    # saves new LinkedIn rows. Run it again for the rows that arrived with no body.
    start_dice_description_backfill()
    return kept


def _resume_response(parsed: dict, include_raw: bool) -> dict:
    if not include_raw:
        parsed = {**parsed, "raw_text": None}
    return parsed


def _parse_or_http(data: bytes, filename: str, content_type: str) -> dict:
    try:
        return parse_resume_bytes(data, filename=filename, content_type=content_type)
    except ResumeParseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _swap_extension(filename: str, extension: str) -> str:
    stem = Path(filename or "converted").stem.strip() or "converted"
    stem = re.sub(r"[^\w.\-]+", "_", stem).strip("._") or "converted"
    return f"{stem}{extension}"


def _file_download(content: bytes, filename: str, media_type: str) -> Response:
    encoded = quote(filename)
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{encoded}",
        },
    )


def _html_preview(title: str, text: str) -> HTMLResponse:
    body = html.escape(text or "").replace("\n", "<br>\n")
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: Calibri, Arial, sans-serif; max-width: 760px; margin: 24px auto; color: #0f172a; line-height: 1.5; }}
    h1 {{ font-size: 1rem; color: #64748b; font-weight: 600; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <div>{body}</div>
</body>
</html>"""
    return HTMLResponse(page)


def _extract_multipart_file(data: bytes) -> tuple[bytes, str, str] | None:
    """Pull the first file part out of a raw multipart body (wrong Content-Type)."""
    stripped = data.lstrip()
    if not stripped.startswith(b"--"):
        return None
    boundary, _, _rest = stripped.partition(b"\n")
    boundary = boundary.strip().rstrip(b"\r")
    if len(boundary) < 4:
        return None
    for part in stripped.split(boundary):
        part = part.lstrip(b"\r\n")
        if b"filename=" not in part[:1200]:
            continue
        header, sep, body = part.partition(b"\r\n\r\n")
        if not sep:
            header, sep, body = part.partition(b"\n\n")
        if not sep:
            continue
        match = re.search(br'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', header, re.I)
        filename = match.group(1).decode("latin-1", "ignore").strip() if match else "upload"
        ctype_match = re.search(br"Content-Type:\s*([^\r\n]+)", header, re.I)
        ctype = ctype_match.group(1).decode("ascii", "ignore").strip() if ctype_match else ""
        body = body.rstrip(b"\r\n")
        if body.endswith(b"--"):
            body = body[:-2].rstrip(b"\r\n")
        if body:
            return body, filename, ctype
    return None


def _filename_from_bytes(data: bytes, content_type: str, fallback: str) -> str:
    header_name = fallback.strip()
    if header_name:
        return header_name
    if data.startswith(b"%PDF"):
        return "upload.pdf"
    if data[:2] == b"PK":
        return "upload.docx"
    if (content_type or "").startswith("text/"):
        return "upload.txt"
    return "upload.bin"


async def _load_resume_upload(request: Request, file: UploadFile | None) -> tuple[bytes, str, str]:
    if file is not None:
        data = await file.read()
        if data:
            return data, file.filename or "resume", file.content_type or ""

    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        for value in form.values():
            filename = getattr(value, "filename", None)
            if not filename:
                continue
            data = await value.read()
            if data:
                return data, filename, getattr(value, "content_type", "") or ""

    data = await request.body()
    extracted = _extract_multipart_file(data) if data else None
    if extracted:
        return extracted
    if data:
        filename = _filename_from_bytes(
            data,
            content_type,
            request.headers.get("x-filename") or "",
        )
        return data, filename, content_type.split(";")[0].strip()

    raise HTTPException(
        status_code=400,
        detail=(
            "No file received. In Postman use Body → form-data, key=file, type=File, "
            "and uncheck Headers → Content-Type. Or use Body → binary with "
            "Content-Type application/pdf."
        ),
    )


def _whatsapp_group_status() -> dict:
    try:
        from jobscraper.whatsapp_alerts import group_sweep_status

        return group_sweep_status()
    except Exception:
        return {"error": "unavailable"}


@app.get("/api/health")
def health():
    mysql = mysql_health()
    return {
        "ok": True,
        "service": "job-discovery",
        "jobs_list": True,
        "jobs_recommend": True,
        "company_careers": True,
        "country": "Saudi Arabia",
        "save_fix": 4,
        "resume_parse": True,
        "profile_autofill": True,
        "profile_linkedin": True,
        "ats_score": True,
        "cv_from_job": True,
        "resume_enhance": True,
        "pdf_docx_convert": True,
        "resume_formats": ["pdf", "docx", "txt"],
        "mysql": mysql,
        "scrape_schedule": {
            "enabled": _truthy("SCRAPE_SCHEDULE_ENABLED", "true"),
            "tz": _env("SCRAPE_TZ", "Asia/Riyadh") or "Asia/Riyadh",
            "hours": _schedule_hours(),
            "target": _scrape_target(),
        },
        "globaljobs": globaljobs_status(),
        "gulfdevjobs": gulfdevjobs_status(),
        "headcount": headcount_status(),
        "ats_boards": ats_boards_status(),
        "inactive_sources": inactive_source_ids(),
        "whatsapp_group": _whatsapp_group_status(),
    }


@app.get("/")
def home():
    return FileResponse(INDEX_FILE)


@app.get("/dashboard")
@app.get("/dashboard.html")
def dashboard_page():
    return FileResponse(DASHBOARD_FILE)


@app.get("/api/filters")
def filters():
    return {
        "states": ["All", "Remote", *DROPDOWN_STATES],
        "regions": ["All", "Remote", *DROPDOWN_STATES],
        "platforms": [
            "All",
            "Indeed",
            "LinkedIn",
            "Google Jobs",
            "Bayt",
            "Dev Global Jobs",
            "Gulf Dev Jobs",
            "Headcount Labs",
            "Greenhouse",
            "Lever",
            "SmartRecruiters",
            "Workday",
        ],
        "days": [0, 1, 3, 7],
        "work_models": list(WORK_MODELS),
        "job_types": [
            {"value": "fulltime", "label": "Full-time"},
            {"value": "contract", "label": "Contract"},
            {"value": "parttime", "label": "Part-time"},
            {"value": "internship", "label": "Internship"},
        ],
        "experience_levels": list(EXPERIENCE_LEVELS),
        "visa_sponsorship": ["yes", "no"],
        "clearance_required": ["yes", "no"],
        "saudi_national_required": ["yes", "no"],
    }


@app.get("/api/v1/sources", summary="List job sources and whether each one is scraped")
def job_sources():
    """Public source list. status inactive means the scraper skips that source."""
    return source_envelope("Job sources retrieved.", list_sources())


@app.get("/api/v1/sources/{source_id}", summary="One job source")
def job_source(source_id: str):
    row = get_source(source_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content=source_envelope(
                "Job source not found.",
                None,
                success=False,
                errors={"id": "Unknown source."},
            ),
        )
    return source_envelope("Job source retrieved.", row)


@app.patch("/api/v1/sources/{source_id}", summary="Turn a job source on or off")
@app.put("/api/v1/sources/{source_id}", summary="Turn a job source on or off")
def update_job_source(source_id: str, body: SourceStatusBody):
    """Set status to inactive and the next scrape skips this source. active turns it back on."""
    cleaned = str(body.status or "").strip().lower()
    if cleaned not in {"active", "inactive"}:
        return JSONResponse(
            status_code=422,
            content=source_envelope(
                "Status must be active or inactive.",
                None,
                success=False,
                errors={"status": "Use active or inactive."},
            ),
        )
    try:
        row = set_source_status(source_id, cleaned)
    except Exception:
        log.exception("Could not update job source %s", source_id)
        return JSONResponse(
            status_code=500,
            content=source_envelope(
                "Could not update the job source.",
                None,
                success=False,
                errors={"status": "Save failed."},
            ),
        )
    if row is None:
        return JSONResponse(
            status_code=404,
            content=source_envelope(
                "Job source not found.",
                None,
                success=False,
                errors={"id": "Unknown source."},
            ),
        )
    if cleaned == "inactive":
        message = "Job source is inactive. Jobs are no longer scraped from this source."
    else:
        message = "Job source is active. Jobs will be scraped from this source again."
    return source_envelope(message, row)


@app.get("/api/jobs")
def get_jobs(
    keywords: str = Query("", description="Comma-separated title/company/skill keywords"),
    state: str = Query("All"),
    platform: str = Query("All"),
    days: int = Query(
        0,
        description="0 returns every job in the database. 1, 3, or 7 keep a shorter window. 30 also returns every job.",
    ),
    city: str = Query(""),
    work_model: str = Query("", description="Comma-separated: remote, hybrid, onsite"),
    job_type: str = Query("", description="Comma-separated: fulltime, contract, parttime, internship"),
    experience_level: str = Query(
        "",
        description="Comma-separated: Intern/New Grad, Entry Level, Mid Level, Senior Level, Lead/Staff, Director/Executive",
    ),
    visa_sponsorship: str = Query("", description="yes or no. Iqama or work-visa sponsorship."),
    clearance_required: str = Query("", description="yes or no"),
    saudi_national_required: str = Query("", description="yes or no"),
    years: int | None = Query(
        None,
        ge=0,
        le=40,
        description="Keep jobs whose required years are at or below this number. Unknown years stay included.",
    ),
    limit: int | None = Query(
        None,
        ge=1,
        le=200,
        description="Page size, max 200. Omit to return every match unless page is set.",
    ),
    offset: int = Query(0, ge=0, description="Rows to skip. Ignored when page is set."),
    page: int | None = Query(
        None,
        ge=1,
        description="1-based page. Overrides offset. Uses limit, or 20 when limit is omitted.",
    ),
):
    jobs = filter_jobs(load_jobs(), parse_keywords(keywords), state, platform, days, city)
    jobs = apply_listing_filters(
        jobs,
        work_model=work_model,
        job_type=job_type,
        experience_level=experience_level,
        visa_sponsorship=visa_sponsorship,
        clearance_required=clearance_required,
        saudi_national_required=saudi_national_required,
        years=years,
    )
    limit, offset = _resolve_jobs_window(limit, offset, page)
    page_rows = _page(jobs, limit, offset)
    total = len(jobs)
    payload = {
        "jobs": _with_time_ago(page_rows),
        "count": len(page_rows),
        "total": total,
        "limit": limit,
        "offset": offset,
        "pagination": _pagination(total, limit, offset),
        "source": "mysql" if mysql_configured() else "json",
        **cache_meta(),
    }
    db_error = getattr(load_jobs_mysql, "last_error", None)
    if db_error:
        payload["mysql_error"] = db_error
        payload["stored"] = mysql_job_count()
    elif total == 0 and mysql_configured():
        payload["stored"] = mysql_job_count()
    return payload


class CompanyCareersRequest(BaseModel):
    company: str = Field(..., min_length=2, max_length=120, description="Company name, for example Aramco or Stripe")
    location: str = Field("", max_length=80, description="Optional location text, for example Riyadh or Saudi Arabia")
    limit: int = Field(50, ge=1, le=100)
    refresh: bool = Field(False, description="Bypass the short cache and read the career portal again")


def _company_careers(company: str, location: str, limit: int, refresh: bool) -> dict:
    try:
        return lookup_company_jobs(company, location=location, limit=limit, refresh=refresh)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/companies/careers", summary="Open jobs on a company's career portal")
def company_careers(
    company: str = Query(..., min_length=2, max_length=120, description="Company name, for example Aramco or Stripe"),
    location: str = Query("", max_length=80, description="Keep jobs whose location contains this text"),
    limit: int = Query(50, ge=1, le=100),
    refresh: bool = Query(False, description="Bypass the short cache and read the career portal again"),
):
    """Find the company's public career page and return the jobs listed there."""
    return _company_careers(company, location, limit, refresh)


@app.post("/api/companies/careers", summary="Open jobs on a company's career portal")
def company_careers_post(body: CompanyCareersRequest):
    """Find the company's public career page and return the jobs listed there."""
    return _company_careers(body.company, body.location, body.limit, body.refresh)


@app.post("/api/scrape")
def scrape_jobs(
    keywords: str = Query(""),
    state: str = Query("All"),
    platform: str = Query("All"),
    days: int = Query(30),
    city: str = Query(""),
    target: int = Query(0, ge=0, le=20000, description="How many jobs to aim for. 5000 searches many fields and takes one to two hours."),
):
    """Start a scrape and return immediately. IIS closes long requests with 502."""
    blocked = board_scrape_block(platform)
    if blocked:
        raise HTTPException(status_code=400, detail=blocked)
    if not _claim_scrape():
        raise HTTPException(status_code=409, detail="A scrape is already running. Try again in a moment.")
    thread = threading.Thread(
        target=_scrape_background,
        args=(keywords, state, platform, city, target),
        name="job-scrape",
        daemon=True,
    )
    thread.start()
    response_target = _scrape_target() if not (keywords or "").strip() and target <= 0 else target
    message = "Scrape started. Poll GET /api/scrape/status until running is false. running stays true while boards are still being searched."
    if response_target >= 1000:
        message = (
            f"Large scrape started. It stops at {response_target} jobs across database skills and certifications. "
            "Jobs are saved after each search. Poll GET /api/scrape/status and watch saved_so_far."
        )
    return JSONResponse(
        status_code=202,
        content={
            "ok": True,
            "started": True,
            "running": True,
            "target": response_target,
            "message": message,
        },
    )


def _scrape_background(keywords: str, state: str, platform: str, city: str, target: int = 0) -> None:
    try:
        _arm_scrape_counts()
        from_database = not (keywords or "").strip()
        keywords = _keywords_for_scrape(keywords)
        if not keywords:
            _store_last_scrape(
                ok=False,
                error="SCRAPE_KEYWORDS is empty and the database has no skills, certifications, or job titles.",
                count=0,
                db_total=mysql_job_count() if mysql_configured() else 0,
            )
            return
        # Postman sends no target. A database scrape should keep running across
        # the stored skills and certifications, not exit after an empty pass.
        if from_database and target <= 0:
            target = _scrape_target()
        count = run_spider(keywords, state, platform, city, target) or _kept_scrape_count()
        db_total = mysql_job_count() if mysql_configured() else count
        _store_last_scrape(ok=True, error=None, count=count, db_total=db_total)
        log.info("Scrape saved %s jobs (db_total=%s)", count, db_total)
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        saved = read_scrape_progress()
        db_total = mysql_job_count() if mysql_configured() else None
        _store_last_scrape(ok=saved > 0, error=str(detail)[:500], count=saved, db_total=db_total)
        log.exception("Background scrape failed")
    finally:
        try:
            from jobscraper.whatsapp_alerts import queue_unsent_group_jobs

            queue_unsent_group_jobs()
        except Exception:
            log.exception("Could not send unsent WhatsApp group alerts after scrape")
        SCRAPE_LOCK.release()


@app.get("/api/scrape/status")
def scrape_status():
    # While a scrape is running, saved_so_far is the spider's own total.
    # The progress file still holds the previous run until this scrape zeros
    # it, so those counts stay hidden until then. When the scrape is idle,
    # report the latest job_discovery_scrape_runs row so saved_so_far,
    # new_so_far, and duplicates_so_far match that table.
    running = SCRAPE_LOCK.locked()
    if running and not _SCRAPE_COUNTS_READY.is_set():
        progress = {"saved": 0, "new": 0, "duplicates": 0}
    elif running:
        progress = read_scrape_progress_stats()
    else:
        latest = latest_finished_scrape()
        if latest and int(latest.get("job_count") or 0) > 0:
            progress = {
                "saved": int(latest["job_count"]),
                "new": int(latest["new_count"] or 0),
                "duplicates": int(latest["duplicate_count"] or 0),
            }
        else:
            progress = read_scrape_progress_stats()
    return {
        "ok": True,
        "running": running,
        "saved_so_far": progress["saved"],
        "new_so_far": progress["new"],
        "duplicates_so_far": progress["duplicates"],
        "last_scrape": _LAST_SCRAPE,
        "mysql": mysql_health(),
        "schedule": {
            "enabled": _truthy("SCRAPE_SCHEDULE_ENABLED", "true"),
            "tz": _env("SCRAPE_TZ", "Asia/Riyadh") or "Asia/Riyadh",
            "hours": _schedule_hours(),
            "keywords": _env("SCRAPE_KEYWORDS", ""),
            "target": _scrape_target(),
            "state": _env("SCRAPE_STATE", "All") or "All",
            "platform": _env("SCRAPE_PLATFORM", "All") or "All",
        },
        "globaljobs": globaljobs_status(),
        "gulfdevjobs": gulfdevjobs_status(),
        "headcount": headcount_status(),
        "ats_boards": ats_boards_status(),
        "inactive_sources": inactive_source_ids(),
        "whatsapp_group": _whatsapp_group_status(),
        **cache_meta(),
    }


@app.get("/api/recommend")
def recommend_contract():
    return {
        "ok": True,
        "service": "jobs-recommend",
        "endpoints": {
            "listings": {
                "method": "GET",
                "path": "/api/jobs",
                "query": {
                    "keywords": "python,fastapi",
                    "state": "All",
                    "platform": "All",
                    "days": 0,
                    "city": "",
                    "limit": 50,
                    "offset": 0,
                    "page": 1,
                },
            },
            "scrape": {
                "method": "POST",
                "path": "/api/scrape",
                "query": {
                    "keywords": "python,fastapi",
                    "state": "All",
                    "platform": "All",
                    "days": 30,
                    "city": "",
                },
            },
            "recommended_roles": {
                "method": "GET",
                "path": "/api/recommend/roles",
                "example": "/api/recommend/roles?skills=Python,FastAPI,AWS&state=All&platform=All&days=0&limit=25&page=1",
                "query": {
                    "skills": "Python,FastAPI,AWS",
                    "state": "All",
                    "platform": "All",
                    "days": 0,
                    "city": "",
                    "limit": 25,
                    "page": 1,
                },
            },
            "save_candidate": {
                "method": "PUT",
                "path": "/api/candidates/{candidate_id}",
                "content_type": "application/json",
                "body": {
                    "first_name": "Zain",
                    "last_name": "Mohammed",
                    "email": "name@company.com",
                    "city": "San Francisco",
                    "state": "CA",
                    "work_history": [
                        {
                            "company": "Google",
                            "job_title": "Software Engineer",
                            "start_date": "2022-01",
                            "end_date": "Present",
                            "description": "Python, FastAPI, AWS",
                        }
                    ],
                    "open_to_remote": "yes",
                    "willing_to_relocate": "no",
                    "need_visa_sponsorship": "no",
                    "authorized_to_work": "yes",
                },
            },
            "recommend": {
                "method": "POST",
                "path": "/api/recommend",
                "content_type": "application/json",
                "body": {
                    "candidate_id": "123",
                    "skills": ["Python", "FastAPI", "AWS"],
                    "resume_text": "optional raw resume text",
                    "resume": "optional parsed resume JSON",
                    "state": "All",
                    "platform": "All",
                    "days": 0,
                    "limit": 25,
                    "refresh": False,
                },
            },
            "recommend_file": {
                "method": "POST",
                "path": "/api/recommend/file",
                "content_type": "multipart/form-data",
                "fields": {
                    "file": "optional resume PDF/DOCX/TXT",
                    "skills": "optional comma-separated skills",
                    "state": "All",
                    "platform": "All",
                    "days": 0,
                    "limit": 25,
                    "refresh": False,
                },
            },
        },
        "docs": "/docs",
    }


@app.get("/api/recommend/roles", summary="Recommended Roles: rank listings by skills, certifications, and experience")
def recommend_roles(
    skills: str = Query("", description="Comma-separated skills, for example Python,FastAPI,AWS"),
    state: str = Query("All"),
    platform: str = Query("All"),
    days: int = Query(0, description="0 returns every job in the database. 1, 3, or 7 keep a shorter window."),
    city: str = Query(""),
    work_model: str = Query("", description="Comma-separated: remote, hybrid, onsite"),
    job_type: str = Query("", description="Comma-separated: fulltime, contract, parttime, internship"),
    experience_level: str = Query(""),
    visa_sponsorship: str = Query("", description="yes or no"),
    clearance_required: str = Query("", description="yes or no"),
    saudi_national_required: str = Query("", description="yes or no"),
    years: int | None = Query(None, ge=0, le=40),
    certifications: str = Query("", description="Comma-separated certifications, for example PMP,AWS Certified"),
    experience: str = Query("", description="Comma-separated past job titles"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    page: int = Query(1, ge=1),
):
    found = parse_skill_query(skills)
    certs = parse_skill_query(certifications)
    titles = [part.strip() for part in experience.split(",") if part.strip()]
    if not found and not certs and not titles:
        raise HTTPException(
            status_code=400,
            detail="Send skills, certifications, or experience titles. Example: skills=Python,FastAPI&certifications=PMP.",
        )
    pool = load_jobs(description_chars=4000)
    filtered = filter_jobs(pool, [], state, platform, days, city)
    filtered = apply_listing_filters(
        filtered,
        work_model=work_model,
        job_type=job_type,
        experience_level=experience_level,
        visa_sponsorship=visa_sponsorship,
        clearance_required=clearance_required,
        saudi_national_required=saudi_national_required,
        years=years,
    )
    ranked_all = recommend_jobs(
        filtered,
        found,
        certifications=certs,
        experience_titles=titles,
        years=years,
        limit=None,
    )
    page_size = _page_size(limit)
    start = (page - 1) * page_size
    ranked = ranked_all[start : start + page_size]
    hint = None
    if not pool:
        hint = _empty_cache_hint()
    elif not filtered:
        hint = "No listings matched these filters."
    elif not ranked_all:
        hint = _no_match_hint()
    return {
        "ok": True,
        "skills": found,
        "certifications": certs,
        "experience_titles": titles,
        "years": years,
        **_score_settings(),
        "jobs": _with_time_ago(ranked),
        "count": len(ranked),
        "total": len(ranked_all),
        "limit": page_size,
        "offset": start,
        "pagination": _pagination(len(ranked_all), page_size, start),
        "hint": hint,
        **cache_meta(),
    }


def _candidate_skills_or_http(profile: dict, explicit: list[str] | None = None) -> list[str]:
    found = normalize_skills(explicit or []) or skills_from_profile(profile)
    if not found:
        raise HTTPException(
            status_code=400,
            detail=(
                "This candidate has no skills yet. Send skills or resume_text, "
                "or POST /api/candidates/{candidate_id}/resume."
            ),
        )
    return found


def _require_candidate(candidate_id: str) -> tuple[str, dict]:
    cid = _candidate_id_or_http(candidate_id)
    profile = _stored_candidate(cid)
    if profile is None and mysql_configured():
        try:
            profile = load_app_user(cid)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not load user {cid}: {exc}") from exc
        if profile is not None:
            profile = normalize_profile(profile)
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail=f"No user found for id '{cid}'.",
        )
    return cid, profile


@app.put("/api/candidates/{candidate_id}", summary="Save the registration profile for a candidate API id")
@app.post("/api/candidates/{candidate_id}", summary="Save the registration profile for a candidate API id")
def save_candidate_profile(candidate_id: str, body: CandidateProfile):
    cid = _candidate_id_or_http(candidate_id)
    saved = _store_candidate(cid, body.model_dump())
    return {
        "ok": True,
        "candidate_id": cid,
        "skills": skills_from_profile(saved),
        "certifications": certifications_from_profile(saved),
        "experience_titles": experience_titles_from_profile(saved),
        "years": career_years(saved),
        "profile": saved,
    }


@app.get("/api/candidates/{candidate_id}", summary="Read a saved registration profile")
def get_candidate_profile(candidate_id: str):
    cid, profile = _require_candidate(candidate_id)
    return {
        "ok": True,
        "candidate_id": cid,
        "skills": skills_from_profile(profile),
        "certifications": certifications_from_profile(profile),
        "experience_titles": experience_titles_from_profile(profile),
        "years": career_years(profile),
        "profile": profile,
    }


@app.post(
    "/api/candidates/{candidate_id}/resume",
    summary="Attach the master resume uploaded during registration",
)
async def upload_candidate_resume(
    candidate_id: str,
    file: UploadFile = File(..., description="Master resume: PDF, DOCX, or TXT"),
):
    cid = _candidate_id_or_http(candidate_id)
    data = await file.read()
    parsed = _parse_or_http(data, file.filename or "resume", file.content_type or "")
    existing = _stored_candidate(cid) or {}
    merged = {
        **existing,
        "skills": normalize_skills((existing.get("skills") or []) + (parsed.get("skills") or [])),
        "certifications": normalize_skills(
            (existing.get("certifications") or []) + (parsed.get("certifications") or [])
        ),
        "work_history": existing.get("work_history")
        or [
            {
                "company": item.get("company") or "",
                "job_title": item.get("title") or "",
                "start_date": item.get("start_date") or "",
                "end_date": item.get("end_date") or "",
                "description": " ".join(item.get("bullets") or []),
            }
            for item in parsed.get("experience") or []
        ],
        "resume_text": parsed.get("raw_text") or existing.get("resume_text") or "",
    }
    saved = _store_candidate(cid, merged)
    return {"ok": True, "candidate_id": cid, "skills": skills_from_profile(saved)}


@app.get("/api/recommend/{candidate_id}", summary="Recommend jobs for a saved candidate API id")
def recommend_for_candidate(
    candidate_id: str,
    state: str = Query("All"),
    platform: str = Query("All"),
    days: int = Query(0, description="0 returns every job in the database. 1, 3, or 7 keep a shorter window."),
    city: str = Query(""),
    skills: str = Query("", description="Optional comma-separated skills. Used when the user profile has none."),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    page: int = Query(1, ge=1),
    refresh: bool = Query(False),
):
    cid, profile = _require_candidate(candidate_id)
    found = parse_skill_query(skills) or skills_from_profile(profile)
    certs = certifications_from_profile(profile)
    titles = experience_titles_from_profile(profile)
    if found or certs or titles:
        return _recommend_payload(
            skills=found,
            state=state,
            platform=platform,
            days=days,
            city=city,
            limit=limit,
            refresh=refresh,
            page=page,
            candidate_id=cid,
            profile=profile,
        )
    pool = load_jobs()
    filtered = filter_jobs(pool, [], "All" if not state or state == "All" else state, platform, days, "" if not city else city)
    if profile is not None:
        filtered = filter_jobs_for_candidate(filtered, profile, state=state, city=city)
    filtered.sort(key=lambda row: (_posted_age_seconds(row), row.get("title") or ""))
    start = (page - 1) * limit
    rows = filtered[start : start + limit]
    hint = "No skills are stored for this user, so these are the latest listings."
    if not pool:
        hint = _empty_cache_hint()
    elif not filtered:
        hint = "No listings matched this user's location or visa preferences."
    return {
        "ok": True,
        "candidate_id": cid,
        "skills": [],
        "jobs": _with_time_ago(rows),
        "count": len(rows),
        "total": len(filtered),
        "limit": limit,
        "offset": start,
        "pagination": _pagination(len(filtered), limit, start),
        "filters": applied_filters(profile or {}, state=state, city=city),
        "hint": hint,
        **cache_meta(),
    }


@app.post(
    "/api/recommend",
    summary="Recommend job listings from a candidate id, skills, or a resume",
)
def recommend_from_json(body: RecommendRequest):
    if body.candidate_id:
        cid, profile = _require_candidate(body.candidate_id)
        skills = normalize_skills(body.skills) or skills_from_profile(profile)
        certs = normalize_skills(body.certifications) or certifications_from_profile(profile)
        titles = experience_titles_from_profile(profile)
        if not skills and not certs and not titles:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This candidate has no skills, certifications, or experience yet. "
                    "Send skills or certifications, or POST /api/candidates/{candidate_id}/resume."
                ),
            )
        return _recommend_payload(
            skills=skills,
            certifications=certs,
            experience_titles=titles,
            state=body.state,
            platform=body.platform,
            days=body.days,
            city=body.city,
            limit=body.limit,
            refresh=body.refresh,
            candidate_id=cid,
            profile=profile,
        )
    resume_payload = body.resume.model_dump() if body.resume else None
    skills = normalize_skills(body.skills)
    if not skills and resume_payload:
        skills = normalize_skills(resume_payload.get("skills") or [])
    parsed_text: dict = {}
    if not skills and body.resume_text and body.resume_text.strip():
        try:
            parsed_text = parse_resume_text(body.resume_text)
        except ResumeParseError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        skills = normalize_skills(parsed_text.get("skills") or [])
    certs = normalize_skills(body.certifications)
    source = resume_payload or parsed_text
    if not certs and source:
        certs = normalize_skills(source.get("certifications") or [])
    resume_jobs = list(source.get("experience") or []) if source else []
    titles = [str(item.get("title") or "").strip() for item in resume_jobs if str(item.get("title") or "").strip()]
    if not skills and not certs and not titles:
        raise HTTPException(
            status_code=400,
            detail="Send at least one skill, certification, or a resume that contains them.",
        )
    return _recommend_payload(
        skills=skills,
        certifications=certs,
        experience_titles=titles,
        years=_years_from_resume_jobs(resume_jobs),
        state=body.state,
        platform=body.platform,
        days=body.days,
        city=body.city,
        limit=body.limit,
        refresh=body.refresh,
    )


@app.post(
    "/api/recommend/file",
    summary="Upload a resume and recommend matching job listings",
)
async def recommend_from_file(
    file: UploadFile | None = File(None, description="Optional resume file: PDF, DOCX, or TXT"),
    skills: str = Form("", description="Optional comma-separated skills"),
    state: str = Form("All"),
    platform: str = Form("All"),
    days: int = Form(0),
    city: str = Form(""),
    limit: int = Form(DEFAULT_LIMIT),
    refresh: bool = Form(False),
):
    listed = parse_skill_query(skills)
    resume_payload = None
    if file is not None:
        data = await file.read()
        if data:
            resume_payload = _parse_or_http(data, file.filename or "resume", file.content_type or "")
    if not listed and resume_payload is None:
        raise HTTPException(
            status_code=400,
            detail="Upload a resume file or send skills as comma-separated text.",
        )
    found = list(listed)
    if resume_payload and not found:
        found = normalize_skills(resume_payload.get("skills") or [])
    certs = normalize_skills((resume_payload or {}).get("certifications") or [])
    resume_jobs = list((resume_payload or {}).get("experience") or [])
    titles = [str(item.get("title") or "").strip() for item in resume_jobs if str(item.get("title") or "").strip()]
    if not found and not certs and not titles:
        raise HTTPException(
            status_code=400,
            detail="The resume had no skills, certifications, or job titles to match.",
        )
    return _recommend_payload(
        skills=found,
        certifications=certs,
        experience_titles=titles,
        years=_years_from_resume_jobs(resume_jobs),
        state=state,
        platform=platform,
        days=days,
        city=city,
        limit=limit,
        refresh=refresh,
    )


@app.get("/api/resume")
def resume_contract():
    return {
        "ok": True,
        "service": "resume-parse",
        "max_upload_bytes": 8 * 1024 * 1024,
        "formats": ["pdf", "docx", "txt"],
        "endpoints": {
            "parse_file": {
                "method": "POST",
                "path": "/api/resume/parse",
                "content_type": "multipart/form-data",
                "field": "file",
                "query": {"include_raw": "false"},
            },
            "parse_text": {
                "method": "POST",
                "path": "/api/resume/parse-text",
                "content_type": "application/json",
                "body": {"text": "resume text"},
                "query": {"include_raw": "false"},
            },
            "enhance": {
                "method": "POST",
                "path": "/api/resume/enhance",
                "content_type": "application/json",
                "body": {"user_id": "123", "job_id": "456"},
                "query": {"format": "pdf"},
                "returns": "application/pdf or a Word .docx file download",
            },
        },
        "docs": "/docs",
    }


@app.post(
    "/api/resume/parse",
    response_model=ResumeParseResponse,
    summary="Parse a resume / CV file",
)
async def parse_resume_file(
    request: Request,
    file: UploadFile | None = File(None, description="Resume file: PDF, DOCX, or TXT"),
    include_raw: bool = Query(False, description="Include extracted raw_text in the response"),
):
    data, filename, content_type = await _load_resume_upload(request, file)
    parsed = _parse_or_http(data, filename, content_type)
    return _resume_response(parsed, include_raw)


@app.post(
    "/api/resume/scrape",
    response_model=ResumeParseResponse,
    summary="Alias of /api/resume/parse",
    include_in_schema=False,
)
async def scrape_resume_file(
    request: Request,
    file: UploadFile | None = File(None, description="Resume file: PDF, DOCX, or TXT"),
    include_raw: bool = Query(False),
):
    return await parse_resume_file(request, file, include_raw)


@app.post(
    "/api/resume/parse-text",
    response_model=ResumeParseResponse,
    summary="Parse pasted resume / CV text",
)
def parse_resume_from_text(
    body: ResumeTextRequest,
    include_raw: bool = Query(False, description="Include extracted raw_text in the response"),
):
    try:
        parsed = parse_resume_text(body.text)
    except ResumeParseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _resume_response(parsed, include_raw)


def _job_for_enhance(job_id: str) -> dict:
    ref = (job_id or "").strip()
    if not ref:
        raise HTTPException(status_code=400, detail="job_id is required.")
    if mysql_configured():
        try:
            job = load_job_mysql(ref)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not load job {ref}: {exc}") from exc
        if job:
            return job
    wanted = ref.lower()
    for job in load_jobs_json():
        if str(job.get("id") or "").lower() == wanted or str(job.get("job_id") or "") == wanted:
            return job
    raise HTTPException(status_code=404, detail=f"No job found for id '{ref}'.")


def _enhanced_filename(profile: dict, job: dict, extension: str, cv_text: str = "") -> str:
    who = ""
    for line in (cv_text or "").splitlines()[:4]:
        cleaned = line.strip()
        if cleaned and len(cleaned.split()) <= 5 and "@" not in cleaned and not cleaned.isupper():
            who = cleaned
            break
    if not who:
        who = " ".join(part for part in (profile.get("first_name"), profile.get("last_name")) if part).strip() or "resume"
    role = str(job.get("title") or "job")
    stem = re.sub(r"[^\w.\-]+", "_", f"{who}_{role}").strip("._")[:80] or "resume"
    return f"{stem}_enhanced.{extension}"


@app.post(
    "/api/resume/enhance",
    summary="Download the user's resume, kept intact and aimed at one job",
    response_class=Response,
)
def enhance_user_resume(
    body: ResumeEnhanceRequest,
    file_format: str = Query("pdf", alias="format", description="Download format: pdf or docx"),
):
    """Keeps every section of the saved resume and aims that same content at the job."""
    fmt = (file_format or "pdf").strip().lower()
    if fmt not in {"pdf", "docx"}:
        raise HTTPException(status_code=400, detail="format must be pdf or docx.")
    _cid, profile = _require_candidate(body.user_id.strip())
    try:
        stored = load_resume_file(body.user_id.strip())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not load resume file: {exc}") from exc
    if stored and stored.get("text"):
        existing = str(profile.get("resume_document") or "")
        if len(str(stored.get("text") or "")) >= len(existing):
            profile = {**profile, "resume_document": stored.get("text")}
    job = _job_for_enhance(body.job_id)
    try:
        built = enhance_profile_for_job(profile, job)
        cv_text = built.get("cv_text") or ""
        if fmt == "docx":
            content = cv_text_to_docx(cv_text)
            media = DOCX_TYPE
        else:
            content = cv_text_to_pdf(cv_text)
            media = PDF_TYPE
    except (JobToCvError, ResumeParseError, ConvertError) as exc:
        status = getattr(exc, "status_code", 400)
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Resume enhance failed: {exc}") from exc
    return _file_download(content, _enhanced_filename(profile, job, fmt, cv_text), media)


class ProfileJob(BaseModel):
    company: str = ""
    job_title: str = ""
    start_date: str = ""
    end_date: str = ""
    responsibilities: str = ""


class ProfileSchool(BaseModel):
    school: str = ""
    degree: str = ""
    field_of_study: str = ""
    graduation_year: str = ""
    grade: str = ""


class ProfileCertification(BaseModel):
    name: str = ""
    issuer: str = ""
    issue_date: str = ""


class ProfilePersonal(BaseModel):
    first_name: str = ""
    last_name: str = ""
    preferred_name: str = ""
    email: str = ""
    phone_country_code: str = ""
    phone_number: str = ""
    nationality: str = ""


class ProfileAddress(BaseModel):
    building_number: str = ""
    street: str = ""
    district: str = ""
    city: str = ""
    region: str = ""
    postal_code: str = ""
    additional_number: str = ""
    short_address: str = ""
    country: str = ""


class ProfileLinks(BaseModel):
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    personal_website: str = ""


class ProfileWorkAuthorization(BaseModel):
    authorized_to_work: str = ""
    need_iqama_or_work_visa: str = ""
    work_authorization_type: str = ""
    work_authorization_validity: str = ""


class ProfileLogistics(BaseModel):
    desired_monthly_salary_sar: str = ""
    notice_period: str = ""
    willing_to_relocate: str = ""
    open_to_remote: str = ""
    meets_minimum_working_age: str = ""
    how_heard: str = ""


class ProfileOptionalIds(BaseModel):
    gender: str = ""
    disability_status: str = ""


class ProfileCustomAnswer(BaseModel):
    question: str = ""
    answer: str = ""


class ProfileAutofillResponse(BaseModel):
    ok: bool = True
    source: ResumeSource
    executive_summary: str = ""
    work_experience: list[ProfileJob] = Field(default_factory=list)
    education: list[ProfileSchool] = Field(default_factory=list)
    certifications: list[ProfileCertification] = Field(default_factory=list)
    skills: str = ""
    personal: ProfilePersonal
    national_address: ProfileAddress
    online_profiles: ProfileLinks
    work_authorization: ProfileWorkAuthorization
    logistics: ProfileLogistics
    optional_identifiers: ProfileOptionalIds
    custom_answers: list[ProfileCustomAnswer] = Field(default_factory=list)


def _autofill_or_http(parsed: dict) -> dict:
    filled = autofill_from_parsed(parsed)
    source = filled.get("source") or {}
    if not source.get("content_type"):
        source["content_type"] = "text/plain"
    filled["source"] = source
    return filled


@app.get("/api/profile/autofill", summary="Profile autofill contract")
def profile_autofill_contract():
    return {
        "ok": True,
        "service": "profile-autofill",
        "note": "Separate from /api/resume/parse. Fields missing from the resume are empty strings.",
        "max_upload_bytes": 8 * 1024 * 1024,
        "formats": ["pdf", "docx", "txt"],
        "endpoints": {
            "upload": {
                "method": "POST",
                "path": "/api/profile/autofill",
                "content_type": "multipart/form-data",
                "field": "file",
            },
            "text": {
                "method": "POST",
                "path": "/api/profile/autofill-text",
                "content_type": "application/json",
                "body": {"text": "resume text"},
            },
            "linkedin": {
                "method": "POST",
                "path": "/api/profile/linkedin",
                "content_type": "application/json",
                "body": {"url": "https://www.linkedin.com/in/username"},
            },
        },
        "docs": "/docs",
    }


@app.post(
    "/api/profile/autofill",
    response_model=ProfileAutofillResponse,
    summary="Upload a resume and fill the profile form",
)
async def profile_autofill_file(
    request: Request,
    file: UploadFile | None = File(None, description="Resume file: PDF, DOCX, or TXT"),
):
    data, filename, content_type = await _load_resume_upload(request, file)
    parsed = _parse_or_http(data, filename, content_type)
    return _autofill_or_http(parsed)


@app.post(
    "/api/profile/autofill-text",
    response_model=ProfileAutofillResponse,
    summary="Fill the profile form from pasted resume text",
)
def profile_autofill_from_text(body: ResumeTextRequest):
    try:
        parsed = parse_resume_text(body.text)
    except ResumeParseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _autofill_or_http(parsed)


@app.post(
    "/api/profile/linkedin",
    response_model=ProfileAutofillResponse,
    summary="Fill the profile form from a LinkedIn profile link",
)
async def profile_from_linkedin_link(request: Request):
    """Same fields as the resume upload. Missing public fields are empty strings."""
    content_type = (request.headers.get("content-type") or "").lower()
    target = ""
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Send JSON with a url field.") from exc
        if isinstance(body, dict):
            target = str(body.get("url") or body.get("link") or "")
    else:
        form = await request.form()
        target = str(form.get("url") or form.get("link") or "")
    try:
        return autofill_from_linkedin(target)
    except LinkedInProfileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _job_from_cache(job_id: str | None) -> dict | None:
    if not job_id:
        return None
    wanted = job_id.strip().lower()
    for job in load_jobs():
        if str(job.get("id") or "").lower() == wanted or str(job.get("job_id") or "") == wanted:
            return job
    raise HTTPException(status_code=404, detail=f"No scraped job found for id '{job_id}'.")


def _ats_or_http(
    *,
    resume_text: str | None = None,
    resume: dict | None = None,
    job_description: str = "",
    job_title: str | None = None,
    company: str | None = None,
    location: str | None = None,
    job_id: str | None = None,
) -> dict:
    cached = _job_from_cache(job_id)
    extra_skills = None
    if cached:
        job_title = job_title or cached.get("title")
        company = company or cached.get("company")
        location = location or cached.get("location")
        extra_skills = list(cached.get("skills") or []) or None
        if not (job_description or "").strip() and extra_skills:
            job_description = (
                f"{cached.get('title') or 'Open role'}\n"
                f"Company: {cached.get('company') or ''}\n"
                f"Location: {cached.get('location') or ''}\n"
                f"Required skills: {', '.join(extra_skills)}"
            )
    try:
        return score_resume(
            resume_text=resume_text,
            resume=resume,
            job_description=job_description,
            job_title=job_title,
            company=company,
            location=location,
            job_skills=extra_skills,
        )
    except (AtsScoreError, JobToCvError, ResumeParseError) as exc:
        status = getattr(exc, "status_code", 400)
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.get("/api/ats")
def ats_contract():
    return {
        "ok": True,
        "service": "ats-score",
        "max_upload_bytes": 8 * 1024 * 1024,
        "formats": ["pdf", "docx", "txt"],
        "score": "0-100",
        "grades": ["A", "B", "C", "D", "F"],
        "endpoints": {
            "score": {
                "method": "POST",
                "path": "/api/ats/score",
                "content_type": "application/json",
                "body": {
                    "resume_text": "resume text",
                    "resume": "optional parsed resume JSON",
                    "job_description": "optional job posting text",
                    "job_id": "optional scraped job id",
                },
            },
            "score_file": {
                "method": "POST",
                "path": "/api/ats/score-file",
                "content_type": "multipart/form-data",
                "fields": {
                    "file": "resume PDF/DOCX/TXT",
                    "job_description": "optional job posting text",
                    "job_id": "optional scraped job id",
                },
            },
        },
        "docs": "/docs",
    }


@app.post(
    "/api/ats/score",
    response_model=AtsScoreResponse,
    summary="Get an ATS score from resume text or parsed JSON",
)
def ats_score_from_json(body: AtsScoreRequest):
    resume_payload = body.resume.model_dump() if body.resume else None
    return _ats_or_http(
        resume_text=body.resume_text,
        resume=resume_payload,
        job_description=body.job_description,
        job_title=body.job_title,
        company=body.company,
        location=body.location,
        job_id=body.job_id,
    )


@app.post(
    "/api/ats/score-file",
    response_model=AtsScoreResponse,
    summary="Upload a resume and get an ATS score",
)
async def ats_score_from_file(
    request: Request,
    file: UploadFile | None = File(None, description="Resume file: PDF, DOCX, or TXT"),
    job_description: str = Form("", description="Optional job posting text"),
    job_title: str | None = Form(None),
    company: str | None = Form(None),
    location: str | None = Form(None),
    job_id: str | None = Form(None),
):
    data, filename, content_type = await _load_resume_upload(request, file)
    parsed = _parse_or_http(data, filename, content_type)
    return _ats_or_http(
        resume=parsed,
        job_description=job_description,
        job_title=job_title,
        company=company,
        location=location,
        job_id=job_id,
    )


def _build_cv_or_http(
    *,
    job_description: str = "",
    job_title: str | None = None,
    company: str | None = None,
    location: str | None = None,
    job_id: str | None = None,
    resume_text: str | None = None,
    resume: dict | None = None,
    include_cv_text: bool = True,
) -> dict:
    cached = _job_from_cache(job_id)
    extra_skills = None
    if cached:
        job_title = job_title or cached.get("title")
        company = company or cached.get("company")
        location = location or cached.get("location")
        extra_skills = list(cached.get("skills") or []) or None
        if not (job_description or "").strip() and extra_skills:
            job_description = (
                f"{cached.get('title') or 'Open role'}\n"
                f"Company: {cached.get('company') or ''}\n"
                f"Location: {cached.get('location') or ''}\n"
                f"Required skills: {', '.join(extra_skills)}"
            )
    try:
        return build_cv_from_job(
            job_description,
            job_title=job_title,
            company=company,
            location=location,
            job_skills=extra_skills,
            resume_text=resume_text,
            resume=resume,
            include_cv_text=include_cv_text,
        )
    except (JobToCvError, ResumeParseError) as exc:
        status = getattr(exc, "status_code", 400)
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.get("/api/cv")
def cv_contract():
    return {
        "ok": True,
        "service": "cv-from-job",
        "modes": ["tailored", "template"],
        "endpoints": {
            "from_job": {
                "method": "POST",
                "path": "/api/cv/from-job",
                "content_type": "application/json",
                "body": {
                    "job_description": "job posting text",
                    "job_title": "optional",
                    "company": "optional",
                    "job_id": "optional scraped job id",
                    "resume_text": "optional resume text",
                    "resume": "optional parsed resume JSON",
                },
                "query": {"include_cv_text": "true"},
            },
            "from_job_file": {
                "method": "POST",
                "path": "/api/cv/from-job-file",
                "content_type": "multipart/form-data",
                "fields": {
                    "file": "optional resume PDF/DOCX/TXT",
                    "job_description": "job posting text",
                    "job_title": "optional",
                    "company": "optional",
                    "job_id": "optional",
                },
                "query": {"include_cv_text": "true"},
            },
        },
        "docs": "/docs",
    }


@app.post(
    "/api/cv/from-job",
    response_model=CvFromJobResponse,
    summary="Build or tailor a CV from a job description",
)
def cv_from_job(
    body: CvFromJobRequest,
    include_cv_text: bool = Query(True, description="Include formatted cv_text for display or download"),
):
    resume_payload = body.resume.model_dump() if body.resume else None
    return _build_cv_or_http(
        job_description=body.job_description,
        job_title=body.job_title,
        company=body.company,
        location=body.location,
        job_id=body.job_id,
        resume_text=body.resume_text,
        resume=resume_payload,
        include_cv_text=include_cv_text,
    )


@app.get("/api/convert")
def convert_contract():
    return {
        "ok": True,
        "service": "pdf-docx-convert",
        "max_upload_bytes": 8 * 1024 * 1024,
        "formats": {
            "pdf_to_docx": ["pdf"],
            "docx_to_pdf": ["docx"],
        },
        "notes": [
            "Default response is a file download, not JSON.",
            "In Postman, PDF→DOCX looks like XML in the Body tab. Use Send and Download, or add ?preview=true.",
            "Text-based conversion. Images, columns, and exact layout are not preserved.",
            "Image-only / scanned PDFs fail because there is no OCR.",
            "Old .doc is not supported.",
        ],
        "endpoints": {
            "pdf_to_docx": {
                "method": "POST",
                "path": "/api/convert/pdf-to-docx",
                "content_type": "multipart/form-data",
                "field": "file",
                "query": {"preview": "false"},
                "returns": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            },
            "docx_to_pdf": {
                "method": "POST",
                "path": "/api/convert/docx-to-pdf",
                "content_type": "multipart/form-data",
                "field": "file",
                "query": {"preview": "false"},
                "returns": "application/pdf",
            },
        },
        "docs": "/docs",
    }


@app.post(
    "/api/convert/pdf-to-docx",
    summary="Convert a PDF file to DOCX",
    response_class=Response,
)
async def convert_pdf_to_docx(
    request: Request,
    file: UploadFile | None = File(None, description="PDF file to convert"),
    preview: bool = Query(False, description="Return HTML text preview instead of a .docx file"),
):
    data, filename, _content_type = await _load_resume_upload(request, file)
    try:
        converted = pdf_to_docx(data, filename)
    except ConvertError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF to DOCX failed: {exc}") from exc
    out_name = _swap_extension(filename, ".docx")
    if preview:
        return _html_preview(out_name, preview_text_from_docx(converted))
    return _file_download(converted, out_name, DOCX_TYPE)


@app.post(
    "/api/convert/docx-to-pdf",
    summary="Convert a DOCX file to PDF",
    response_class=Response,
)
async def convert_docx_to_pdf(
    request: Request,
    file: UploadFile | None = File(None, description="Word .docx file to convert"),
    preview: bool = Query(False, description="Return HTML text preview instead of a .pdf file"),
):
    data, filename, _content_type = await _load_resume_upload(request, file)
    try:
        converted = docx_to_pdf(data, filename)
    except ConvertError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DOCX to PDF failed: {exc}") from exc
    out_name = _swap_extension(filename, ".pdf")
    if preview:
        return _html_preview(out_name, preview_text_from_docx(data))
    return _file_download(converted, out_name, PDF_TYPE)


@app.post(
    "/api/cv/from-job-file",
    response_model=CvFromJobResponse,
    summary="Upload a resume and tailor it to a job description",
)
async def cv_from_job_file(
    file: UploadFile | None = File(None, description="Optional resume file: PDF, DOCX, or TXT"),
    job_description: str = Form("", description="Full job posting text"),
    job_title: str | None = Form(None),
    company: str | None = Form(None),
    location: str | None = Form(None),
    job_id: str | None = Form(None),
    include_cv_text: bool = Query(True, description="Include formatted cv_text for display or download"),
):
    resume_payload = None
    if file is not None:
        data = await file.read()
        if data:
            resume_payload = _parse_or_http(data, file.filename or "resume", file.content_type or "")

    return _build_cv_or_http(
        job_description=job_description,
        job_title=job_title,
        company=company,
        location=location,
        job_id=job_id,
        resume=resume_payload,
        include_cv_text=include_cv_text,
    )


if __name__ == "__main__":
    import os
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    reload = os.environ.get("RELOAD", "1") == "1"
    uvicorn.run("api:app", host=host, port=port, reload=reload)
