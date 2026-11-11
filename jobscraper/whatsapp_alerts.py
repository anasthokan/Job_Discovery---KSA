"""WhatsApp alerts for newly scraped jobs.

Every ingest path saves through upsert_jobs. That function queues only rows
that were not already stored. A short debounce then posts each new job to the
Jobs in Saudi group, and scores those rows with the same matcher as
/api/recommend to post one message per matching job to that person.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from jobscraper.candidates import (
    career_years,
    certifications_from_profile,
    experience_titles_from_profile,
    filter_jobs_for_candidate,
    skills_from_profile,
)
from jobscraper.profile_autofill import _DIAL_CODES
from jobscraper.recommend import score_job

log = logging.getLogger("jobscraper.whatsapp")

ALERTS_TABLE = "job_whatsapp_alerts"
DEFAULT_SEND_URL = "http://74.208.184.175:8000/sessions/Intellirestro/send-message"
DEFAULT_GROUP_URL = "http://74.208.184.175:8000/sessions/Intellirestro/send-message-to-group"
DEFAULT_GROUP_ID = "966563226748-1448363692@g.us"
DEFAULT_GROUP_NAME = "Jobs in Saudi"
DEFAULT_API_KEY = "my-key"
CREATE_ALERTS_SQL = f"""
CREATE TABLE IF NOT EXISTS {ALERTS_TABLE} (
  id BIGINT NOT NULL AUTO_INCREMENT,
  phone VARCHAR(64) NOT NULL,
  source_id VARCHAR(255) NOT NULL,
  user_id VARCHAR(64) NULL,
  title VARCHAR(512) NULL,
  company VARCHAR(512) NULL,
  url VARCHAR(1024) NULL,
  platform VARCHAR(64) NULL,
  score INT NULL,
  match_json JSON NULL,
  message TEXT NULL,
  sent_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_whatsapp_alert (phone, source_id),
  KEY idx_whatsapp_alert_sent (phone, sent_at),
  KEY idx_whatsapp_alert_user (user_id, sent_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""
_ALERT_COLUMNS = {
    "user_id": "VARCHAR(64) NULL",
    "title": "VARCHAR(512) NULL",
    "company": "VARCHAR(512) NULL",
    "url": "VARCHAR(1024) NULL",
    "platform": "VARCHAR(64) NULL",
    "score": "INT NULL",
    "match_json": "JSON NULL",
    "message": "TEXT NULL",
}

_DIALS = tuple(sorted(set(_DIAL_CODES), key=len, reverse=True))
_LOCK = threading.Lock()
_DELIVER_LOCK = threading.Lock()
_pending: list[dict] = []
_inflight: set[str] = set()
_timer: threading.Timer | None = None
_recipients: dict = {"at": 0.0, "rows": []}
_RECIPIENT_TTL = 600
_group_backoff_until = 0.0
_SWEEP_STARTED = False
_FIRST_SEEN_INDEXED = False
_SWEEP_STATUS = {
    "at": None,
    "found": 0,
    "queued": 0,
    "skipped": 0,
    "error": None,
    "skipped_titles": [],
}
# Group posts only jobs scraped after this process started. Older rows stay in
# the database and are not announced.
_GROUP_NOT_BEFORE = datetime.now(timezone.utc).replace(tzinfo=None)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def alerts_enabled() -> bool:
    return _env("WHATSAPP_ALERTS", "true").lower() in {"1", "true", "yes", "on"}


def group_alerts_enabled() -> bool:
    if not alerts_enabled():
        return False
    return _env("WHATSAPP_GROUP_ALERTS", "true").lower() in {"1", "true", "yes", "on"}


def _api_key() -> str:
    return _env("WHATSAPP_API_KEY", DEFAULT_API_KEY)


def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


def _has_dial(digits: str) -> bool:
    """True when digits already include a country code.

    10-digit nationals stay national. Otherwise 81 (Japan) swallows an Indian
    mobile such as 8149951779 and the real country code is never applied.
    """
    if len(digits) < 11:
        return False
    for dial in _DIALS:
        rest = len(digits) - len(dial)
        if digits.startswith(dial) and 6 <= rest <= 12:
            return True
    return False


def whatsapp_number(raw: str, code: str = "") -> str | None:
    """Digits with country code, no plus. Local Saudi mobiles become 966..."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    code_digits = re.sub(r"\D", "", code or "")
    if code_digits and not _has_dial(digits):
        national = digits[1:] if digits.startswith("0") else digits
        if national:
            digits = code_digits + national
    if not _has_dial(digits):
        if len(digits) == 10 and digits.startswith("05"):
            digits = "966" + digits[1:]
        elif len(digits) == 9 and digits.startswith("5"):
            digits = "966" + digits
    if _has_dial(digits) and 10 <= len(digits) <= 15:
        return digits
    return None


def _skills(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "ignore")
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return []


def _alert_job(row: dict) -> dict:
    description = str(row.get("description") or "")
    return {
        "source_id": str(row.get("source_id") or ""),
        "title": str(row.get("title") or ""),
        "company": str(row.get("company") or ""),
        "skills": _skills(row.get("skills")),
        "state": str(row.get("state") or ""),
        "location": str(row.get("location") or ""),
        "platform": str(row.get("platform") or ""),
        "url": str(row.get("url") or ""),
        "company_career_url": str(row.get("company_career_url") or ""),
        "description": description[:8000],
        "job_type": row.get("job_type") or "fulltime",
        "work_model": row.get("work_model") or "unknown",
        "experience_level": row.get("experience_level") or "unknown",
        "years_experience": row.get("years_experience"),
        "visa_sponsorship": row.get("visa_sponsorship") or "unknown",
        "saudi_national_required": row.get("saudi_national_required") or "no",
        "posted_at": row.get("posted_at"),
        "platform_posted_at": row.get("platform_posted_at"),
        "_attempts": int(row.get("_attempts") or 0),
        "_group": bool(row.get("_group", True)),
    }


def group_sweep_status() -> dict:
    """Last group sweep, so a restart can be checked without the server log."""
    return dict(_SWEEP_STATUS)


def queue_job_alerts(rows: list[dict], *, group: bool | None = None, immediate: bool = False) -> None:
    """Hold brand-new jobs briefly so one scrape wave becomes one matching pass."""
    if not alerts_enabled() or not rows:
        return
    prepared = []
    seen: set[str] = set()
    for row in rows:
        job = _alert_job(row)
        if not job["source_id"] or not job["title"] or job["source_id"] in seen:
            continue
        if not job_is_alertable(job):
            continue
        if group is not None:
            job["_group"] = group
        seen.add(job["source_id"])
        prepared.append(job)
    if not prepared:
        return
    with _LOCK:
        known = {item["source_id"] for item in _pending} | _inflight
        _pending.extend(job for job in prepared if job["source_id"] not in known)
        _ensure_timer(0 if immediate else None)
    start_group_sweep()


def queue_recent_unsent(hours: int = 24) -> None:
    """Send matches for jobs touched since the last restart, once each.

    A description fill can put an older row into recommend without a new insert.
    Those rows were saved before this alert existed, so the insert hook never saw them.
    """
    if not alerts_enabled():
        return
    start_group_sweep()
    from jobscraper.db import JOBS_TABLE, get_conn

    conn = get_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            ensure_alert_table(cur)
            cur.execute(
                f"""
                SELECT source_id, title, company, skills, state, location, platform, url,
                       description, job_type, work_model, experience_level, years_experience,
                       visa_sponsorship, saudi_national_required, posted_at, platform_posted_at
                FROM {JOBS_TABLE}
                WHERE first_seen_at >= UTC_TIMESTAMP() - INTERVAL %s HOUR
                   OR updated_at >= UTC_TIMESTAMP() - INTERVAL %s HOUR
                ORDER BY first_seen_at DESC
                LIMIT 400
                """,
                (hours, hours),
            )
            rows = list(cur.fetchall() or [])
    except Exception:
        log.exception("WhatsApp catch-up query failed")
        _note("WhatsApp catch-up query failed")
        return
    _note(f"WhatsApp catch-up found {len(rows)} recent jobs")
    if rows:
        # Personal matches only. The group hears a job when that scrape saves it.
        queue_job_alerts(rows, group=False)
    start_group_sweep()


def _remember_sweep(
    *,
    found: int = 0,
    queued: int = 0,
    skipped: int = 0,
    error: str | None = None,
    skipped_titles: list[str] | None = None,
) -> None:
    global _SWEEP_STATUS
    _SWEEP_STATUS = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "found": found,
        "queued": queued,
        "skipped": skipped,
        "error": error,
        "skipped_titles": list(skipped_titles or []),
    }


def _ensure_first_seen_index(cur, table: str) -> None:
    """Jobs scanned by first_seen_at. Without this, startup reads the whole table."""
    global _FIRST_SEEN_INDEXED
    if _FIRST_SEEN_INDEXED:
        return
    try:
        cur.execute(
            """
            SELECT 1 AS ok FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = 'idx_jobs_first_seen'
            LIMIT 1
            """,
            (table,),
        )
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE {table} ADD INDEX idx_jobs_first_seen (first_seen_at)")
    except Exception:
        log.exception("Could not add first_seen index")
    _FIRST_SEEN_INDEXED = True


def _attach_short_descriptions(cur, table: str, rows: list[dict]) -> None:
    """Short description for the few jobs about to be posted. Not the whole table."""
    ids = [str(row.get("source_id") or "") for row in rows if row.get("source_id")]
    found: dict[str, str] = {}
    for start in range(0, len(ids), 200):
        chunk = ids[start : start + 200]
        marks = ", ".join(["%s"] * len(chunk))
        cur.execute(
            f"SELECT source_id, LEFT(description, 800) AS description "
            f"FROM {table} WHERE source_id IN ({marks})",
            chunk,
        )
        for hit in cur.fetchall() or []:
            source_id = str(hit.get("source_id") or "")
            if source_id:
                found[source_id] = str(hit.get("description") or "")
    for row in rows:
        row["description"] = found.get(str(row.get("source_id") or ""), "")


def queue_unsent_group_jobs(limit: int | None = None) -> None:
    """Post group jobs from the last 6 hours that never got a real message.

    A row marked skipped is tried again. Jobs already posted stay out.
    The description column is not part of this scan. It is a LONGTEXT, and
    reading it for the whole table hits the 20s query limit, so the sweep
    died on startup and the group stayed quiet.
    """
    if not group_alerts_enabled():
        _remember_sweep(error="group alerts are off")
        return
    channel = _group_id()
    if not channel:
        _remember_sweep(error="group id is missing")
        return
    if limit is None:
        limit = max(1, _int_env("WHATSAPP_GROUP_SWEEP_LIMIT", 1000))
    from jobscraper.db import JOBS_TABLE, get_conn

    conn = get_conn()
    if conn is None:
        _remember_sweep(error="database connection failed")
        _note("WhatsApp group sweep query failed")
        return
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SET SESSION max_execution_time=120000")
            except Exception:
                pass
            ensure_alert_table(cur)
            _ensure_first_seen_index(cur, JOBS_TABLE)
            cur.execute(
                f"""
                SELECT j.source_id, j.title, j.company, j.skills, j.state, j.location,
                       j.platform, j.url, j.company_career_url, j.job_type, j.work_model,
                       j.experience_level, j.years_experience, j.visa_sponsorship,
                       j.saudi_national_required, j.posted_at, j.platform_posted_at,
                       IFNULL(a.message, '') AS alert_message
                FROM {JOBS_TABLE} j
                LEFT JOIN {ALERTS_TABLE} a
                  ON a.source_id = j.source_id AND a.phone = %s
                WHERE j.title IS NOT NULL AND TRIM(j.title) <> ''
                  AND LOWER(IFNULL(j.platform, '')) NOT LIKE '%whatsapp%'
                  AND IFNULL(j.url, '') NOT LIKE '%wa.me%'
                  AND IFNULL(j.url, '') NOT LIKE '%api.whatsapp.com%'
                  AND j.first_seen_at >= UTC_TIMESTAMP() - INTERVAL 6 HOUR
                  AND (a.id IS NULL OR IFNULL(a.message, '') IN ('', 'skipped'))
                ORDER BY j.first_seen_at DESC
                LIMIT %s
                """,
                (channel[:64], limit),
            )
            fetched = list(cur.fetchall() or [])
            rows = []
            skipped = 0
            skipped_titles: list[str] = []
            for row in fetched:
                job = _alert_job(row)
                text = group_message_for(job)
                if job_is_alertable(job) and text and _group_message_ok(text):
                    rows.append(row)
                    continue
                if str(row.get("alert_message") or "") == "skipped":
                    continue
                # Leave these out of the send queue, and record them so the
                # next sweep is not stuck re-reading the same unusable rows.
                title = str(row.get("title") or "").strip()
                if title and len(skipped_titles) < 8:
                    skipped_titles.append(title[:80])
                try:
                    cur.connection.ping(reconnect=True)
                    _mark_sent(cur, channel[:64], job["source_id"], job=job, message="skipped")
                    skipped += 1
                except Exception as exc:
                    code = exc.args[0] if getattr(exc, "args", None) else None
                    if code == 1062:
                        cur.execute(
                            f"UPDATE {ALERTS_TABLE} SET message = 'skipped' "
                            "WHERE phone = %s AND source_id = %s "
                            "AND IFNULL(message, '') IN ('', 'skipped')",
                            (channel[:64], job["source_id"]),
                        )
                        skipped += 1
                    else:
                        log.exception("WhatsApp group sweep could not skip unusable job")
            if rows:
                try:
                    _attach_short_descriptions(cur, JOBS_TABLE, rows)
                except Exception:
                    log.exception("WhatsApp group sweep could not read descriptions")
    except Exception:
        log.exception("WhatsApp group sweep query failed")
        _note("WhatsApp group sweep query failed")
        _remember_sweep(error="query failed")
        return
    _remember_sweep(found=len(fetched), queued=len(rows), skipped=skipped, skipped_titles=skipped_titles)
    _note(
        f"WhatsApp group sweep found {len(rows)} unsent job(s)"
        + (f"; skipped {skipped} with no usable title or link" if skipped else "")
    )
    if rows:
        queue_job_alerts(rows, immediate=True)


def start_group_sweep() -> None:
    global _SWEEP_STARTED
    if not group_alerts_enabled():
        return
    with _LOCK:
        if _SWEEP_STARTED:
            return
        _SWEEP_STARTED = True

    def _loop() -> None:
        while True:
            try:
                queue_unsent_group_jobs()
            except Exception:
                log.exception("WhatsApp group sweep failed")
            time.sleep(max(30, _int_env("WHATSAPP_GROUP_SWEEP_SECONDS", 90)))

    threading.Thread(target=_loop, name="whatsapp-group-sweep", daemon=True).start()


def _note_group_backoff() -> None:
    global _group_backoff_until
    pause = max(30, _int_env("WHATSAPP_GROUP_BACKOFF_SECONDS", 120))
    _group_backoff_until = time.time() + pause
    _note(f"WhatsApp group send failed; next batch waits {pause}s")


def _ensure_timer(delay: int | None = None) -> None:
    global _timer
    if _timer is not None and _timer.is_alive():
        return
    if delay is None:
        delay = max(0, _int_env("WHATSAPP_DEBOUNCE_SECONDS", 30))
    backoff = _group_backoff_until - time.time()
    if backoff > delay:
        delay = backoff
    _timer = threading.Timer(delay, _flush)
    _timer.daemon = True
    _timer.start()


def _flush() -> None:
    global _timer
    with _LOCK:
        batch = list(_pending)
        _pending.clear()
        _timer = None
    try:
        if batch:
            with _DELIVER_LOCK:
                deliver_job_alerts(batch)
    except Exception:
        log.exception("WhatsApp alert flush failed")
    finally:
        with _LOCK:
            if _pending:
                _ensure_timer()


_WHATSAPP_LINK = re.compile(
    r"(?:https?://)?(?:www\.)?(?:wa\.me|api\.whatsapp\.com)/\S+",
    re.IGNORECASE,
)
_HEADER_ECHO = re.compile(r"new\s+job\s+(?:in\s+saudi|for\s+you)", re.IGNORECASE)
_PLACE_NOISE = re.compile(
    r"saudi arabia\s*[·•|\-]\s*whatsapp|\bwhatsapp\b",
    re.IGNORECASE,
)
_JUNK_TITLES = {
    "job",
    "jobs",
    "new job",
    "new job in saudi",
    "new job for you",
    "saudi arabia",
    "the right people",
    "right people",
}
_COUNTRY_ONLY = {
    "saudi arabia",
    "saudi",
    "ksa",
    "kingdom of saudi arabia",
    "ksa saudi arabia",
}
_GROUP_DESC_LIMIT = 220
_GROUP_TITLE_LIMIT = 100
_GROUP_MESSAGE_LIMIT = 1100


def _clip(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0].rstrip()
    return cut + "…"


def _clean_field(value: str) -> str:
    text = _WHATSAPP_LINK.sub(" ", str(value or ""))
    text = _HEADER_ECHO.sub(" ", text)
    text = _PLACE_NOISE.sub(" ", text)
    return " ".join(text.split()).strip()


def _job_link(url: str) -> str:
    """Real posting link. A wa.me link is a WhatsApp number, not the job."""
    text = str(url or "").strip()
    if not text or _WHATSAPP_LINK.search(text):
        return ""
    return text


def _group_apply_link(job: dict) -> str:
    """Company career page when we have one, otherwise the board listing."""
    from jobscraper.agent_reach import employer_career_url

    career = employer_career_url(job.get("company_career_url"))
    if career:
        return career
    return _job_link(str(job.get("url") or ""))


def _display_platform(value: str) -> str:
    platform = _clean_field(value)
    if not platform or "whatsapp" in platform.lower():
        return ""
    return platform


def _is_whatsapp_source(job: dict) -> bool:
    platform = str(job.get("platform") or "").lower()
    url = str(job.get("url") or "").lower()
    if "whatsapp" in platform:
        return True
    if "wa.me" in url or "api.whatsapp.com" in url:
        return True
    return False


def _is_junk_title(title: str) -> bool:
    lowered = " ".join(title.lower().strip(" .").split())
    if not lowered or lowered in _JUNK_TITLES:
        return True
    if lowered.startswith("new job in saudi") or lowered.startswith("new job for you"):
        return True
    return False


def _short_label(value: str, limit: int = 80) -> str:
    """Company or city. A pasted paragraph is not a place name."""
    text = _clean_field(value)
    if not text or text.lower() in _COUNTRY_ONLY:
        return ""
    if len(text) > limit or "!" in text or len(text.split()) > 8:
        return ""
    return text


def _display_title(job: dict) -> str:
    title = _clip(_clean_field(str(job.get("title") or "")), _GROUP_TITLE_LIMIT)
    if not title or _is_junk_title(title):
        return ""
    # Scrapes sometimes store a description sentence in title.
    # A long lowercase sentence is body text. A short lowercase title is still a job.
    if title[:1].islower() and (len(title) > 60 or len(title.split()) > 8 or "." in title or "!" in title):
        return ""
    if "wa.me" in title.lower() or "api.whatsapp.com" in title.lower():
        return ""
    return title


def _clean_description(job: dict, title: str) -> str:
    raw = str(job.get("description") or "")
    # Old WhatsApp bubbles sometimes got saved back into description.
    raw = _HEADER_ECHO.split(raw)[0]
    parts = []
    for chunk in re.split(r"[\r\n]+", raw):
        low_raw = chunk.lower()
        if "whatsapp" in low_raw or "wa.me" in low_raw or "api.whatsapp.com" in low_raw:
            continue
        piece = _clean_field(chunk)
        if not piece or piece.lower().startswith("skills:"):
            continue
        if "new job in saudi" in piece.lower() or "new job for you" in piece.lower():
            continue
        parts.append(piece)
    description = " ".join(parts)
    if title and description.lower().startswith(title.lower()[:40].lower()):
        return ""
    return description


def job_is_alertable(job: dict) -> bool:
    """Drop WhatsApp-sourced junk and rows with no real title or apply link."""
    if _is_whatsapp_source(job):
        return False
    title = _display_title(job)
    url = _job_link(str(job.get("url") or ""))
    company = _short_label(str(job.get("company") or ""))
    if not title:
        return False
    if not url and not company:
        return False
    return True


def _group_repeat_key(job: dict) -> str:
    title = _display_title(job).lower()
    company = _short_label(str(job.get("company") or "")).lower()
    url = _job_link(str(job.get("url") or "")).lower()
    return f"{title}|{company}|{url}"


def _group_urls_already_sent(cur, channel: str, urls: list[str]) -> set[str]:
    found: set[str] = set()
    clean: list[str] = []
    seen: set[str] = set()
    for url in urls:
        text = _job_link(url)
        if text and text not in seen:
            seen.add(text)
            clean.append(text)
    for start in range(0, len(clean), 100):
        chunk = clean[start : start + 100]
        marks = ", ".join(["%s"] * len(chunk))
        cur.execute(
            f"SELECT url FROM {ALERTS_TABLE} WHERE phone = %s "
            f"AND IFNULL(message, '') NOT IN ('', 'skipped') AND url IN ({marks})",
            [channel, *chunk],
        )
        for hit in cur.fetchall() or []:
            saved = _job_link(str(hit.get("url") or ""))
            if saved:
                found.add(saved)
    return found


def _group_message_ok(text: str) -> bool:
    """Reject pasted WhatsApp bubbles. A real apply link may stay."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or lines[0].lower() != "new job in saudi":
        return False
    if _is_junk_title(lines[1]):
        return False
    body: list[str] = []
    for line in lines[1:]:
        low = line.lower()
        if low.startswith("http://") or low.startswith("https://"):
            if "wa.me" in low or "api.whatsapp.com" in low:
                return False
            continue
        body.append(low)
    content = "\n".join(body)
    if "wa.me" in content or "api.whatsapp.com" in content or "new job in saudi" in content:
        return False
    return True


def _format_job_message(
    job: dict,
    *,
    header: str,
    extra_lines: list[str] | None = None,
    prefer_career: bool = False,
) -> str:
    title = _display_title(job)
    if not title:
        return ""
    place = " · ".join(
        part
        for part in (
            _short_label(str(job.get("company") or "")),
            _short_label(str(job.get("location") or job.get("state") or "")),
            _display_platform(str(job.get("platform") or "")),
        )
        if part
    )
    skills = [_clean_field(str(item)) for item in (job.get("skills") or []) if _clean_field(str(item))][:6]
    url = _group_apply_link(job) if prefer_career else _job_link(str(job.get("url") or ""))
    description = _clean_description(job, title)

    fixed = [header, "", title]
    if place:
        fixed.append(place)
    if skills:
        fixed.append("Skills: " + ", ".join(skills))
    for line in extra_lines or []:
        if line:
            fixed.append(line)
    fixed_text = "\n".join(fixed)
    room = _GROUP_MESSAGE_LIMIT - len(fixed_text) - len(url) - 4
    if description and room >= 40:
        fixed.extend(["", _clip(description, min(_GROUP_DESC_LIMIT, room))])
    if url:
        fixed.extend(["", url])
    return "\n".join(fixed).strip()


def group_message_for(job: dict) -> str:
    """One job, one short WhatsApp group message.

    The link is the company career page when that was found. Otherwise it is
    the board listing.
    """
    return _format_job_message(job, header="New job in Saudi", prefer_career=True)


def message_for(job: dict, match: dict) -> str:
    why = list(match.get("matched_skills") or [])[:4] or list(match.get("matched_titles") or [])[:2]
    extra = []
    if why:
        extra.append("Match: " + ", ".join(_clean_field(str(item)) for item in why if _clean_field(str(item))))
    return _format_job_message(job, header="New job for you", extra_lines=extra)


def _auth_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    key = _api_key()
    if not key:
        return headers
    header = _env("WHATSAPP_API_KEY_HEADER", "x-api-key") or "x-api-key"
    if header.lower() == "authorization" and not key.lower().startswith("bearer "):
        key = "Bearer " + key
    headers[header] = key
    return headers


_PHONE_COLUMNS = (
    "whatsapp",
    "whatsapp_number",
    "phone",
    "phone_number",
    "mobile",
    "mobile_number",
    "contact_number",
)
_CODE_COLUMNS = ("phone_country_code", "phone_code", "country_code", "dial_code")


def _note(detail: str) -> None:
    log.warning("%s", detail)
    try:
        path = Path(__file__).resolve().parent.parent / "data" / "whatsapp-alerts.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {detail}\n")
    except Exception:
        log.exception("Could not write WhatsApp alert log")


def _send_url() -> str:
    """Prefer the names already in the server .env: WHATSAPP_API_URL, then WHATSAPP_SEND_URL."""
    raw = _env("WHATSAPP_API_URL") or _env("WHATSAPP_SEND_URL") or DEFAULT_SEND_URL
    raw = raw.strip().rstrip("/")
    path = urlparse(raw).path
    if path and path not in {"/"}:
        return raw
    return raw + "/sessions/Intellirestro/send-message"


def _send_body(number: str, message: str, url: str) -> dict:
    if url.rstrip("/").endswith("/send-text"):
        chat = number if "@" in number else f"{number}@c.us"
        return {"chatId": chat, "text": message}
    return {"number": number, "message": message}


def _group_id() -> str:
    value = _env("WHATSAPP_GROUP_ID", DEFAULT_GROUP_ID)
    if "@g.us" not in value:
        return ""
    return value


def _group_name() -> str:
    return _env("WHATSAPP_GROUP_NAME", DEFAULT_GROUP_NAME) or DEFAULT_GROUP_NAME


def _group_url() -> str:
    return (_env("WHATSAPP_GROUP_URL") or DEFAULT_GROUP_URL).strip().rstrip("/")


def _post_json(url: str, payload: dict, *, label: str) -> bool:
    if not _api_key():
        _note(f"{label} skipped: WHATSAPP_API_KEY is empty and the gateway returns 401")
        return False
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=_auth_headers(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()[:500]
            if not (200 <= int(response.status) < 300):
                _note(f"{label} failed status={response.status} body={raw!r}")
                return False
            if raw:
                try:
                    parsed = json.loads(raw.decode("utf-8", "ignore"))
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("success") is False:
                    _note(f"WhatsApp gateway rejected the message: {parsed}")
                    return False
            return True
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300]
        _note(f"{label} failed status={exc.code} body={detail!r}")
        return False
    except Exception:
        log.exception("%s failed", label)
        _note(f"{label} failed")
        return False


def send_whatsapp(number: str, message: str) -> bool:
    url = _send_url()
    return _post_json(url, _send_body(number, message, url), label=f"WhatsApp send to {number}")


def send_whatsapp_group(message: str) -> bool:
    group_id = _group_id()
    if not group_id:
        _note("WhatsApp group send skipped: WHATSAPP_GROUP_ID is not a group id")
        return False
    return _post_json(
        _group_url(),
        {"groupid": group_id, "groupname": _group_name(), "message": message},
        label="WhatsApp group send",
    )


def ensure_alert_table(cur) -> None:
    cur.execute(CREATE_ALERTS_SQL)
    _ensure_alert_columns(cur)
    _widen_phone(cur)


def _widen_phone(cur) -> None:
    """Group ids such as 966...@g.us do not fit the original VARCHAR(20) phone column."""
    cur.execute(
        """
        SELECT CHARACTER_MAXIMUM_LENGTH AS width
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = 'phone'
        """,
        (ALERTS_TABLE,),
    )
    row = cur.fetchone() or {}
    width = int(row.get("width") or 0)
    if width and width < 64:
        cur.execute(f"ALTER TABLE {ALERTS_TABLE} MODIFY COLUMN `phone` VARCHAR(64) NOT NULL")


def _ensure_alert_columns(cur) -> None:
    """Add recommendation fields on older alert tables that only stored phone+source_id."""
    existing = _table_columns(cur, ALERTS_TABLE)
    if "candidate_id" in existing:
        cur.execute(
            "SELECT INDEX_NAME AS name FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = 'candidate_id'",
            (ALERTS_TABLE,),
        )
        for row in cur.fetchall() or []:
            index = str(row.get("name") or "")
            if index and index != "PRIMARY":
                cur.execute(f"ALTER TABLE {ALERTS_TABLE} DROP INDEX `{index}`")
        cur.execute(f"ALTER TABLE {ALERTS_TABLE} DROP COLUMN `candidate_id`")
        existing.discard("candidate_id")
    for name, ddl in _ALERT_COLUMNS.items():
        if name in existing:
            continue
        cur.execute(f"ALTER TABLE {ALERTS_TABLE} ADD COLUMN `{name}` {ddl}")


def _profile_phone(profile: dict) -> tuple[str, str]:
    number = str(profile.get("phone") or profile.get("phone_number") or "")
    code = str(profile.get("phone_code") or profile.get("phone_country_code") or "")
    return number, code


def _recipient_from_profile(
    profile: dict,
    phone: str,
    *,
    user_id: str = "",
) -> dict | None:
    skills = skills_from_profile(profile)
    certifications = certifications_from_profile(profile)
    titles = experience_titles_from_profile(profile)
    if not skills and not certifications and not titles:
        return None
    return {
        "phone": phone,
        "user_id": str(user_id or "").strip() or None,
        "profile": profile,
        "skills": skills,
        "certifications": certifications,
        "titles": titles,
        "years": career_years(profile),
    }


def _named_column(names: set[str], choices: tuple[str, ...]) -> str:
    for name in choices:
        if name in names:
            return name
    return ""


def _table_columns(cur, table: str) -> set[str]:
    cur.execute(
        """
        SELECT COLUMN_NAME AS name
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        """,
        (table,),
    )
    return {str(row.get("name") or "") for row in cur.fetchall() or []}


def _load_recipients() -> list[dict]:
    from jobscraper.db import CANDIDATES_TABLE, get_conn, load_app_user

    conn = get_conn()
    if conn is None:
        return []
    user_rows: list[dict] = []
    candidate_rows: list[dict] = []
    try:
        with conn.cursor() as cur:
            ensure_alert_table(cur)
            cur.execute("SHOW TABLES LIKE 'users'")
            has_users = bool(cur.fetchone())
            cur.execute("SHOW TABLES LIKE 'profiles'")
            has_profiles = bool(cur.fetchone())
            if has_users and has_profiles:
                columns = _table_columns(cur, "profiles")
                number_col = _named_column(columns, _PHONE_COLUMNS)
                code_col = _named_column(columns, _CODE_COLUMNS)
                if number_col:
                    code_sql = f", p.`{code_col}` AS phone_code" if code_col else ", '' AS phone_code"
                    cur.execute(
                        f"SELECT u.id AS user_id, p.`{number_col}` AS phone {code_sql} "
                        "FROM users u JOIN profiles p ON p.user_id = u.id "
                        f"WHERE p.`{number_col}` IS NOT NULL AND TRIM(p.`{number_col}`) <> ''"
                    )
                    user_rows = list(cur.fetchall() or [])
            cur.execute(f"SHOW TABLES LIKE '{CANDIDATES_TABLE}'")
            if cur.fetchone():
                cur.execute(f"SELECT candidate_id, profile FROM {CANDIDATES_TABLE}")
                candidate_rows = list(cur.fetchall() or [])
    except Exception:
        log.exception("Could not list WhatsApp alert recipients")
        _note("WhatsApp alerts skipped: could not read user phones")
        return []

    recipients: list[dict] = []
    seen: set[str] = set()
    for row in user_rows:
        number = whatsapp_number(str(row.get("phone") or ""), str(row.get("phone_code") or ""))
        if not number or number in seen:
            continue
        try:
            profile = load_app_user(str(row.get("user_id")))
        except Exception:
            log.exception("Alert profile failed for user %s", row.get("user_id"))
            continue
        if not profile:
            continue
        profile = dict(profile)
        profile.setdefault("phone", row.get("phone") or "")
        built = _recipient_from_profile(
            profile,
            number,
            user_id=str(row.get("user_id") or ""),
        )
        if not built:
            continue
        seen.add(number)
        recipients.append(built)

    from jobscraper.db import _profile_from_cell

    for row in candidate_rows:
        profile = _profile_from_cell(row.get("profile")) or {}
        raw, code = _profile_phone(profile)
        number = whatsapp_number(raw, code)
        if not number or number in seen:
            continue
        built = _recipient_from_profile(profile, number)
        if not built:
            continue
        seen.add(number)
        recipients.append(built)
    return recipients


def _cached_recipients() -> list[dict]:
    now = time.time()
    if now - float(_recipients.get("at") or 0) < _RECIPIENT_TTL:
        return list(_recipients.get("rows") or [])
    rows = _load_recipients()
    _recipients["at"] = now
    _recipients["rows"] = rows
    return list(rows)


def _sent_ids(cur, phone: str, source_ids: list[str], *, require_message: bool = False) -> set[str]:
    found: set[str] = set()
    posted = " AND IFNULL(message, '') NOT IN ('', 'skipped')" if require_message else ""
    for start in range(0, len(source_ids), 200):
        chunk = source_ids[start : start + 200]
        marks = ", ".join(["%s"] * len(chunk))
        cur.execute(
            f"SELECT source_id FROM {ALERTS_TABLE} WHERE phone = %s AND source_id IN ({marks}){posted}",
            [phone, *chunk],
        )
        for hit in cur.fetchall() or []:
            if hit.get("source_id"):
                found.add(str(hit["source_id"]))
    return found


def _sent_today(cur, phone: str) -> int:
    cur.execute(
        f"SELECT COUNT(*) AS n FROM {ALERTS_TABLE} "
        "WHERE phone = %s AND sent_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY",
        (phone,),
    )
    row = cur.fetchone() or {}
    return int(row.get("n") or 0)


def _match_payload(match: dict) -> str:
    return json.dumps(
        {
            "score": int(match.get("score") or 0),
            "matched_skills": list(match.get("matched_skills") or [])[:20],
            "matched_certifications": list(match.get("matched_certifications") or [])[:20],
            "matched_titles": list(match.get("matched_titles") or [])[:10],
            "experience_fit": match.get("experience_fit") or "none",
        },
        ensure_ascii=False,
    )


def _mark_sent(
    cur,
    phone: str,
    source_id: str,
    *,
    job: dict | None = None,
    match: dict | None = None,
    message: str = "",
    user_id: str | None = None,
) -> None:
    """Persist the recommendation that was just sent on WhatsApp."""
    job = job or {}
    match = match or {}
    cur.execute(
        f"""
        INSERT INTO {ALERTS_TABLE} (
          phone, source_id, user_id, title, company, url, platform,
          score, match_json, message, sent_at
        ) VALUES (
          %s, %s, %s, %s, %s, %s, %s,
          %s, %s, %s, UTC_TIMESTAMP(6)
        )
        """,
        (
            phone,
            source_id,
            user_id or None,
            (str(job.get("title") or "")[:512] or None),
            (str(job.get("company") or "")[:512] or None),
            (str(job.get("url") or "")[:1024] or None),
            (str(job.get("platform") or "")[:64] or None),
            int(match.get("score") or 0) or None,
            _match_payload(match),
            (message or "")[:65000] or None,
        ),
    )


def _retry_job(job: dict) -> dict | None:
    retry = dict(job)
    retry["_attempts"] = int(job.get("_attempts") or 0) + 1
    if retry["_attempts"] >= 3:
        return None
    return retry


def _requeue(jobs: list[dict]) -> None:
    merged: dict[str, dict] = {}
    for job in jobs:
        source_id = str(job.get("source_id") or "")
        if not source_id:
            continue
        previous = merged.get(source_id)
        if previous is None or int(job.get("_attempts") or 0) >= int(previous.get("_attempts") or 0):
            merged[source_id] = job
    if merged:
        queue_job_alerts(list(merged.values()))


def _group_flush_limit() -> int:
    """How many group messages one pass may send. 0 means the whole batch."""
    return max(0, _int_env("WHATSAPP_GROUP_MAX_PER_FLUSH", 0))


def _record_group_send(cur, channel: str, job: dict, message: str) -> bool:
    """Store that this job already went to the group. False if the row was not saved."""
    for _attempt in range(2):
        try:
            cur.connection.ping(reconnect=True)
            _mark_sent(cur, channel, job["source_id"], job=job, message=message)
            return True
        except Exception as exc:
            code = exc.args[0] if getattr(exc, "args", None) else None
            if code == 1062:
                if message:
                    cur.execute(
                        f"UPDATE {ALERTS_TABLE} SET message = %s, sent_at = UTC_TIMESTAMP(6) "
                        "WHERE phone = %s AND source_id = %s "
                        "AND IFNULL(message, '') IN ('', 'skipped')",
                        ((message or "")[:65000], channel, job["source_id"]),
                    )
                return True
            log.exception("WhatsApp group alert row was not saved")
    return False


def _post_new_jobs_to_group(cur, jobs: list[dict]) -> tuple[int, list[dict], bool]:
    """Post new scrapes to the group. Returns (sent, still pending, keep going).

    keep going is true only when this pass stopped because of the batch cap
    and the gateway was still accepting messages.
    """
    if not group_alerts_enabled():
        return 0, [], False
    channel = _group_id()
    if not channel:
        _note("WhatsApp group send skipped: WHATSAPP_GROUP_ID is not a group id")
        return 0, [], False
    group_jobs = [job for job in jobs if job.get("_group", True)]
    already = _sent_ids(
        cur,
        channel[:64],
        [job["source_id"] for job in group_jobs],
        require_message=True,
    )
    posted_urls = _group_urls_already_sent(
        cur,
        channel[:64],
        [_job_link(str(job.get("url") or "")) for job in group_jobs],
    )
    pending = []
    seen_keys: set[str] = set()
    for job in group_jobs:
        if job["source_id"] in already:
            continue
        url = _job_link(str(job.get("url") or ""))
        key = _group_repeat_key(job)
        if (url and url in posted_urls) or key in seen_keys:
            _record_group_send(cur, channel[:64], job, "skipped")
            continue
        seen_keys.add(key)
        if url:
            posted_urls.add(url)
        pending.append(job)
    cap = _group_flush_limit()
    window = pending if cap <= 0 else pending[:cap]
    overflow = [] if cap <= 0 else pending[cap:]
    sent = 0
    left: list[dict] = []
    consecutive_fails = 0
    fail_limit = max(1, _int_env("WHATSAPP_GROUP_FAILS_BEFORE_BACKOFF", 3))
    gap = max(1.5, float(_env("WHATSAPP_GROUP_SEND_GAP", "2") or "2"))
    keep_going = False
    index = 0
    try:
        for index, job in enumerate(window):
            if not job_is_alertable(job):
                _record_group_send(cur, channel[:64], job, "skipped")
                consecutive_fails = 0
                continue
            text = group_message_for(job)
            if not text or not _group_message_ok(text):
                _record_group_send(cur, channel[:64], job, "skipped")
                consecutive_fails = 0
                continue
            if send_whatsapp_group(text):
                _record_group_send(cur, channel[:64], job, text)
                sent += 1
                consecutive_fails = 0
                # Gateway merges bursts into one cut bubble if sends are too close.
                time.sleep(gap)
                continue
            consecutive_fails += 1
            left.append(job)
            if consecutive_fails >= fail_limit:
                _note_group_backoff()
                left.extend(window[index + 1 :])
                break
            time.sleep(gap)
        else:
            # One rejected job must not pull the overflow straight back into
            # this same pass, or that job is retried forever.
            keep_going = bool(overflow) and not left
    except Exception:
        log.exception("WhatsApp group send stopped")
        _note("WhatsApp group send stopped; remaining jobs stay queued")
        left.extend(window[index:])
        keep_going = False
    left.extend(overflow)
    return sent, left, keep_going


def deliver_job_alerts(jobs: list[dict]) -> int:
    """Post each new job to the group, then send personal matches. Returns messages sent."""
    if not alerts_enabled():
        return 0
    unique: dict[str, dict] = {}
    for job in jobs:
        source_id = str(job.get("source_id") or "")
        if source_id:
            unique[source_id] = job
    jobs = list(unique.values())
    if not jobs:
        return 0
    source_ids = [job["source_id"] for job in jobs]
    with _LOCK:
        _inflight.update(source_ids)
    recipients = _cached_recipients()

    from jobscraper.db import get_conn

    conn = get_conn()
    if conn is None:
        with _LOCK:
            _inflight.difference_update(source_ids)
        return 0
    minimum = max(1, _int_env("WHATSAPP_MIN_SCORE", 1))
    per_flush = max(1, _int_env("WHATSAPP_MAX_PER_FLUSH", 30))
    # 0 means every matching job is sent. A positive number is a daily cap.
    per_day = _int_env("WHATSAPP_MAX_PER_DAY", 0)
    sent = 0
    group_sent = 0
    group_left: list[dict] = []
    failed: list[dict] = []
    waiting: list[dict] = []
    try:
        with conn.cursor() as cur:
            ensure_alert_table(cur)
            group_batch = list(jobs)
            while group_batch:
                sent_now, group_left, keep_going = _post_new_jobs_to_group(cur, group_batch)
                group_sent += sent_now
                sent += sent_now
                if not keep_going or not group_left or len(group_left) >= len(group_batch):
                    break
                group_batch = group_left
            if not recipients:
                _note(
                    f"WhatsApp group sent {group_sent} of {len(jobs)} new job(s); "
                    "personal alerts skipped: no user with a phone and skills"
                )
            for person in recipients:
                phone = person["phone"]
                already = _sent_ids(cur, phone, [job["source_id"] for job in jobs])
                room = None if per_day <= 0 else per_day - _sent_today(cur, phone)
                if room is not None and room <= 0:
                    continue
                matched: list[tuple[int, dict, dict]] = []
                for job in jobs:
                    if job["source_id"] in already:
                        continue
                    if not job_is_alertable(job):
                        continue
                    if not filter_jobs_for_candidate([job], person["profile"], state="All", city=""):
                        continue
                    match = score_job(
                        job,
                        person["skills"],
                        certifications=person["certifications"],
                        experience_titles=person["titles"],
                        years=person["years"],
                    )
                    if int(match.get("score") or 0) < minimum:
                        continue
                    matched.append((int(match["score"]), job, match))
                matched.sort(key=lambda item: (-item[0], item[1].get("title") or ""))
                take = per_flush if room is None else min(per_flush, room)
                for _score, job, _match in matched[take:]:
                    waiting.append(job)
                for _score, job, match in matched[:take]:
                    text = message_for(job, match)
                    if send_whatsapp(phone, text):
                        try:
                            _mark_sent(
                                cur,
                                phone,
                                job["source_id"],
                                job=job,
                                match=match,
                                message=text,
                                user_id=person.get("user_id"),
                            )
                        except Exception as exc:
                            code = exc.args[0] if getattr(exc, "args", None) else None
                            if code != 1062:
                                raise
                        sent += 1
                        time.sleep(0.35)
                        continue
                    retry = _retry_job(job)
                    if retry is not None:
                        failed.append(retry)
    except Exception:
        log.exception("WhatsApp alert delivery failed")
        _note("WhatsApp alert delivery failed")
    finally:
        with _LOCK:
            _inflight.difference_update(source_ids)
    _requeue(group_left + failed + waiting)
    if recipients:
        _note(
            f"WhatsApp alerts sent {sent - group_sent} personal message(s) and "
            f"{group_sent} group message(s) for {len(jobs)} job(s) "
            f"to {len(recipients)} user(s); {len(waiting)} still queued"
        )
    return sent
