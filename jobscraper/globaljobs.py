"""Saudi listings from the Dev Global Jobs display API.

One pull per minute stays far under their 120 requests per minute per IP cap.
The list call only has an excerpt, so each id is loaded once for the full posting.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("jobscraper.globaljobs")

API_BASE = "https://devglobaljobs.com"
PLATFORM = "Dev Global Jobs"
# Leave room under the published 120 requests per minute per IP.
REQUEST_BUDGET = 100
_LOCK = threading.Lock()
_LAST: dict = {"ok": None, "saved": 0, "fetched": 0, "error": None, "at": None}
_HTML_BREAK = re.compile(r"(?i)<\s*br\s*/?>|</\s*(p|div|li|h[1-6]|tr|ul|ol)\s*>")
_HTML_TAG = re.compile(r"<[^>]+>")


def _env(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


def _enabled() -> bool:
    return _env("GLOBALJOBS_ENABLED", "true").lower() not in {"0", "false", "no", "off"}


def _country() -> str:
    return (_env("GLOBALJOBS_COUNTRY", "sa") or "sa").lower()


def interval_minutes() -> int:
    try:
        minutes = int(_env("GLOBALJOBS_INTERVAL_MINUTES", "1") or "1")
    except ValueError:
        minutes = 1
    return max(1, min(minutes, 60))


def globaljobs_status() -> dict:
    return {
        "enabled": _enabled(),
        "country": _country(),
        "interval_minutes": interval_minutes(),
        "platform": PLATFORM,
        "attribution": "Powered by Dev Global Jobs",
        "attribution_url": API_BASE,
        "last": dict(_LAST),
    }


def _plain(value: str) -> str:
    text = _HTML_BREAK.sub("\n", value or "")
    text = _HTML_TAG.sub(" ", text)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ").replace("&#39;", "'")
    text = text.replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _get_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "JobDiscoveryKSA/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Dev Global Jobs returned a non-object response")
    return payload


def _days_ago(posted: str) -> int:
    if not posted:
        return 0
    try:
        stamp = datetime.fromisoformat(posted.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).days)


def _location(record: dict) -> str:
    location = str(record.get("location") or "").strip()
    country = str(record.get("country") or "").upper()
    if country == "SA" and not re.search(r"saudi|ksa|السعود", location, re.I):
        location = f"{location}, Saudi Arabia" if location else "Saudi Arabia"
    return location or "Saudi Arabia"


def _to_job(record: dict) -> dict | None:
    from jobscraper.locations import parse_state

    job_id = record.get("id")
    title = str(record.get("title") or "").strip()
    if job_id in (None, "") or not title:
        return None
    description = _plain(str(record.get("description") or record.get("descriptionExcerpt") or ""))
    skill = str(record.get("skillCategory") or "").strip()
    location = _location(record)
    return {
        "id": f"devglobaljobs-{job_id}",
        "title": title,
        "company": str(record.get("organization") or "Unknown").strip() or "Unknown",
        "skills": [skill] if skill else [],
        "state": parse_state(location),
        "location": location,
        "platform": PLATFORM,
        "url": str(record.get("url") or f"{API_BASE}/jobs/detail/{job_id}").strip(),
        "description": description,
        "posted_at": str(record.get("postedAt") or ""),
        "daysAgo": _days_ago(str(record.get("postedAt") or "")),
        "job_type": str(record.get("jobType") or ""),
        "raw": record,
    }


def _list_page(country: str, offset: int) -> dict:
    return _get_json(f"{API_BASE}/api/v1/jobs?limit=100&offset={offset}&country={country}")


def pull_records() -> list[dict]:
    """List Saudi jobs, then load each posting. Stops before the per-IP cap."""
    country = _country()
    used = 0
    offset = 0
    summaries: list[dict] = []
    while used < REQUEST_BUDGET:
        page = _list_page(country, offset)
        used += 1
        batch = page.get("jobs") if isinstance(page.get("jobs"), list) else []
        summaries.extend(item for item in batch if isinstance(item, dict))
        meta = page.get("meta") if isinstance(page.get("meta"), dict) else {}
        if not meta.get("hasMore") or not batch:
            break
        offset += 100
    records: list[dict] = []
    for summary in summaries:
        if used >= REQUEST_BUDGET:
            records.append(summary)
            continue
        job_id = summary.get("id")
        if job_id in (None, ""):
            continue
        try:
            detail = _get_json(f"{API_BASE}/api/v1/jobs/{job_id}")
            used += 1
        except Exception:
            log.exception("Dev Global Jobs detail failed for %s", job_id)
            records.append(summary)
            continue
        records.append(detail)
    return records


def sync_global_jobs() -> dict:
    """Pull the feed and upsert it. Safe to run every minute from the scheduler."""
    if not _enabled():
        return {"ok": True, "saved": 0, "fetched": 0, "skipped": True}
    if not _LOCK.acquire(blocking=False):
        log.info("Dev Global Jobs sync already running")
        return {"ok": True, "saved": 0, "fetched": 0, "skipped": True}
    saved = 0
    fetched = 0
    try:
        records = pull_records()
        jobs = [job for job in (_to_job(record) for record in records) if job]
        fetched = len(jobs)
        from jobscraper.db import mysql_configured, upsert_jobs

        if jobs and mysql_configured():
            saved = upsert_jobs(jobs)
        elif jobs:
            log.warning("Dev Global Jobs fetched %s rows but MySQL is not configured", fetched)
        _LAST.update(
            {
                "ok": True,
                "saved": saved,
                "fetched": fetched,
                "error": None,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        log.info("Dev Global Jobs saved %s of %s", saved, fetched)
        return {"ok": True, "saved": saved, "fetched": fetched}
    except Exception as exc:
        _LAST.update(
            {
                "ok": False,
                "saved": saved,
                "fetched": fetched,
                "error": str(exc)[:500],
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        log.exception("Dev Global Jobs sync failed")
        return {"ok": False, "saved": saved, "fetched": fetched, "error": str(exc)[:500]}
    finally:
        _LOCK.release()
