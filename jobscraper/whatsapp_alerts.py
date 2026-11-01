"""WhatsApp alerts for jobs that newly match a person's recommendations.

Every ingest path saves through upsert_jobs. That function queues only rows
that were not already stored. A short debounce then scores those rows with the
same matcher as /api/recommend and posts one message per matching job.
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
CREATE_ALERTS_SQL = f"""
CREATE TABLE IF NOT EXISTS {ALERTS_TABLE} (
  id BIGINT NOT NULL AUTO_INCREMENT,
  phone VARCHAR(20) NOT NULL,
  source_id VARCHAR(255) NOT NULL,
  sent_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_whatsapp_alert (phone, source_id),
  KEY idx_whatsapp_alert_sent (phone, sent_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

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
    key = _env("WHATSAPP_API_KEY")
    if not key:
        return headers
    header = _env("WHATSAPP_API_KEY_HEADER", "X-API-Key") or "X-API-Key"
    if header.lower() == "authorization" and not key.lower().startswith("bearer "):
        key = "Bearer " + key
    headers[header] = key
    return headers


def _note_failure(detail: str) -> None:
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


def send_whatsapp(number: str, message: str) -> bool:
    url = _send_url()
    if not _env("WHATSAPP_API_KEY"):
        _note_failure("WhatsApp send skipped: WHATSAPP_API_KEY is empty and the gateway returns 401")
        return False
    body = json.dumps(_send_body(number, message, url), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=_auth_headers(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()[:500]
            if not (200 <= int(response.status) < 300):
                _note_failure(f"WhatsApp send failed status={response.status} body={raw!r}")
                return False
            if raw:
                try:
                    payload = json.loads(raw.decode("utf-8", "ignore"))
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and payload.get("success") is False:
                    _note_failure(f"WhatsApp gateway rejected the message: {payload}")
                    return False
            return True
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300]
        _note_failure(f"WhatsApp send failed status={exc.code} body={detail!r}")
        return False
    except Exception:
        log.exception("WhatsApp send failed for %s", number)
        _note_failure(f"WhatsApp send failed for {number}")
        return False


def ensure_alert_table(cur) -> None:
    cur.execute(CREATE_ALERTS_SQL)


def _profile_phone(profile: dict) -> tuple[str, str]:
    number = str(profile.get("phone") or profile.get("phone_number") or "")
    code = str(profile.get("phone_code") or profile.get("phone_country_code") or "")
    return number, code


def _recipient_from_profile(profile: dict, phone: str) -> dict | None:
    skills = skills_from_profile(profile)
    certifications = certifications_from_profile(profile)
    titles = experience_titles_from_profile(profile)
    if not skills and not certifications and not titles:
        return None
    return {
        "phone": phone,
        "profile": profile,
        "skills": skills,
        "certifications": certifications,
        "titles": titles,
        "years": career_years(profile),
    }


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
                cur.execute("SHOW COLUMNS FROM profiles LIKE 'phone'")
                if cur.fetchone():
                    code_col = ""
                    for name in ("phone_country_code", "phone_code", "country_code"):
                        cur.execute("SHOW COLUMNS FROM profiles LIKE %s", (name,))
                        if cur.fetchone():
                            code_col = name
                            break
                    code_sql = f", p.`{code_col}` AS phone_code" if code_col else ", '' AS phone_code"
                    cur.execute(
                        "SELECT u.id AS user_id, p.phone AS phone"
                        f"{code_sql} FROM users u "
                        "JOIN profiles p ON p.user_id = u.id "
                        "WHERE p.phone IS NOT NULL AND TRIM(p.phone) <> ''"
                    )
                    user_rows = list(cur.fetchall() or [])
            cur.execute(f"SHOW TABLES LIKE '{CANDIDATES_TABLE}'")
            if cur.fetchone():
                cur.execute(f"SELECT candidate_id, profile FROM {CANDIDATES_TABLE}")
                candidate_rows = list(cur.fetchall() or [])
    except Exception:
        log.exception("Could not list WhatsApp alert recipients")
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
        built = _recipient_from_profile(profile, number)
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


def _mark_sent(cur, phone: str, source_id: str) -> None:
    cur.execute(
        f"INSERT INTO {ALERTS_TABLE} (phone, source_id, sent_at) "
        "VALUES (%s, %s, UTC_TIMESTAMP(6))",
        (phone, source_id),
    )


def deliver_job_alerts(jobs: list[dict]) -> int:
    """Score new jobs per person and send each match. Returns messages sent."""
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
    if not recipients:
        log.info("WhatsApp alerts skipped: no users with a phone and a matchable profile")
        return 0

    from jobscraper.db import get_conn

    conn = get_conn()
    if conn is None:
        return 0
    minimum = max(1, _int_env("WHATSAPP_MIN_SCORE", 1))
    per_flush = max(1, _int_env("WHATSAPP_MAX_PER_FLUSH", 5))
    per_day = max(1, _int_env("WHATSAPP_MAX_PER_DAY", 15))
    sent = 0
    failed: list[dict] = []
    try:
        with conn.cursor() as cur:
            ensure_alert_table(cur)
            for person in recipients:
                phone = person["phone"]
                already = _sent_ids(cur, phone, [job["source_id"] for job in jobs])
                room = per_day - _sent_today(cur, phone)
                if room <= 0:
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
                for _score, job, match in matched[: min(per_flush, room)]:
                    if send_whatsapp(phone, message_for(job, match)):
                        try:
                            _mark_sent(cur, phone, job["source_id"])
                        except Exception as exc:
                            code = exc.args[0] if getattr(exc, "args", None) else None
                            if code != 1062:
                                raise
                        sent += 1
                        time.sleep(0.35)
                        continue
                    retry = dict(job)
                    retry["_attempts"] = int(job.get("_attempts") or 0) + 1
                    if retry["_attempts"] < 3:
                        failed.append(retry)
    except Exception:
        log.exception("WhatsApp alert delivery failed")
        return sent
    if failed:
        queue_job_alerts(failed)
    if sent:
        log.info("WhatsApp alerts sent %s message(s) for %s new job(s)", sent, len(jobs))
    return sent
