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
from jobscraper.candidates import (
    CandidateError,
    clean_candidate_id,
    filter_jobs_for_candidate,
    load_candidate_file,
    normalize_profile,
    save_candidate_file,
    skills_from_profile,
    applied_filters,
)
from jobscraper.document_convert import (
    ConvertError,
    DOCX_TYPE,
    PDF_TYPE,
    docx_to_pdf,
    pdf_to_docx,
    preview_text_from_docx,
)
from jobscraper.jd_to_cv import JobToCvError, build_cv_from_job
from jobscraper.job_fields import (
    EXPERIENCE_LEVELS,
    WORK_MODELS,
    apply_listing_filters,
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
    normalize_skills,
    parse_skill_query,
    recommend_jobs,
)
from jobscraper.resume_parser import ResumeParseError, parse_resume_bytes, parse_resume_text
from jobscraper.db import (
    close_conn as close_mysql,
    finish_scrape_run,
    health as mysql_health,
    import_json_if_empty,
    init_db as init_mysql,
    job_count as mysql_job_count,
    last_scrape_meta,
    load_app_user,
    load_candidate as load_candidate_mysql,
    attach_descriptions,
    attach_raw_descriptions,
    load_jobs as load_jobs_mysql,
    mysql_configured,
    save_candidate as save_candidate_mysql,
    start_scrape_run,
    upsert_jobs,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DATA_FILE = ROOT / "data" / "jobs.json"
INDEX_FILE = ROOT / "index.html"
DASHBOARD_FILE = ROOT / "dashboard.html"
SCRAPE_LOCK = threading.Lock()
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


def scheduled_scrape() -> None:
    if not SCRAPE_LOCK.acquire(blocking=False):
        log.warning("Scheduled scrape skipped: another scrape is running")
        return
    try:
        keywords = _env("SCRAPE_KEYWORDS", "")
        state = _env("SCRAPE_STATE", "All") or "All"
        platform = _env("SCRAPE_PLATFORM", "All") or "All"
        city = _env("SCRAPE_CITY", "")
        log.info("Scheduled scrape starting keywords=%s", keywords)
        run_spider(keywords, state, platform, city)
        log.info("Scheduled scrape finished")
    except Exception:
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
    scheduler.start()
    log.info("Scrape schedule: %s:00 %s", hours, tz_name)
    return scheduler


def _boot() -> None:
    """MySQL setup stays off the accept path. IIS returns 502 until this port is open."""
    global _SCHEDULER
    try:
        init_mysql()
        import_json_if_empty(DATA_FILE)
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
    description="Job listings API, skill-based job recommendations, resume/CV parse, ATS resume score, job-description-to-CV, PDF/DOCX conversion, and MySQL job storage.",
    version="1.6.0",
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


class RecommendRequest(BaseModel):
    candidate_id: str | None = Field(
        None,
        description="Candidate API id from registration. Loads the saved profile.",
    )
    skills: list[str] = Field(default_factory=list, description="Candidate skills, e.g. Python, FastAPI, AWS")
    resume_text: str | None = Field(None, description="Optional raw resume text; skills are extracted from it")
    resume: ResumeInput | None = Field(None, description="Optional parsed resume JSON from /api/resume/parse")
    state: str = Field("All", description="US state filter, Remote, or All")
    platform: str = Field("All", description="Job board name, or All")
    days: int = Field(30, description="Only listings posted within this many days")
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


def load_jobs() -> list[dict]:
    if mysql_configured():
        rows = load_jobs_mysql()
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


def format_time_ago(posted_at, fallback_days: int = 0) -> str:
    """Relative label for the job card: '2 min ago', '1 hour ago', '7 days ago', '1 month ago'."""
    posted = _as_utc(posted_at)
    if posted is None:
        return _ago_from_days(fallback_days)
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
        row["daysAgo"] = format_time_ago(job.get("posted_at"), fallback)
        presented.append(row)
    attach_descriptions(presented)
    if len(presented) <= 200:
        attach_raw_descriptions(presented)
    return presented


def filter_jobs(
    jobs: list[dict],
    keywords: list[str],
    state: str,
    platform: str,
    days: int,
    city: str = "",
    ksa_only: bool = True,
) -> list[dict]:
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
        if int(job.get("daysAgo") or 0) > days:
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
) -> dict:
    if refresh:
        if not SCRAPE_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A scrape is already running. Try again in a moment.")
        try:
            run_spider(",".join(skills), state, platform, city)
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=504, detail="Job scrape timed out.") from exc
        finally:
            SCRAPE_LOCK.release()
    list_state = "All" if profile is not None and (not state or state == "All") else state
    list_city = "" if profile is not None and not city else city
    pool = load_jobs()
    filtered = filter_jobs(pool, [], list_state, platform, days, list_city)
    if profile is not None:
        filtered = filter_jobs_for_candidate(filtered, profile, state=state, city=city)
    ranked_all = recommend_jobs(filtered, skills, limit=MAX_LIMIT)
    page_num = max(page, 1)
    start = (page_num - 1) * limit
    ranked = ranked_all[start : start + limit]
    hint = None
    if not pool:
        hint = _empty_cache_hint()
    elif not filtered and profile is not None:
        hint = "No listings matched this candidate's location, remote, or visa preferences."
    elif not filtered:
        hint = _empty_cache_hint()
    elif not ranked_all:
        hint = "Listings were found, but none overlapped these skills. Try refresh=true or broader skills."
    payload = {
        "ok": True,
        "count": len(ranked),
        "total": len(ranked_all),
        "skills": skills,
        "jobs": _with_time_ago(ranked),
        "pagination": _pagination(len(ranked_all), limit, start),
        "hint": hint,
        **cache_meta(),
    }
    if candidate_id:
        payload["candidate_id"] = candidate_id
    if profile is not None:
        payload["filters"] = applied_filters(profile, state=state, city=city)
    return payload


def run_spider(keywords: str, location: str, platform: str, city: str = "", target: int = 0) -> None:
    DATA_FILE.parent.mkdir(exist_ok=True)
    if DATA_FILE.exists():
        DATA_FILE.unlink()
    run_id = start_scrape_run(keywords, location, platform)
    cmd = [
        sys.executable,
        "-m",
        "scrapy",
        "crawl",
        "jobs",
        "-a",
        f"keywords={keywords}",
        "-a",
        f"location={location}",
        "-a",
        f"city={city}",
        "-a",
        f"platform={platform}",
        "-a",
        f"target={max(0, int(target or 0))}",
        "-s",
        "LOG_LEVEL=WARNING",
    ]
    # A 5,000-job run searches about 40 roles on 4 boards and can take one to two hours.
    timeout = 10800 if target >= 1000 else SCRAPE_TIMEOUT
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
        finish_scrape_run(run_id, job_count_value=0, error="timeout")
        raise
    except Exception as exc:
        finish_scrape_run(run_id, job_count_value=0, error=str(exc)[:2000])
        raise
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Scrapy crawl failed").strip()
        finish_scrape_run(run_id, job_count_value=0, error=detail[-2000:])
        raise HTTPException(status_code=502, detail=detail[-2000:])
    this_run = load_jobs_json()
    try:
        saved = upsert_jobs(this_run)
    except Exception as exc:
        detail = f"MySQL save failed: {exc}"
        finish_scrape_run(run_id, job_count_value=0, error=detail[:2000])
        raise HTTPException(status_code=502, detail=detail[:2000]) from exc
    finish_scrape_run(run_id, job_count_value=len(this_run) or saved)


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


@app.get("/api/health")
def health():
    mysql = mysql_health()
    return {
        "ok": True,
        "service": "job-discovery",
        "jobs_list": True,
        "jobs_recommend": True,
        "country": "Saudi Arabia",
        "save_fix": 4,
        "resume_parse": True,
        "ats_score": True,
        "cv_from_job": True,
        "pdf_docx_convert": True,
        "resume_formats": ["pdf", "docx", "txt"],
        "mysql": mysql,
        "scrape_schedule": {
            "enabled": _truthy("SCRAPE_SCHEDULE_ENABLED", "true"),
            "tz": _env("SCRAPE_TZ", "Asia/Riyadh") or "Asia/Riyadh",
            "hours": _schedule_hours(),
        },
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
        ],
        "days": [1, 3, 7, 30],
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


@app.get("/api/jobs")
def get_jobs(
    keywords: str = Query("", description="Comma-separated title/company/skill keywords"),
    state: str = Query("All"),
    platform: str = Query("All"),
    days: int = Query(30),
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
    if not SCRAPE_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A scrape is already running. Try again in a moment.")
    thread = threading.Thread(
        target=_scrape_background,
        args=(keywords, state, platform, city, target),
        name="job-scrape",
        daemon=True,
    )
    thread.start()
    message = "Scrape started. Poll GET /api/scrape/status until running is false, then check the ksajob.jobs table."
    if target >= 1000:
        message = (
            f"Large scrape started for about {target} jobs across many fields. "
            "It can take one to two hours. Poll GET /api/scrape/status until running is false."
        )
    return JSONResponse(
        status_code=202,
        content={
            "ok": True,
            "started": True,
            "running": True,
            "target": target,
            "message": message,
        },
    )


def _scrape_background(keywords: str, state: str, platform: str, city: str, target: int = 0) -> None:
    global _LAST_SCRAPE
    try:
        run_spider(keywords, state, platform, city, target)
        jobs = [
            job
            for job in load_jobs_json()
            if is_ksa_job(
                job.get("state") or "",
                job.get("location") or "",
                allow_bare_remote=(job.get("platform") or "") in KSA_SCOPED_PLATFORMS,
            )
        ]
        _LAST_SCRAPE = {
            "ok": True,
            "error": None,
            "count": len(jobs),
            "db_total": mysql_job_count() if mysql_configured() else len(jobs),
        }
        log.info("Scrape saved %s jobs (db_total=%s)", len(jobs), _LAST_SCRAPE["db_total"])
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        _LAST_SCRAPE = {"ok": False, "error": str(detail)[:500], "count": 0, "db_total": None}
        log.exception("Background scrape failed")
    finally:
        SCRAPE_LOCK.release()


@app.get("/api/scrape/status")
def scrape_status():
    return {
        "ok": True,
        "running": SCRAPE_LOCK.locked(),
        "last_scrape": _LAST_SCRAPE,
        "mysql": mysql_health(),
        "schedule": {
            "enabled": _truthy("SCRAPE_SCHEDULE_ENABLED", "true"),
            "tz": _env("SCRAPE_TZ", "Asia/Riyadh") or "Asia/Riyadh",
            "hours": _schedule_hours(),
            "keywords": _env("SCRAPE_KEYWORDS", ""),
            "state": _env("SCRAPE_STATE", "All") or "All",
            "platform": _env("SCRAPE_PLATFORM", "All") or "All",
        },
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
                    "days": 30,
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
                "example": "/api/recommend/roles?skills=Python,FastAPI,AWS&state=All&platform=All&days=30&limit=25&page=1",
                "query": {
                    "skills": "Python,FastAPI,AWS",
                    "state": "All",
                    "platform": "All",
                    "days": 30,
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
                    "days": 30,
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
                    "days": 30,
                    "limit": 25,
                    "refresh": False,
                },
            },
        },
        "docs": "/docs",
    }


@app.get("/api/recommend/roles", summary="Recommended Roles tab: rank listings by skills")
def recommend_roles(
    skills: str = Query(..., description="Comma-separated skills, for example Python,FastAPI,AWS"),
    state: str = Query("All"),
    platform: str = Query("All"),
    days: int = Query(30),
    city: str = Query(""),
    work_model: str = Query("", description="Comma-separated: remote, hybrid, onsite"),
    job_type: str = Query("", description="Comma-separated: fulltime, contract, parttime, internship"),
    experience_level: str = Query(""),
    visa_sponsorship: str = Query("", description="yes or no"),
    clearance_required: str = Query("", description="yes or no"),
    saudi_national_required: str = Query("", description="yes or no"),
    years: int | None = Query(None, ge=0, le=40),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    page: int = Query(1, ge=1),
):
    found = parse_skill_query(skills)
    if not found:
        raise HTTPException(
            status_code=400,
            detail="Send skills as comma-separated text, for example Python,FastAPI,AWS.",
        )
    pool = load_jobs()
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
    ranked_all = recommend_jobs(filtered, found, limit=MAX_LIMIT)
    start = (page - 1) * limit
    ranked = ranked_all[start : start + limit]
    hint = None
    if not pool:
        hint = _empty_cache_hint()
    elif not filtered:
        hint = "No listings matched these filters."
    elif not ranked_all:
        hint = "Listings were found, but none overlapped these skills."
    return {
        "ok": True,
        "skills": found,
        "jobs": _with_time_ago(ranked),
        "count": len(ranked),
        "total": len(ranked_all),
        "limit": limit,
        "offset": start,
        "pagination": _pagination(len(ranked_all), limit, start),
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
        "profile": saved,
    }


@app.get("/api/candidates/{candidate_id}", summary="Read a saved registration profile")
def get_candidate_profile(candidate_id: str):
    cid, profile = _require_candidate(candidate_id)
    return {
        "ok": True,
        "candidate_id": cid,
        "skills": skills_from_profile(profile),
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
        "resume_text": parsed.get("raw_text") or existing.get("resume_text") or "",
    }
    saved = _store_candidate(cid, merged)
    return {"ok": True, "candidate_id": cid, "skills": skills_from_profile(saved)}


@app.get("/api/recommend/{candidate_id}", summary="Recommend jobs for a saved candidate API id")
def recommend_for_candidate(
    candidate_id: str,
    state: str = Query("All"),
    platform: str = Query("All"),
    days: int = Query(30),
    city: str = Query(""),
    skills: str = Query("", description="Optional comma-separated skills. Used when the user profile has none."),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    page: int = Query(1, ge=1),
    refresh: bool = Query(False),
):
    cid, profile = _require_candidate(candidate_id)
    found = parse_skill_query(skills) or skills_from_profile(profile)
    if found:
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
        skills = _candidate_skills_or_http(profile, body.skills)
        return _recommend_payload(
            skills=skills,
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
    skills = _skills_from_inputs(body.skills, body.resume_text, resume_payload)
    return _recommend_payload(
        skills=skills,
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
    days: int = Form(30),
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
    found = _skills_from_inputs(listed, None, resume_payload)
    return _recommend_payload(
        skills=found,
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
