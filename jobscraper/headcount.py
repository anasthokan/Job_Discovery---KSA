"""Saudi jobs from the Headcount Labs API, free tier only.

The free allowance is 100,000 records per UTC month. A job delivered again
counts again. This feed never calls ``/v1/jobs/changes``: that feed is global
and would spend the month in a few hours. It lists current Saudi jobs
(``country=SA``), then later asks only for rows changed since the last
catalog watermark. A local cap stops the pull before Headcount's hard limit.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("jobscraper.headcount")

API_BASE = "https://api.headcountlabs.com"
PLATFORM = "Headcount Labs"
FREE_LIMIT = 100_000
# Stay under the published 30 requests / 60 seconds on the free allowance.
_REQUEST_GAP_SECONDS = 2.2
# Leave room under the free allowance's 1,000 requests per UTC day.
_REQUEST_DAILY_BUDGET = 900
_PAGE_LIMIT = 250
_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "headcount_state.json"

_SYNC_LOCK = threading.Lock()
_PACE_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
_LAST: dict = {
    "ok": None,
    "saved": 0,
    "fetched": 0,
    "charged": 0,
    "mode": None,
    "error": None,
    "skipped": None,
    "at": None,
}


class HeadcountError(Exception):
    def __init__(self, message: str, *, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _enabled() -> bool:
    return _env("HEADCOUNT_ENABLED", "true").lower() not in {"0", "false", "no", "off"}


def _api_key() -> str:
    return _env("HEADCOUNT_API_KEY")


def _country() -> str:
    code = (_env("HEADCOUNT_COUNTRY", "SA") or "SA").upper()
    return code if len(code) == 2 and code.isalpha() else "SA"


def _record_cap() -> int:
    try:
        cap = int(_env("HEADCOUNT_RECORD_CAP", "95000") or "95000")
    except ValueError:
        cap = 95000
    return max(1000, min(cap, FREE_LIMIT - 1000))


def _max_pull() -> int:
    try:
        limit = int(_env("HEADCOUNT_MAX_PULL", "30000") or "30000")
    except ValueError:
        limit = 30000
    return max(1, min(limit, _record_cap()))


def _refresh_days() -> int:
    try:
        days = int(_env("HEADCOUNT_FULL_REFRESH_DAYS", "7") or "7")
    except ValueError:
        days = 7
    return max(1, min(days, 28))


def interval_minutes() -> int:
    try:
        minutes = int(_env("HEADCOUNT_INTERVAL_MINUTES", "60") or "60")
    except ValueError:
        minutes = 60
    # A one-minute poll would burn the 1,000 requests/day allowance on counts alone.
    return max(30, min(minutes, 24 * 60))


def scheduled() -> bool:
    return _enabled() and bool(_api_key())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _state_path() -> Path:
    return _STATE_PATH


def _blank_state() -> dict:
    now = _utc_now()
    return {
        "utc_month": now.strftime("%Y-%m"),
        "utc_day": now.strftime("%Y-%m-%d"),
        "records_used": 0,
        "records_remaining": None,
        "records_limit": FREE_LIMIT,
        "requests_today": 0,
        "last_watermark": None,
        "last_full_at": None,
        "hold_until": None,
        "last_mode": None,
        "last_error": None,
    }


def _load_state() -> dict:
    state = _blank_state()
    path = _state_path()
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            saved = {}
        if isinstance(saved, dict):
            state.update(saved)
    now = _utc_now()
    month = now.strftime("%Y-%m")
    day = now.strftime("%Y-%m-%d")
    if state.get("utc_month") != month:
        state["utc_month"] = month
        state["records_used"] = 0
        state["records_remaining"] = None
    if state.get("utc_day") != day:
        state["utc_day"] = day
        state["requests_today"] = 0
    return state


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _remaining_budget(state: dict) -> int:
    used = int(state.get("records_used") or 0)
    budget = _record_cap() - used
    remaining = state.get("records_remaining")
    if isinstance(remaining, int):
        # Keep the last 5,000 of the provider allowance untouched.
        budget = min(budget, remaining - (FREE_LIMIT - _record_cap()))
    return max(0, budget)


def _hold_active(state: dict) -> bool:
    until = _parse_time(state.get("hold_until"))
    return until is not None and _utc_now() < until


def choose_pull(
    full_count: int,
    delta: int | None,
    *,
    has_full: bool,
    days_since_full: float,
    remaining: int,
    max_pull: int,
    refresh_days: int,
) -> str:
    """Decide what this run may download without leaving the free allowance.

    ``full`` reads the current Saudi catalog. ``incremental`` reads only rows
    changed since the last watermark. ``empty`` means nothing changed.
    ``noise`` means the change window is most of the catalog, which would
    bill the same jobs again. ``budget`` and ``too_large`` mean do not call
    the list endpoint.
    """
    if full_count > max_pull:
        return "too_large"
    if not has_full:
        return "full" if full_count <= remaining else "budget"
    if delta is None or delta <= 0:
        return "empty"
    noisy = full_count > 0 and delta >= int(full_count * 0.5)
    if noisy:
        if days_since_full >= refresh_days and full_count <= remaining:
            return "full"
        return "noise"
    if delta > remaining or delta > max_pull:
        return "budget"
    return "incremental"


def headcount_status() -> dict:
    state = _load_state()
    last_full = _parse_time(state.get("last_full_at"))
    return {
        "enabled": _enabled(),
        "configured": bool(_api_key()),
        "country": _country(),
        "platform": PLATFORM,
        "interval_minutes": interval_minutes(),
        "free_limit": FREE_LIMIT,
        "record_cap": _record_cap(),
        "max_pull": _max_pull(),
        "records_used": int(state.get("records_used") or 0),
        "records_remaining": state.get("records_remaining"),
        "utc_month": state.get("utc_month"),
        "requests_today": int(state.get("requests_today") or 0),
        "request_daily_budget": _REQUEST_DAILY_BUDGET,
        "last_watermark": state.get("last_watermark"),
        "last_full_at": last_full.isoformat() if last_full else None,
        "hold_until": state.get("hold_until"),
        "last_mode": state.get("last_mode"),
        "last_error": state.get("last_error"),
        "changes_feed": False,
        "last": dict(_LAST),
    }


def _known(field):
    if isinstance(field, dict):
        if field.get("state") in {"known", "derived"}:
            return field.get("value")
        if "state" in field:
            return None
        return field
    if isinstance(field, str) and field.strip():
        return field.strip()
    return None


def _location_text(record: dict) -> str | None:
    arrangement = str(_known(record.get("work_arrangement")) or "")
    cities: list[str] = []
    saw_other_country = False
    saw_saudi = False
    for item in record.get("locations") or []:
        value = _known(item)
        if not isinstance(value, dict):
            continue
        country = str(value.get("country") or "").upper()
        if country and country != "SA":
            saw_other_country = True
            continue
        if country == "SA" or not country:
            saw_saudi = True
        city = str(value.get("city") or "").strip()
        if city and city not in cities:
            cities.append(city)
    if saw_other_country and not saw_saudi and not cities:
        return None
    place = ", ".join(cities[:3])
    if arrangement == "remote" and not place:
        return "Remote, Saudi Arabia"
    if arrangement == "remote":
        return f"{place}, Saudi Arabia · Remote"
    if arrangement == "hybrid" and place:
        return f"{place}, Saudi Arabia · Hybrid"
    if arrangement == "hybrid":
        return "Hybrid, Saudi Arabia"
    if place:
        return f"{place}, Saudi Arabia"
    return "Saudi Arabia"


def _posting_parts(record: dict) -> tuple[str, str, str, dict | None]:
    postings = [item for item in (record.get("postings") or []) if isinstance(item, dict)]
    listed = [item for item in postings if item.get("posting_status") == "listed"] or postings
    url = ""
    posted = ""
    source = ""
    attribution = None
    for posting in listed:
        if not source:
            source = str(posting.get("source") or "").strip()
        if not posted:
            posted = str(posting.get("source_posted_at") or "").strip()
        if attribution is None and isinstance(posting.get("attribution"), dict):
            attribution = posting.get("attribution")
        if url:
            continue
        for key in ("application_url", "posting_url"):
            candidate = _known(posting.get(key))
            if isinstance(candidate, str) and candidate.startswith("http"):
                url = candidate.strip()
                break
    return url, posted, source, attribution


def _days_ago(value: str) -> int:
    stamp = _parse_time(value)
    if stamp is None:
        return 0
    return max(0, int((_utc_now() - stamp).total_seconds() // 86400))


def to_job(record: dict) -> dict | None:
    """Map one Headcount job into the shape ``upsert_jobs`` stores."""
    from jobscraper.locations import parse_state

    if not isinstance(record, dict):
        return None
    if str(record.get("currentness") or "current") != "current":
        return None
    job_id = str(record.get("job_id") or "").strip()
    title = _known(record.get("title"))
    if not job_id or not isinstance(title, str) or not title.strip():
        return None
    location = _location_text(record)
    if not location:
        return None
    url, posted, source, attribution = _posting_parts(record)
    if not url:
        return None
    company = _known(record.get("employer_name"))
    if not isinstance(company, str) or not company.strip():
        company = "Unknown"
    description = _known(record.get("description"))
    description = description.strip() if isinstance(description, str) else ""
    if isinstance(attribution, dict) and attribution.get("required") and attribution.get("text"):
        credit = str(attribution.get("text") or "").strip()
        link = str(attribution.get("link") or "").strip()
        if link and link not in credit:
            credit = f"{credit} {link}".strip()
        if credit:
            description = f"{description}\n\n{credit}".strip()
    posted_at = posted or str(record.get("job_first_seen_at") or "")
    pay = None
    for posting in record.get("postings") or []:
        if isinstance(posting, dict):
            pay = _known(posting.get("pay"))
            if pay:
                break
    return {
        "id": f"headcount-{job_id}"[:255],
        "title": title.strip(),
        "company": company.strip(),
        "skills": [],
        "state": parse_state(location),
        "location": location,
        "platform": PLATFORM,
        "url": url,
        "description": description,
        "posted_at": posted_at,
        "platform_posted_at": posted_at,
        "daysAgo": _days_ago(posted_at),
        "raw": {
            "headcount_job_id": job_id,
            "source": source,
            "work_arrangement": _known(record.get("work_arrangement")),
            "pay": pay,
            "attribution": attribution,
            "job_updated_at": record.get("job_updated_at"),
            "currentness": record.get("currentness") or "current",
        },
    }


def _pace() -> None:
    global _LAST_REQUEST_AT
    with _PACE_LOCK:
        wait = _REQUEST_GAP_SECONDS - (time.monotonic() - _LAST_REQUEST_AT)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_AT = time.monotonic()


def _header_int(headers: dict, name: str) -> int | None:
    raw = headers.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _apply_allowance(state: dict, headers: dict, fallback: int) -> int:
    charged = _header_int(headers, "x-data-records-charged")
    if charged is None:
        charged = max(0, fallback)
    used = _header_int(headers, "x-data-records-used")
    if used is None:
        state["records_used"] = int(state.get("records_used") or 0) + charged
    else:
        state["records_used"] = used
    remaining = _header_int(headers, "x-data-records-remaining")
    if remaining is not None:
        state["records_remaining"] = remaining
    limit = _header_int(headers, "x-data-records-limit")
    if limit is not None:
        state["records_limit"] = limit
    return charged


def _request(path: str, params: dict, state: dict, *, allow_retry: bool = True) -> tuple[dict, dict]:
    if int(state.get("requests_today") or 0) >= _REQUEST_DAILY_BUDGET:
        raise HeadcountError("daily request budget reached")
    _pace()
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value not in (None, "")})
    url = f"{API_BASE}{path}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {_api_key()}",
            "User-Agent": "JobDiscoveryKSA/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
            headers = {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        headers = {key.lower(): value for key, value in exc.headers.items()}
        error = payload.get("error") if isinstance(payload, dict) else {}
        code = str(error.get("code") or "") if isinstance(error, dict) else ""
        retry_after = _header_int(headers, "retry-after") or 0
        if exc.code != 429:
            state["requests_today"] = int(state.get("requests_today") or 0) + 1
            _save_state(state)
        if exc.code == 429 and code == "data_limit_exceeded":
            state["records_used"] = max(int(state.get("records_used") or 0), _record_cap())
            state["records_remaining"] = 0
            raise HeadcountError("free monthly allowance is used", status=429, code=code)
        if exc.code == 429 and allow_retry and 0 < retry_after <= 90:
            time.sleep(retry_after)
            return _request(path, params, state, allow_retry=False)
        if exc.code in {503, 504} and allow_retry:
            time.sleep(3)
            return _request(path, params, state, allow_retry=False)
        if exc.code == 401:
            raise HeadcountError("Headcount API key was rejected", status=401, code=code or "unauthorized")
        raise HeadcountError(
            f"Headcount HTTP {exc.code} {code}".strip(),
            status=exc.code,
            code=code,
        )
    except urllib.error.URLError as exc:
        raise HeadcountError(f"Headcount request failed: {exc.reason}") from exc
    state["requests_today"] = int(state.get("requests_today") or 0) + 1
    if not isinstance(payload, dict):
        raise HeadcountError("Headcount returned a non-object response")
    return payload, headers


def _count(params: dict, state: dict) -> tuple[int, str | None]:
    payload, headers = _request("/v1/jobs/count", params, state)
    _apply_allowance(state, headers, 0)
    try:
        count = int(payload.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    watermark = payload.get("snapshot_watermark")
    return count, str(watermark) if watermark else None


def _days_since_full(state: dict) -> float:
    stamp = _parse_time(state.get("last_full_at"))
    if stamp is None:
        return 10**6
    return (_utc_now() - stamp).total_seconds() / 86400


def _pull(params: dict, state: dict) -> tuple[list[dict], int, str | None, bool]:
    jobs: list[dict] = []
    charged = 0
    watermark = None
    cursor = None
    complete = True
    seen_cursors: set[str] = set()
    for _page in range(1, 120):
        if _remaining_budget(state) <= 0:
            complete = False
            break
        page_params = dict(params)
        page_params["limit"] = _PAGE_LIMIT
        if cursor:
            page_params["cursor"] = cursor
        try:
            payload, headers = _request("/v1/jobs", page_params, state)
        except HeadcountError as exc:
            if exc.status == 400 and cursor:
                log.warning("Headcount catalog snapshot changed; stopping this pull")
                complete = False
                break
            raise
        batch = payload.get("data") if isinstance(payload.get("data"), list) else []
        charged += _apply_allowance(state, headers, len(batch))
        _save_state(state)
        for record in batch:
            job = to_job(record)
            if job:
                jobs.append(job)
        watermark = payload.get("snapshot_watermark") or watermark
        cursor = payload.get("next_cursor")
        if not cursor:
            break
        if cursor in seen_cursors:
            complete = False
            break
        seen_cursors.add(str(cursor))
    else:
        complete = False
    return jobs, charged, str(watermark) if watermark else None, complete


def _remember(state: dict, *, mode: str | None, error: str | None) -> None:
    state["last_mode"] = mode
    state["last_error"] = error
    _save_state(state)


def _finish(result: dict) -> dict:
    _LAST.update(result)
    return result


def sync_headcount() -> dict:
    """Pull Saudi Headcount jobs and upsert them. Safe to run from the scheduler."""
    from jobscraper.sources import is_active, touch_last_sync

    if not is_active("headcount"):
        log.info("Headcount sync skipped: source is inactive")
        return _finish({"ok": True, "saved": 0, "fetched": 0, "charged": 0, "mode": None, "error": None, "skipped": "inactive", "at": _utc_now().isoformat()})
    if not _enabled():
        return _finish({"ok": True, "saved": 0, "fetched": 0, "charged": 0, "mode": None, "error": None, "skipped": "disabled", "at": _utc_now().isoformat()})
    if not _api_key():
        return _finish({"ok": True, "saved": 0, "fetched": 0, "charged": 0, "mode": None, "error": None, "skipped": "missing_api_key", "at": _utc_now().isoformat()})
    if not _SYNC_LOCK.acquire(blocking=False):
        log.info("Headcount sync already running")
        return {"ok": True, "saved": 0, "fetched": 0, "charged": 0, "skipped": "running"}
    state = _blank_state()
    loaded = False
    mode = None
    saved = 0
    fetched = 0
    charged = 0
    try:
        state = _load_state()
        loaded = True
        if _hold_active(state):
            return _finish({
                "ok": True,
                "saved": 0,
                "fetched": 0,
                "charged": 0,
                "mode": None,
                "error": None,
                "skipped": "hold",
                "at": _utc_now().isoformat(),
            })
        if int(state.get("requests_today") or 0) >= _REQUEST_DAILY_BUDGET:
            _remember(state, mode=state.get("last_mode"), error="daily Headcount request budget reached")
            return _finish({
                "ok": True,
                "saved": 0,
                "fetched": 0,
                "charged": 0,
                "mode": None,
                "error": state.get("last_error"),
                "skipped": "requests",
                "at": _utc_now().isoformat(),
            })
        if _remaining_budget(state) <= 0:
            _remember(state, mode=state.get("last_mode"), error="free-tier record cap reached; waiting for the next UTC month")
            return _finish({
                "ok": True,
                "saved": 0,
                "fetched": 0,
                "charged": 0,
                "mode": None,
                "error": state.get("last_error"),
                "skipped": "cap",
                "at": _utc_now().isoformat(),
            })

        touch_last_sync("headcount")
        country = _country()
        base = {"country": country, "currentness": "current"}
        full_count, full_watermark = _count(base, state)
        _save_state(state)
        delta = None
        delta_watermark = None
        since = state.get("last_watermark") or state.get("last_full_at")
        if state.get("last_full_at") and since:
            delta, delta_watermark = _count({**base, "updated_since": since}, state)
            _save_state(state)
        decision = choose_pull(
            full_count,
            delta,
            has_full=bool(state.get("last_full_at")),
            days_since_full=_days_since_full(state),
            remaining=_remaining_budget(state),
            max_pull=_max_pull(),
            refresh_days=_refresh_days(),
        )
        if decision == "empty":
            if delta_watermark:
                state["last_watermark"] = delta_watermark
            _remember(state, mode="empty", error=None)
            log.info("Headcount has no Saudi changes since %s", since)
            return _finish({
                "ok": True,
                "saved": 0,
                "fetched": 0,
                "charged": 0,
                "mode": "empty",
                "error": None,
                "skipped": None,
                "at": _utc_now().isoformat(),
            })
        if decision in {"noise", "budget", "too_large"}:
            reason = {
                "noise": "change window is most of the Saudi catalog; skipped so the same jobs are not billed again",
                "budget": "this pull would pass the free-tier cap",
                "too_large": f"catalog is above the {_max_pull()} job free-tier pull limit",
            }[decision]
            _remember(state, mode=decision, error=reason)
            log.info("Headcount skip (%s): %s", decision, reason)
            return _finish({
                "ok": True,
                "saved": 0,
                "fetched": 0,
                "charged": 0,
                "mode": decision,
                "error": reason,
                "skipped": decision,
                "at": _utc_now().isoformat(),
            })

        mode = "full" if decision == "full" else "incremental"
        params = dict(base)
        if mode == "incremental" and since:
            params["updated_since"] = since
        first_load = not state.get("last_full_at")
        jobs, charged, watermark, complete = _pull(params, state)
        fetched = len(jobs)
        from jobscraper.db import mysql_configured, upsert_jobs

        if jobs and mysql_configured():
            for start in range(0, len(jobs), 400):
                saved += upsert_jobs(
                    jobs[start : start + 400],
                    notify=not (mode == "full" and first_load),
                ).saved
        elif jobs:
            log.warning("Headcount fetched %s rows but MySQL is not configured", fetched)
        if complete:
            state["last_watermark"] = watermark or (full_watermark if mode == "full" else delta_watermark)
            state["hold_until"] = None
            if mode == "full":
                state["last_full_at"] = state.get("last_watermark") or _utc_now().isoformat()
            _remember(state, mode=mode, error=None)
        else:
            state["hold_until"] = (_utc_now() + timedelta(hours=6)).isoformat()
            _remember(state, mode=mode, error="pull stopped early; retry after hold")
        log.info("Headcount %s saved %s of %s (charged %s, complete=%s)", mode, saved, fetched, charged, complete)
        return _finish({
            "ok": True,
            "saved": saved,
            "fetched": fetched,
            "charged": charged,
            "mode": mode,
            "error": state.get("last_error"),
            "skipped": None if complete else "incomplete",
            "at": _utc_now().isoformat(),
        })
    except HeadcountError as exc:
        if loaded:
            _remember(state, mode=mode, error=str(exc)[:500])
        log.warning("Headcount sync failed: %s", exc)
        return _finish({
            "ok": False,
            "saved": saved,
            "fetched": fetched,
            "charged": charged,
            "mode": mode,
            "error": str(exc)[:500],
            "skipped": None,
            "at": _utc_now().isoformat(),
        })
    except Exception as exc:
        if loaded:
            _remember(state, mode=mode, error=str(exc)[:500])
        log.exception("Headcount sync failed")
        return _finish({
            "ok": False,
            "saved": saved,
            "fetched": fetched,
            "charged": charged,
            "mode": mode,
            "error": str(exc)[:500],
            "skipped": None,
            "at": _utc_now().isoformat(),
        })
    finally:
        _SYNC_LOCK.release()
