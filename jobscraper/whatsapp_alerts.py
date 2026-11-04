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
_timer: threading.Timer | None = None
_recipients: dict = {"at": 0.0, "rows": []}
_RECIPIENT_TTL = 600


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
    }


def queue_job_alerts(rows: list[dict]) -> None:
    """Hold brand-new jobs briefly so one scrape wave becomes one matching pass."""
    if not alerts_enabled() or not rows:
        return
    prepared = []
    seen: set[str] = set()
    for row in rows:
        job = _alert_job(row)
        if not job["source_id"] or not job["title"] or job["source_id"] in seen:
            continue
        seen.add(job["source_id"])
        prepared.append(job)
    if not prepared:
        return
    with _LOCK:
        known = {item["source_id"] for item in _pending}
        _pending.extend(job for job in prepared if job["source_id"] not in known)
        _ensure_timer()


def queue_recent_unsent(hours: int = 24) -> None:
    """Send matches for jobs touched since the last restart, once each.

    A description fill can put an older row into recommend without a new insert.
    Those rows were saved before this alert existed, so the insert hook never saw them.
    """
    if not alerts_enabled():
        return
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
        queue_job_alerts(rows)


def _ensure_timer() -> None:
    global _timer
    if _timer is not None and _timer.is_alive():
        return
    delay = max(0, _int_env("WHATSAPP_DEBOUNCE_SECONDS", 30))
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


def _clip(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def group_message_for(job: dict) -> str:
    place = " · ".join(
        part
        for part in (
            job.get("company"),
            job.get("location") or job.get("state"),
            job.get("platform"),
        )
        if part
    )
    lines = ["New job in Saudi", "", str(job.get("title") or "Job")]
    if place:
        lines.append(place)
    skills = [str(item).strip() for item in (job.get("skills") or []) if str(item).strip()][:6]
    if skills:
        lines.append("Skills: " + ", ".join(skills))
    description = _clip(job.get("description") or "", 320)
    if description:
        lines.extend(["", description])
    url = str(job.get("url") or "").strip()
    if url:
        lines.extend(["", url])
    return "\n".join(lines)


def message_for(job: dict, match: dict) -> str:
    place = " · ".join(
        part for part in (job.get("company"), job.get("location"), job.get("platform")) if part
    )
    lines = ["New job for you", "", str(job.get("title") or "Job")]
    if place:
        lines.append(place)
    why = list(match.get("matched_skills") or [])[:4] or list(match.get("matched_titles") or [])[:2]
    if why:
        lines.append("Match: " + ", ".join(why))
    description = _clip(job.get("description") or "", 320)
    if description:
        lines.extend(["", description])
    url = str(job.get("url") or "").strip()
    if url:
        lines.extend(["", url])
    return "\n".join(lines)


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


def _sent_ids(cur, phone: str, source_ids: list[str]) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(source_ids), 200):
        chunk = source_ids[start : start + 200]
        marks = ", ".join(["%s"] * len(chunk))
        cur.execute(
            f"SELECT source_id FROM {ALERTS_TABLE} WHERE phone = %s AND source_id IN ({marks})",
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


def _post_new_jobs_to_group(cur, jobs: list[dict]) -> tuple[int, list[dict]]:
    """Post each new scrape to the group once. Returns (sent, still pending)."""
    if not group_alerts_enabled():
        return 0, []
    channel = _group_id()
    if not channel:
        _note("WhatsApp group send skipped: WHATSAPP_GROUP_ID is not a group id")
        return 0, []
    already = _sent_ids(cur, channel[:64], [job["source_id"] for job in jobs])
    pending = [job for job in jobs if job["source_id"] not in already]
    per_flush = max(1, _int_env("WHATSAPP_GROUP_MAX_PER_FLUSH", 30))
    sent = 0
    left: list[dict] = []
    for job in pending[:per_flush]:
        text = group_message_for(job)
        if send_whatsapp_group(text):
            try:
                _mark_sent(cur, channel[:64], job["source_id"], job=job, message=text)
            except Exception as exc:
                code = exc.args[0] if getattr(exc, "args", None) else None
                if code != 1062:
                    raise
            sent += 1
            time.sleep(0.35)
            continue
        retry = _retry_job(job)
        if retry is not None:
            left.append(retry)
    left.extend(pending[per_flush:])
    return sent, left


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
    recipients = _cached_recipients()

    from jobscraper.db import get_conn

    conn = get_conn()
    if conn is None:
        return 0
    minimum = max(1, _int_env("WHATSAPP_MIN_SCORE", 1))
    per_flush = max(1, _int_env("WHATSAPP_MAX_PER_FLUSH", 30))
    # 0 means every matching job is sent. A positive number is a daily cap.
    per_day = _int_env("WHATSAPP_MAX_PER_DAY", 0)
    sent = 0
    group_sent = 0
    failed: list[dict] = []
    waiting: list[dict] = []
    try:
        with conn.cursor() as cur:
            ensure_alert_table(cur)
            group_sent, group_left = _post_new_jobs_to_group(cur, jobs)
            sent += group_sent
            waiting.extend(group_left)
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
        return sent
    _requeue(failed + waiting)
    if recipients:
        _note(
            f"WhatsApp alerts sent {sent - group_sent} personal message(s) and "
            f"{group_sent} group message(s) for {len(jobs)} job(s) "
            f"to {len(recipients)} user(s); {len(waiting)} still queued"
        )
    return sent
