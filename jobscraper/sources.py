"""Job-source on/off switch.

A source with status "inactive" is left out of scheduled scrapes, manual
scrapes, career-page lookups, and description backfill. Status is stored in
MySQL. When MySQL is not configured, data/source_status.json is used instead.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("jobscraper.sources")

_ROOT = Path(__file__).resolve().parent.parent
STATUS_FILE = _ROOT / "data" / "source_status.json"
SOURCES_TABLE = "job_discovery_sources"
_TTL_SECONDS = 2.0
_STATS_TTL_SECONDS = 60.0

CREATE_SOURCES_SQL = f"""
CREATE TABLE IF NOT EXISTS {SOURCES_TABLE} (
  id VARCHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  last_sync_at DATETIME(6) NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

# Order matches the public source list. Extra rows are boards this scraper
# actually reads, so they can be turned off the same way.
CATALOG = (
    {
        "id": "greenhouse",
        "name": "Greenhouse ATS API",
        "detail": "greenhouse.io API v2",
        "integration": "Native API",
        "integrationTone": "pro",
        "action": "Force Sync",
        "platform": "Greenhouse",
    },
    {
        "id": "indeed",
        "name": "Indeed Aggregator",
        "detail": "indeed.com",
        "integration": "Multi-Node",
        "integrationTone": "enterprise",
        "action": "Restart Node",
        "platform": "Indeed",
    },
    {
        "id": "lever",
        "name": "Lever Direct Adapter",
        "detail": "jobs.lever.co",
        "integration": "Native API",
        "integrationTone": "pro",
        "action": "Force Sync",
        "platform": "Lever",
    },
    {
        "id": "linkedin",
        "name": "LinkedIn Job Feeds",
        "detail": "linkedin.com/jobs",
        "integration": "Multi-Node",
        "integrationTone": "enterprise",
        "action": "Force Sync",
        "platform": "LinkedIn",
    },
    {
        "id": "workday",
        "name": "Workday Enterprise Scraper",
        "detail": "myworkdayjobs.com",
        "integration": "Browser Swarm",
        "integrationTone": "neutral",
        "action": "Force Sync",
        "platform": "Workday",
    },
    {
        "id": "yc",
        "name": "Y Combinator Startup Jobs",
        "detail": "ycombinator.com/jobs",
        "integration": "RSS & Web Clip",
        "integrationTone": "pro",
        "action": "Force Sync",
        "platform": "Y Combinator",
    },
    {
        "id": "google",
        "name": "Google Jobs",
        "detail": "google.com/search",
        "integration": "Multi-Node",
        "integrationTone": "enterprise",
        "action": "Force Sync",
        "platform": "Google Jobs",
    },
    {
        "id": "bayt",
        "name": "Bayt",
        "detail": "bayt.com",
        "integration": "Multi-Node",
        "integrationTone": "enterprise",
        "action": "Force Sync",
        "platform": "Bayt",
    },
    {
        "id": "smartrecruiters",
        "name": "SmartRecruiters",
        "detail": "jobs.smartrecruiters.com",
        "integration": "Native API",
        "integrationTone": "pro",
        "action": "Force Sync",
        "platform": "SmartRecruiters",
    },
    {
        "id": "globaljobs",
        "name": "Dev Global Jobs",
        "detail": "devglobaljobs.com",
        "integration": "Native API",
        "integrationTone": "pro",
        "action": "Force Sync",
        "platform": "Dev Global Jobs",
    },
    {
        "id": "headcount",
        "name": "Headcount Labs",
        "detail": "api.headcountlabs.com",
        "integration": "Native API",
        "integrationTone": "pro",
        "action": "Force Sync",
        "platform": "Headcount Labs",
    },
)

BY_ID = {row["id"]: row for row in CATALOG}
PLATFORM_TO_ID = {row["platform"]: row["id"] for row in CATALOG}
ATS_SOURCE_IDS = ("greenhouse", "lever", "smartrecruiters", "workday")

_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}
_LOADED_AT = 0.0
_TABLE_READY = False
_STATS: dict[str, dict] = {}
_STATS_AT = 0.0


def envelope(message: str, data, *, success: bool = True, errors=None) -> dict:
    return {
        "success": success,
        "message": message,
        "data": data,
        "meta": None,
        "errors": errors,
        "requestId": str(uuid.uuid4()),
    }


def is_active(source_id: str) -> bool:
    """Unknown ids stay on. Only a catalog source can be switched off."""
    if source_id not in BY_ID:
        return True
    return _status(source_id) == "active"


def is_platform_active(platform: str) -> bool:
    source_id = PLATFORM_TO_ID.get(platform or "")
    if not source_id:
        return True
    return is_active(source_id)


def active_site_ids(sites: list[str]) -> list[str]:
    return [site for site in sites if is_active(site)]


def filter_sites(sites: list[str]) -> list[str]:
    kept = []
    for site in sites:
        if is_active(site):
            kept.append(site)
            continue
        log.info("Skipping inactive job source %s", site)
    return kept


def inactive_source_ids() -> list[str]:
    return [row["id"] for row in CATALOG if not is_active(row["id"])]


def list_sources() -> list[dict]:
    stored = _rows()
    activity = _activity()
    return [_public(row, stored.get(row["id"]) or {}, activity.get(row["platform"]) or {}) for row in CATALOG]


def get_source(source_id: str) -> dict | None:
    spec = BY_ID.get((source_id or "").strip().lower())
    if not spec:
        return None
    stored = _rows()
    activity = _activity()
    return _public(spec, stored.get(spec["id"]) or {}, activity.get(spec["platform"]) or {})


def set_source_status(source_id: str, status: str) -> dict | None:
    spec = BY_ID.get((source_id or "").strip().lower())
    if not spec:
        return None
    cleaned = str(status or "").strip().lower()
    if cleaned not in {"active", "inactive"}:
        raise ValueError("status must be active or inactive")
    previous = _rows().get(spec["id"]) or {}
    _write_status(spec["id"], cleaned, previous.get("last_sync_at"))
    log.info("Job source %s is now %s", spec["id"], cleaned)
    return get_source(spec["id"])


def touch_last_sync(source_id: str) -> None:
    """Remember that this source was just read. Failures here do not stop a scrape."""
    if source_id not in BY_ID:
        return
    try:
        _write_sync(source_id)
    except Exception:
        log.exception("Could not record last sync for %s", source_id)


def _public(spec: dict, stored: dict, activity: dict) -> dict:
    new_jobs = int(activity.get("new_jobs") or 0)
    repeats = int(activity.get("repeats") or 0)
    total = new_jobs + repeats
    rate = round(100 * repeats / total) if total else 0
    if rate < 25:
        tone = "good"
    elif rate < 50:
        tone = "neutral"
    else:
        tone = "bad"
    label = "1 job" if new_jobs == 1 else f"{new_jobs} jobs"
    return {
        "id": spec["id"],
        "name": spec["name"],
        "detail": spec["detail"],
        "integration": spec["integration"],
        "integrationTone": spec["integrationTone"],
        "status": stored.get("status") or "active",
        "jobsPerDay": label,
        "duplicateRate": f"{rate}%",
        "duplicateTone": tone,
        "lastSync": _iso(stored.get("last_sync_at")),
        "action": spec["action"],
    }


def _status(source_id: str) -> str:
    row = _rows().get(source_id) or {}
    status = str(row.get("status") or "active").strip().lower()
    return status if status in {"active", "inactive"} else "active"


def _rows() -> dict[str, dict]:
    global _LOADED_AT
    now = time.monotonic()
    with _LOCK:
        if _LOADED_AT and now - _LOADED_AT < _TTL_SECONDS:
            return dict(_CACHE)
        try:
            loaded = _read_store()
        except Exception:
            log.exception("Could not read job source status")
            if _LOADED_AT:
                return dict(_CACHE)
            return {}
        _CACHE.clear()
        _CACHE.update(loaded)
        _LOADED_AT = now
        return dict(_CACHE)


def _remember(source_id: str, status: str, last_sync_at) -> None:
    global _LOADED_AT
    with _LOCK:
        current = dict(_CACHE.get(source_id) or {})
        current["status"] = status
        current["last_sync_at"] = last_sync_at
        current["updated_at"] = datetime.now(timezone.utc)
        _CACHE[source_id] = current
        _LOADED_AT = time.monotonic()


def _read_store() -> dict[str, dict]:
    from jobscraper.db import get_conn, mysql_configured

    if not mysql_configured():
        return _read_file()
    conn = get_conn()
    if conn is None:
        return _read_file()
    with conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(f"SELECT id, status, last_sync_at, updated_at FROM {SOURCES_TABLE}")
        found = {}
        for row in cur.fetchall() or []:
            source_id = str(row.get("id") or "")
            if source_id in BY_ID:
                found[source_id] = row
        return found


def _write_status(source_id: str, status: str, last_sync_at) -> None:
    from jobscraper.db import close_conn, get_conn, mysql_configured

    if mysql_configured():
        conn = get_conn()
        if conn is None:
            raise RuntimeError("MySQL is not connected")
        try:
            with conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    f"INSERT INTO {SOURCES_TABLE} (id, status, last_sync_at, updated_at) "
                    "VALUES (%s, %s, %s, UTC_TIMESTAMP(6)) "
                    "ON DUPLICATE KEY UPDATE status = VALUES(status), updated_at = UTC_TIMESTAMP(6)",
                    (source_id, status, _naive_utc(last_sync_at)),
                )
        except Exception:
            close_conn()
            raise
        _remember(source_id, status, last_sync_at)
        return
    stored = _read_file()
    current = dict(stored.get(source_id) or {})
    current["status"] = status
    current["last_sync_at"] = _iso(last_sync_at)
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    stored[source_id] = current
    _write_file(stored)
    _remember(source_id, status, last_sync_at)


def _write_sync(source_id: str) -> None:
    from jobscraper.db import close_conn, get_conn, mysql_configured

    synced_at = datetime.now(timezone.utc)
    if mysql_configured():
        conn = get_conn()
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    f"INSERT INTO {SOURCES_TABLE} (id, status, last_sync_at, updated_at) "
                    "VALUES (%s, 'active', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)) "
                    "ON DUPLICATE KEY UPDATE last_sync_at = UTC_TIMESTAMP(6), updated_at = UTC_TIMESTAMP(6)",
                    (source_id,),
                )
                cur.execute(
                    f"SELECT status, last_sync_at FROM {SOURCES_TABLE} WHERE id = %s",
                    (source_id,),
                )
                row = cur.fetchone() or {}
        except Exception:
            close_conn()
            raise
        _remember(source_id, row.get("status") or "active", row.get("last_sync_at") or synced_at)
        return
    stored = _read_file()
    current = dict(stored.get(source_id) or {"status": "active"})
    current["last_sync_at"] = synced_at.isoformat()
    current["updated_at"] = synced_at.isoformat()
    stored[source_id] = current
    _write_file(stored)
    _remember(source_id, current.get("status") or "active", synced_at)


def _ensure_table(cur) -> None:
    global _TABLE_READY
    if _TABLE_READY:
        return
    cur.execute(CREATE_SOURCES_SQL)
    _TABLE_READY = True


def _read_file() -> dict[str, dict]:
    try:
        raw = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    found = {}
    for source_id, row in raw.items():
        if source_id not in BY_ID or not isinstance(row, dict):
            continue
        found[source_id] = {
            "id": source_id,
            "status": row.get("status") or "active",
            "last_sync_at": row.get("last_sync_at"),
            "updated_at": row.get("updated_at"),
        }
    return found


def _write_file(stored: dict[str, dict]) -> None:
    STATUS_FILE.parent.mkdir(exist_ok=True)
    payload = {}
    for source_id, row in stored.items():
        if source_id not in BY_ID:
            continue
        payload[source_id] = {
            "status": row.get("status") or "active",
            "last_sync_at": _iso(row.get("last_sync_at")),
            "updated_at": _iso(row.get("updated_at")),
        }
    STATUS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _activity() -> dict[str, dict]:
    global _STATS, _STATS_AT
    now = time.monotonic()
    with _LOCK:
        if _STATS_AT and now - _STATS_AT < _STATS_TTL_SECONDS:
            return dict(_STATS)
    loaded = _query_activity()
    with _LOCK:
        _STATS = loaded
        _STATS_AT = time.monotonic()
        return dict(_STATS)


def _query_activity() -> dict[str, dict]:
    from jobscraper.db import JOBS_TABLE, close_conn, get_conn, mysql_configured

    if not mysql_configured():
        return {}
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    try:
        conn = get_conn()
        if conn is None:
            return {}
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT platform, "
                "SUM(CASE WHEN first_seen_at >= %s THEN 1 ELSE 0 END) AS new_jobs, "
                "SUM(CASE WHEN last_seen_at >= %s AND (first_seen_at IS NULL OR first_seen_at < %s) THEN 1 ELSE 0 END) AS repeats "
                f"FROM {JOBS_TABLE} "
                "WHERE first_seen_at >= %s OR last_seen_at >= %s "
                "GROUP BY platform",
                (cutoff, cutoff, cutoff, cutoff, cutoff),
            )
            rows = cur.fetchall() or []
    except Exception:
        close_conn()
        log.exception("Could not count jobs per source")
        return {}
    found = {}
    for row in rows:
        platform = str(row.get("platform") or "")
        if platform:
            found[platform] = {
                "new_jobs": int(row.get("new_jobs") or 0),
                "repeats": int(row.get("repeats") or 0),
            }
    return found


def _naive_utc(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _iso(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    text = str(value).strip()
    return text or None
