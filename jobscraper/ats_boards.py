"""Saudi jobs from public Greenhouse, Lever, SmartRecruiters, and Workday boards.

These hosts are company career sites, not a country-wide search. Each run reads
the boards below and keeps postings whose location is in Saudi Arabia.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

log = logging.getLogger("jobscraper.ats_boards")

UA = "JobDiscoveryKSA/1.0"
PLATFORMS = ("Greenhouse", "Lever", "SmartRecruiters", "Workday")
_DETAIL_CAP = 30
_LOCK = threading.Lock()
_LAST: dict = {
    "ok": None,
    "saved": 0,
    "fetched": 0,
    "boards": 0,
    "error": None,
    "at": None,
}
_HTML_BREAK = re.compile(r"(?i)<\s*br\s*/?>|</\s*(p|div|li|h[1-6]|tr|ul|ol)\s*>")
_HTML_TAG = re.compile(r"<[^>]+>")

# Tokens that are live, or Saudi employers worth checking when a role opens.
GREENHOUSE = (
    "tamara",
    "thunes",
    "elastic",
    "lucidmotors",
    "gitlab",
    "datadog",
    "alixpartners",
    "scaleai",
    "andurilindustries",
    "purestorage",
    "careem",
    "foodics",
    "tabby",
    "unifonic",
    "jahez",
    "salla",
    "moyasar",
    "mozn",
    "nana",
    "mrsool",
    "qlub",
    "rewaa",
    "hungerstation",
    "noon",
    "floward",
    "geidea",
    "sary",
    "kitopi",
    "talabat",
    "deliveryhero",
    "soum",
    "zid",
    "hala",
    "jisr",
    "zenhr",
    "bayzat",
    "namshi",
    "swvl",
    "vezeeta",
    "lean",
    "propertyfinder",
    "instashop",
    "deliveroo",
    "thechefz",
    "toyou",
    "paytabs",
    "hyperpay",
    "nearpay",
    "retailo",
    "eyewa",
    "stcpay",
    "barq",
    "brkz",
    "wasalt",
    "stripe",
    "snowflake",
    "databricks",
    "cloudflare",
    "mongodb",
    "okta",
    "nvidia",
    "canva",
    "deel",
    "rippling",
    "notion",
    "coinbase",
    "grafana",
    "confluent",
    "sentry",
    "grab",
    "spotify",
    "revolut",
    "klarna",
    "adyen",
    "wise",
    "hashicorp",
    "reddit",
    "discord",
    "plaid",
    "wizinc",
    "beyondtrust",
    "minio",
    "crowdstrike",
    "palantir",
    "checkout",
    "remotecom",
)

LEVER = (
    "soum",
    "trendyol",
    "lalamove",
    "tamara",
    "tabby",
    "foodics",
    "careem",
    "noon",
    "jahez",
    "floward",
    "kitopi",
    "swvl",
    "bayzat",
    "propertyfinder",
    "namshi",
    "vezeeta",
    "unifonic",
    "hungerstation",
    "salla",
    "geidea",
)

SMARTRECRUITERS = (
    ("sutherland", "Sutherland"),
    ("CheckPointSoftwareTechnologies2", "Check Point"),
    ("ServiceNow", "ServiceNow"),
    ("NielsenIQ", "NielsenIQ"),
    ("DigitalVirgo", "Digital Virgo"),
    ("IFS1", "IFS"),
    ("AlMosafer", "Almosafer"),
    ("AECOM2", "AECOM"),
    ("AccorHotel", "Accor"),
    ("EgisGroup", "Egis"),
    ("rhg", "Radisson Hotel Group"),
    ("etihadairways5", "Etihad Airways"),
)

WORKDAY = (
    {"company": "Accenture", "tenant": "accenture", "cluster": "wd103", "site": "AccentureCareers"},
    {"company": "Adobe", "tenant": "adobe", "cluster": "wd5", "site": "external_experienced"},
    {"company": "Intel", "tenant": "intel", "cluster": "wd1", "site": "External"},
    {"company": "JD.com", "tenant": "jd", "cluster": "wd103", "site": "Careers_at_JD"},
    {"company": "Air Products", "tenant": "airproducts", "cluster": "wd5", "site": "AP0001"},
    {"company": "Deutsche Bank", "tenant": "db", "cluster": "wd3", "site": "dbwebsite"},
    {"company": "PwC", "tenant": "pwc", "cluster": "wd3", "site": "Global_Experienced_Careers"},
    {"company": "ABB", "tenant": "abb", "cluster": "wd3", "site": "external_career_page"},
    {"company": "Abbott", "tenant": "abbott", "cluster": "wd5", "site": "abbottcareers"},
    {"company": "AstraZeneca", "tenant": "astrazeneca", "cluster": "wd3", "site": "Careers"},
    {"company": "Baker Hughes", "tenant": "bakerhughes", "cluster": "wd5", "site": "BakerHughes"},
    {"company": "Carrier", "tenant": "carrier", "cluster": "wd5", "site": "jobs"},
    {"company": "Cigna", "tenant": "cigna", "cluster": "wd5", "site": "cignacareers"},
    {"company": "Cisco", "tenant": "cisco", "cluster": "wd5", "site": "Cisco_Careers"},
    {"company": "Clarios", "tenant": "clarios", "cluster": "wd5", "site": "clarioscareers"},
    {"company": "IQVIA", "tenant": "iqvia", "cluster": "wd1", "site": "IQVIA"},
    {"company": "Mastercard", "tenant": "mastercard", "cluster": "wd1", "site": "CorporateCareers"},
    {"company": "Medtronic", "tenant": "medtronic", "cluster": "wd1", "site": "MedtronicCareers"},
    {"company": "Novartis", "tenant": "novartis", "cluster": "wd3", "site": "Novartis_Careers"},
    {"company": "Philips", "tenant": "philips", "cluster": "wd3", "site": "jobs-and-careers"},
    {"company": "Salesforce", "tenant": "salesforce", "cluster": "wd12", "site": "External_Career_Site"},
    {"company": "Visa", "tenant": "visa", "cluster": "wd5", "site": "visa"},
)

_WORKDAY_TERMS = ("Saudi Arabia", "Riyadh", "Jeddah", "Dammam", "Jubail", "NEOM")


def _env(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


def _enabled() -> bool:
    return _env("ATS_BOARDS_ENABLED", "true").lower() not in {"0", "false", "no", "off"}


def interval_minutes() -> int:
    try:
        minutes = int(_env("ATS_BOARDS_INTERVAL_MINUTES", "360") or "360")
    except ValueError:
        minutes = 360
    return max(30, min(minutes, 24 * 60))


def ats_boards_status() -> dict:
    return {
        "enabled": _enabled(),
        "interval_minutes": interval_minutes(),
        "platforms": list(PLATFORMS),
        "boards": len(GREENHOUSE) + len(LEVER) + len(SMARTRECRUITERS) + len(WORKDAY),
        "last": dict(_LAST),
    }


def _plain(value: str) -> str:
    text = _HTML_BREAK.sub("\n", value or "")
    text = _HTML_TAG.sub(" ", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&nbsp;", " ")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:20000]


def _get_json(url: str, *, payload: dict | None = None, timeout: int = 20, max_bytes: int = 8_000_000):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        if exc.code not in {404, 410}:
            log.info("ATS request %s failed (%s)", url, exc.code)
        return None
    except Exception as exc:
        log.info("ATS request failed %s (%s)", url, exc)
        return None
    if len(body) > max_bytes:
        log.info("ATS response too large %s", url)
        return None
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        log.info("ATS response was not JSON %s", url)
        return None


def _stamp_and_days(value) -> tuple[str, int]:
    from jobscraper.agent_reach import absolute_posted_at, relative_days

    stamp = absolute_posted_at(value)
    if not stamp:
        return "", relative_days(str(value or ""))
    try:
        posted = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return stamp, 0
    return stamp, max(0, (datetime.now(timezone.utc) - posted).days)


def _keep(location: str, platform: str, description: str) -> tuple[str, str] | None:
    from jobscraper.locations import is_ksa_job, normalize_scraped_location, parse_state

    text = (location or "").strip()
    if not text or not is_ksa_job("", text, allow_bare_remote=False):
        return None
    return normalize_scraped_location(parse_state(text), text, platform, description)


def _job(
    *,
    source: str,
    job_id: str,
    title: str,
    company: str,
    location: str,
    url: str,
    posted_at="",
    description: str = "",
    job_type: str = "",
) -> dict | None:
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    company = re.sub(r"\s+", " ", str(company or "")).strip()
    url = str(url or "").strip()
    if not title or not company or not url.startswith("http"):
        return None
    text = _plain(description)
    kept = _keep(location, source, text)
    if not kept:
        return None
    state, place = kept
    stamp, days = _stamp_and_days(posted_at)
    ident = re.sub(r"\s+", "-", str(job_id or url)).strip("-")[:180]
    return {
        "id": f"{source.casefold()}-{ident}"[:255],
        "title": title[:512],
        "company": company[:255] or "Unknown",
        "skills": [],
        "state": state,
        "location": place,
        "platform": source,
        "url": url[:1000],
        "description": text,
        "posted_at": stamp,
        "platform_posted_at": stamp,
        "daysAgo": days,
        "job_type": str(job_type or "")[:80],
        "raw": {"platform": source, "location": place},
    }


def _gh_location(row: dict) -> str:
    loc = row.get("location") or {}
    if isinstance(loc, dict):
        return str(loc.get("name") or "")
    return str(loc or "")


def _load_greenhouse(token: str) -> list[dict]:
    data = _get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
        max_bytes=20_000_000,
    )
    if not isinstance(data, dict):
        return []
    rows = [row for row in (data.get("jobs") or []) if isinstance(row, dict)]
    if not rows:
        return []
    ksa_ids = {str(row.get("id") or "") for row in rows if _gh_location(row) and _keep(_gh_location(row), "Greenhouse", "")}
    if not ksa_ids:
        return []
    meta = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}")
    board_name = str(meta.get("name") or "").strip() if isinstance(meta, dict) else ""
    if len(rows) <= 120:
        rich = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
        if isinstance(rich, dict) and isinstance(rich.get("jobs"), list):
            rows = [row for row in rich["jobs"] if isinstance(row, dict)]
    jobs = []
    details = 0
    for row in rows:
        job_id = str(row.get("id") or "")
        if job_id not in ksa_ids:
            continue
        content = str(row.get("content") or "")
        if not content and details < _DETAIL_CAP and job_id:
            detail = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}")
            details += 1
            if isinstance(detail, dict):
                row = detail
                content = str(row.get("content") or "")
        company = str(row.get("company_name") or board_name or token).strip() or token
        job = _job(
            source="Greenhouse",
            job_id=f"{token}-{job_id or row.get('absolute_url')}",
            title=str(row.get("title") or ""),
            company=company,
            location=_gh_location(row),
            url=str(row.get("absolute_url") or ""),
            posted_at=row.get("first_published") or row.get("updated_at") or "",
            description=content,
        )
        if job:
            pay = row.get("pay_input_ranges") or row.get("pay_ranges")
            if pay:
                job.setdefault("raw", {})["pay_input_ranges"] = pay
            jobs.append(job)
    return jobs


def _load_lever(token: str) -> list[dict]:
    data = _get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    if not isinstance(data, list):
        return []
    company = token[:1].upper() + token[1:]
    jobs = []
    for row in data:
        if not isinstance(row, dict):
            continue
        categories = row.get("categories") if isinstance(row.get("categories"), dict) else {}
        parts = [str(row.get("descriptionPlain") or row.get("description") or "")]
        for item in row.get("lists") or []:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
                parts.append(str(item.get("content") or ""))
        commitment = categories.get("commitment")
        job_type = commitment if isinstance(commitment, str) else ""
        job = _job(
            source="Lever",
            job_id=str(row.get("id") or ""),
            title=str(row.get("text") or ""),
            company=company,
            location=str(categories.get("location") or ""),
            url=str(row.get("hostedUrl") or row.get("applyUrl") or ""),
            posted_at=row.get("createdAt") or "",
            description="\n".join(parts),
            job_type=job_type,
        )
        if job:
            if isinstance(row.get("salaryRange"), dict):
                job.setdefault("raw", {})["salaryRange"] = row["salaryRange"]
            jobs.append(job)
    return jobs


def _sr_location(row: dict) -> str:
    loc = row.get("location") if isinstance(row.get("location"), dict) else {}
    country = str(loc.get("country") or "").lower()
    full = ", ".join(part.strip() for part in str(loc.get("fullLocation") or "").split(",") if part.strip())
    city = str(loc.get("city") or "").strip()
    if country not in {"sa", "sau", "saudi arabia"}:
        return full or city
    if full and not re.search(r"saudi|\bksa\b", full, re.I):
        return f"{full}, Saudi Arabia"
    return full or (f"{city}, Saudi Arabia" if city else "Saudi Arabia")


def _load_smartrecruiters(token: str, company: str) -> list[dict]:
    meta = _get_json(f"https://api.smartrecruiters.com/v1/companies/{token}")
    if isinstance(meta, dict) and meta.get("name"):
        company = str(meta.get("name") or company).strip() or company
    identifier = str((meta or {}).get("identifier") or token) if isinstance(meta, dict) else token
    jobs = []
    offset = 0
    while offset < 300:
        data = _get_json(
            "https://api.smartrecruiters.com/v1/companies/"
            f"{identifier}/postings?limit=100&offset={offset}&country=sa"
        )
        if not isinstance(data, dict):
            break
        rows = data.get("content") or []
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            posting_id = str(row.get("id") or "")
            url = str(row.get("ref") or "")
            if "smartrecruiters.com" not in url or "/postings/" in url:
                url = f"https://jobs.smartrecruiters.com/{identifier}/{posting_id}"
            employment = row.get("typeOfEmployment")
            job_type = ""
            if isinstance(employment, dict):
                job_type = str(employment.get("label") or "")
            description = ""
            if posting_id and len(jobs) < _DETAIL_CAP:
                detail = _get_json(
                    f"https://api.smartrecruiters.com/v1/companies/{identifier}/postings/{posting_id}"
                )
                if isinstance(detail, dict):
                    from jobscraper.agent_reach import smartrecruiters_description

                    description = smartrecruiters_description(detail)
                    if not url.startswith("http"):
                        url = f"https://jobs.smartrecruiters.com/{identifier}/{posting_id}"
            job = _job(
                source="SmartRecruiters",
                job_id=f"{identifier}-{posting_id}",
                title=str(row.get("name") or ""),
                company=company,
                location=_sr_location(row),
                url=url,
                posted_at=row.get("releasedDate") or "",
                description=description,
                job_type=job_type,
            )
            if job:
                jobs.append(job)
        offset += len(rows)
        total = int(data.get("totalFound") or 0)
        if offset >= total:
            break
    return jobs


def _workday_page(board: dict, search: str, offset: int) -> dict | None:
    tenant = board["tenant"]
    site = board["site"]
    url = (
        f"https://{tenant}.{board['cluster']}.myworkdayjobs.com"
        f"/wday/cxs/{tenant}/{site}/jobs"
    )
    data = _get_json(
        url,
        payload={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search},
    )
    return data if isinstance(data, dict) else None


def _workday_description(board: dict, path: str) -> str:
    tenant = board["tenant"]
    endpoint = (
        f"https://{tenant}.{board['cluster']}.myworkdayjobs.com"
        f"/wday/cxs/{tenant}/{board['site']}{path}"
    )
    data = _get_json(endpoint)
    if not isinstance(data, dict):
        return ""
    info = data.get("jobPostingInfo") if isinstance(data.get("jobPostingInfo"), dict) else {}
    return str(info.get("jobDescription") or "")


def _load_workday(board: dict) -> list[dict]:
    tenant = board["tenant"]
    cluster = board["cluster"]
    site = board["site"]
    host = f"{tenant}.{cluster}.myworkdayjobs.com"
    found: dict[str, dict] = {}
    first = _workday_page(board, _WORKDAY_TERMS[0], 0)
    if first is None:
        return []
    for term in _WORKDAY_TERMS:
        offset = 0
        for _ in range(3):
            data = first if term == _WORKDAY_TERMS[0] and offset == 0 else _workday_page(board, term, offset)
            first = None
            if not isinstance(data, dict):
                break
            postings = data.get("jobPostings") or []
            if not postings:
                break
            for row in postings:
                if not isinstance(row, dict):
                    continue
                path = str(row.get("externalPath") or "").strip()
                title = str(row.get("title") or "").strip()
                if not path or not title:
                    continue
                if not path.startswith("/"):
                    path = "/" + path
                if path in found:
                    continue
                location = str(row.get("locationsText") or "")
                if f"/{site.lower()}/" in path.lower():
                    url = f"https://{host}{path}"
                else:
                    url = f"https://{host}/en-US/{site}{path}"
                found[path] = {
                    "title": title,
                    "location": location,
                    "url": url,
                    "path": path,
                    "posted": str(row.get("postedOn") or ""),
                }
            offset += len(postings)
            total = int(data.get("total") or 0)
            if total and offset >= total:
                break
    jobs = []
    described = 0
    for item in found.values():
        description = ""
        if described < _DETAIL_CAP and _keep(item["location"], "Workday", ""):
            description = _workday_description(board, item["path"])
            described += 1
        job = _job(
            source="Workday",
            job_id=f"{tenant}-{item['path'].rstrip('/').split('/')[-1][:80]}",
            title=item["title"],
            company=board["company"],
            location=item["location"],
            url=item["url"],
            posted_at=item["posted"],
            description=description,
        )
        if job:
            jobs.append(job)
    return jobs


def collect_jobs() -> list[dict]:
    """Read every active board and return Saudi listings."""
    from jobscraper.sources import is_active, touch_last_sync

    tasks = []
    ran: list[str] = []
    found: list[dict] = []
    seen: set[str] = set()
    with ThreadPoolExecutor(max_workers=6) as pool:
        if is_active("greenhouse"):
            ran.append("greenhouse")
            for token in GREENHOUSE:
                tasks.append(pool.submit(_load_greenhouse, token))
        if is_active("lever"):
            ran.append("lever")
            for token in LEVER:
                tasks.append(pool.submit(_load_lever, token))
        if is_active("smartrecruiters"):
            ran.append("smartrecruiters")
            for token, company in SMARTRECRUITERS:
                tasks.append(pool.submit(_load_smartrecruiters, token, company))
        if is_active("workday"):
            ran.append("workday")
            for board in WORKDAY:
                tasks.append(pool.submit(_load_workday, board))
        if not tasks:
            return []
        for future in as_completed(tasks):
            try:
                rows = future.result()
            except Exception:
                log.exception("ATS board failed")
                continue
            for job in rows:
                key = str(job.get("url") or job.get("id") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                found.append(job)
    for source_id in ran:
        touch_last_sync(source_id)
    return found


def sync_ats_boards() -> dict:
    """Pull Saudi jobs from the four ATS boards and upsert them."""
    from jobscraper.sources import is_active

    if not _enabled():
        return {"ok": True, "saved": 0, "fetched": 0, "skipped": True}
    if not any(is_active(source_id) for source_id in ("greenhouse", "lever", "smartrecruiters", "workday")):
        log.info("ATS board sync skipped: greenhouse, lever, smartrecruiters, and workday are inactive")
        return {"ok": True, "saved": 0, "fetched": 0, "skipped": True}
    if not _LOCK.acquire(blocking=False):
        log.info("ATS board sync already running")
        return {"ok": True, "saved": 0, "fetched": 0, "skipped": True}
    saved = 0
    fetched = 0
    try:
        jobs = collect_jobs()
        fetched = len(jobs)
        from jobscraper.db import mysql_configured, upsert_jobs

        if jobs and mysql_configured():
            saved = upsert_jobs(jobs)
        elif jobs:
            log.warning("ATS boards fetched %s Saudi jobs but MySQL is not configured", fetched)
        _LAST.update(
            {
                "ok": True,
                "saved": saved,
                "fetched": fetched,
                "boards": len(GREENHOUSE) + len(LEVER) + len(SMARTRECRUITERS) + len(WORKDAY),
                "error": None,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        log.info("ATS boards saved %s of %s Saudi jobs", saved, fetched)
        return {"ok": True, "saved": saved, "fetched": fetched}
    except Exception as exc:
        _LAST.update(
            {
                "ok": False,
                "saved": saved,
                "fetched": fetched,
                "boards": len(GREENHOUSE) + len(LEVER) + len(SMARTRECRUITERS) + len(WORKDAY),
                "error": str(exc)[:500],
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        log.exception("ATS board sync failed")
        return {"ok": False, "saved": saved, "fetched": fetched, "error": str(exc)[:500]}
    finally:
        _LOCK.release()
