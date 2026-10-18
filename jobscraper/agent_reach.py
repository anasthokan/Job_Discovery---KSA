"""Job sources routed the Agent Reach way: Jina Reader for pages, Algolia for HN."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from urllib.parse import unquote, urlencode, urlparse, urlunparse

JINA_PREFIX = "https://r.jina.ai/"
JINA_HEADERS = {
    "Accept": "text/plain",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}
DICE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
}

LINKEDIN_JOB_RE = re.compile(
    r"\[([^\]]+)\]\((https://(?:www\.)?linkedin\.com/jobs/view/[^)\s]+)\)",
    re.I,
)
LINKEDIN_COMPANY_RE = re.compile(r"####\s*\[([^\]]+)\]\(")
DICE_JOB_RE = re.compile(
    r"\[([^\]]+)\]\((https://www\.dice\.com/(?:job-detail|direct-apply)/[a-z0-9-]+)\)",
    re.I,
)
DICE_COMPANY_RE = re.compile(
    r"\[([^\]]+)\]\(https://www\.dice\.com/company-profile/[^)]+\)",
    re.I,
)
YC_IMAGE_RE = re.compile(r"!\[Image \d+: ([^\]]+)\]\(")
YC_APPLY_RE = re.compile(r"\[Apply\]\((https://account\.ycombinator\.com/authenticate\?[^)]+)\)")
YC_JOB_ID_RE = re.compile(r"signup_job_id(?:%3D|=)(\d+)")
YC_ROLE_RE = re.compile(r"^(Fulltime|Parttime|Part-time|Intern|Contract)\s+(.+)$", re.I)
RELATIVE_RE = re.compile(
    r"(today|just now|yesterday|(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago)",
    re.I,
)
ANTIBOT_MARKERS = (
    "title: just a moment",
    "additional verification required",
    "requiring captcha",
    "performing security verification",
    "attention required! | cloudflare",
)
SKIP_LINES = {
    "actively hiring",
    "be an early applicant",
    "full-time",
    "part-time",
    "contract",
}

YC_ROLE_TYPES = (
    "engineering manager",
    "machine learning",
    "full stack",
    "frontend",
    "backend",
    "hardware",
    "robotics",
    "mechanical",
    "design",
    "product",
    "ios",
    "android",
    "devops",
    "data",
    "growth",
    "sales",
    "ops",
)


def jina_url(url: str) -> str:
    return f"{JINA_PREFIX}{url}"


def clean_url(url: str) -> str:
    parsed = urlparse(url.split()[0].rstrip(").,;"))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def is_antibot(text: str) -> bool:
    sample = (text or "")[:4000].casefold()
    return any(marker in sample for marker in ANTIBOT_MARKERS)


def relative_days(value: str) -> int:
    text = (value or "").strip()
    match = RELATIVE_RE.search(text)
    if not match:
        return 0
    token = match.group(1).lower()
    if token in {"today", "just now"}:
        return 0
    if token == "yesterday":
        return 1
    amount = int(match.group(2) or 0)
    unit = (match.group(3) or "day").lower()
    if unit.startswith("minute") or unit.startswith("hour"):
        return 0
    if unit.startswith("day"):
        return amount
    if unit.startswith("week"):
        return amount * 7
    if unit.startswith("month"):
        return amount * 30
    return amount * 365


def linkedin_search_urls(query: str, location: str, remote: bool) -> list[str]:
    urls = []
    for start in (0, 10, 20):
        params = {
            "keywords": query,
            "location": location,
            "start": str(start),
            "f_TPR": "r2592000",
        }
        if remote:
            params["f_WT"] = "2"
        target = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urlencode(params)
        urls.append(jina_url(target))
    return urls


def dice_search_urls(query: str, location: str) -> list[str]:
    urls = []
    for page in (1, 2):
        params = {
            "q": query,
            "countryCode": "US",
            "page": str(page),
            "pageSize": "20",
            "language": "en",
        }
        if location and location.lower() not in {"united states", "remote"}:
            params["locationName"] = location
        urls.append("https://www.dice.com/jobs?" + urlencode(params))
    return urls


def yc_jobs_url() -> str:
    return jina_url("https://www.workatastartup.com/jobs")


def hn_latest_thread_url() -> str:
    return (
        "https://hn.algolia.com/api/v1/search_by_date"
        "?query=Who%20is%20hiring&tags=author_whoishiring,story&hitsPerPage=1"
    )


def hn_comments_url(story_id: str, query: str, page: int) -> str:
    params = {
        "tags": f"comment,story_{story_id}",
        "hitsPerPage": "50",
        "page": str(page),
    }
    if query:
        params["query"] = query
    return "https://hn.algolia.com/api/v1/search?" + urlencode(params)


def parse_linkedin_markdown(text: str) -> list[dict]:
    jobs = []
    seen = set()
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        match = LINKEDIN_JOB_RE.search(line)
        if not match:
            continue
        title = match.group(1).strip()
        url = clean_url(match.group(2))
        if title.lower().startswith("image "):
            continue
        ident_match = re.search(r"-(\d+)$", url)
        ident = ident_match.group(1) if ident_match else url
        if ident in seen:
            continue
        seen.add(ident)
        company = "Unknown"
        location = "United States"
        posted = ""
        window = lines[index + 1 : index + 14]
        for offset, nxt in enumerate(window):
            company_match = LINKEDIN_COMPANY_RE.search(nxt)
            if not company_match:
                continue
            company = company_match.group(1).strip() or company
            for extra in window[offset + 1 :]:
                extra = extra.strip()
                if not extra:
                    continue
                if extra.startswith("*") or extra.startswith("###") or extra.startswith("####"):
                    break
                if extra.startswith("[") or LINKEDIN_JOB_RE.search(extra):
                    break
                if extra.lower() in SKIP_LINES:
                    continue
                if RELATIVE_RE.search(extra):
                    posted = extra
                    loc_part = RELATIVE_RE.sub("", extra)
                    loc_part = re.sub(
                        r"(actively hiring|be an early applicant)",
                        "",
                        loc_part,
                        flags=re.I,
                    ).strip(" -•|")
                    if loc_part and location == "United States":
                        location = loc_part
                    continue
                if location == "United States":
                    location = extra
            break
        jobs.append(
            {
                "id": f"linkedin-{ident}",
                "title": title,
                "company": company,
                "location": location,
                "url": url,
                "posted_at": posted,
                "daysAgo": relative_days(posted),
                "platform": "LinkedIn",
            }
        )
    return jobs


def posting_paragraph(text: str, title: str = "") -> str:
    """Real posting text. A title or one short line is not a description."""
    body = re.sub(r"\s+", " ", html.unescape(text or "")).strip()
    heading = re.sub(r"\s+", " ", title or "").strip()
    if len(body) < 120:
        return ""
    if heading and body.lower() == heading.lower():
        return ""
    return body


def parse_dice_search_html(page: str) -> list[dict]:
    """Jobs embedded in the Dice search page, including the posting summary."""
    start = (page or "").find("jobList")
    if start < 0:
        return []
    segment = page[start : start + 500000].replace('\\"', '"').replace("\\\\", "\\")
    data_at = segment.find('{"data":')
    if data_at < 0:
        return []
    blob = segment[data_at:]
    depth = 0
    end = None
    for index, char in enumerate(blob):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if not end:
        return []
    try:
        payload = json.loads(blob[:end])
    except json.JSONDecodeError:
        return []
    jobs = []
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        guid = str(row.get("guid") or "").strip()
        url = str(row.get("detailsPageUrl") or "").strip()
        if not url and guid:
            url = f"https://www.dice.com/job-detail/{guid}"
        title = str(row.get("title") or "").strip()
        if not title or not url:
            continue
        place = row.get("jobLocation") if isinstance(row.get("jobLocation"), dict) else {}
        location = str((place or {}).get("displayName") or "").strip()
        if row.get("isRemote") and "remote" not in location.lower():
            location = f"{location}, Remote" if location else "Remote"
        jobs.append(
            {
                "id": f"dice-{guid or url}",
                "title": title,
                "company": str(row.get("companyName") or "Unknown").strip() or "Unknown",
                "location": location or "United States",
                "url": url,
                "posted_at": str(row.get("postedDate") or ""),
                "summary": str(row.get("summary") or ""),
                "job_type": str(row.get("employmentType") or ""),
                "raw": row,
            }
        )
    return jobs


def dice_detail_url(url: str) -> str:
    """Dice job-detail page. direct-apply links are a shell and only carry a short summary."""
    match = re.search(
        r"dice\.com/(?:job-detail|direct-apply)/([0-9a-f-]{16,})",
        url or "",
        re.I,
    )
    if not match:
        return (url or "").strip()
    return f"https://www.dice.com/job-detail/{match.group(1)}"


def dice_description_from_html(page: str) -> str:
    """Full posting body from a Dice job detail page."""
    marker = "jobDescription\">"
    start_at = (page or "").find(marker)
    if start_at < 0:
        return ""
    start = start_at + len(marker)
    depth = 1
    index = start
    text = page or ""
    while index < len(text) and depth:
        if text.startswith("<div", index):
            depth += 1
            index = text.find(">", index)
            index = len(text) if index < 0 else index + 1
            continue
        if text.startswith("</div>", index):
            depth -= 1
            if depth == 0:
                plain = re.sub(r"<[^>]+>", " ", text[start:index])
                return posting_paragraph(plain)
            index += 6
            continue
        index += 1
    return ""


def _inner_html(page: str, start_tag_at: int) -> str:
    """Inner HTML of the element that starts at start_tag_at, including nested tags."""
    text = page or ""
    tag_match = re.match(r"<\s*([A-Za-z0-9]+)", text[start_tag_at:])
    if not tag_match:
        return ""
    tag = tag_match.group(1).lower()
    open_end = text.find(">", start_tag_at)
    if open_end < 0:
        return ""
    opener = text[start_tag_at : open_end + 1]
    if opener.rstrip().endswith("/>"):
        return ""
    depth = 1
    index = open_end + 1
    open_tag = re.compile(rf"<{tag}\b", re.I)
    close = f"</{tag}"
    while index < len(text) and depth:
        next_open = open_tag.search(text, index)
        next_close = text.lower().find(close, index)
        if next_close < 0:
            return text[open_end + 1 :]
        if next_open and next_open.start() < next_close:
            depth += 1
            index = text.find(">", next_open.end())
            index = len(text) if index < 0 else index + 1
            continue
        depth -= 1
        if depth == 0:
            return text[open_end + 1 : next_close]
        index = text.find(">", next_close)
        index = len(text) if index < 0 else index + 1
    return ""


def linkedin_description_from_html(page: str) -> str:
    match = re.search(
        r'<div[^>]*class="[^"]*show-more-less-html__markup[^"]*"[^>]*>',
        page or "",
        re.I,
    )
    if not match:
        return ""
    plain = re.sub(r"<[^>]+>", " ", _inner_html(page, match.start()))
    return posting_paragraph(plain)


def yc_description_from_html(page: str) -> str:
    match = re.search(
        r"descriptionHtml&quot;:&quot;(.*?)&quot;,&quot;",
        page or "",
        re.S,
    )
    if not match:
        return ""
    raw = html.unescape(match.group(1))
    try:
        raw = raw.encode("utf-8").decode("unicode_escape")
    except UnicodeError:
        pass
    plain = re.sub(r"<[^>]+>", " ", raw)
    return posting_paragraph(plain)


def smartrecruiters_description(payload: dict) -> str:
    sections = ((payload or {}).get("jobAd") or {}).get("sections") or {}
    parts = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        text = (sections.get(key) or {}).get("text") or ""
        plain = re.sub(r"<[^>]+>", " ", str(text))
        if plain.strip():
            parts.append(plain)
    return posting_paragraph(" ".join(parts))


def fetch_posting_paragraph(platform: str, url: str) -> str:
    """Load the source posting and return its paragraph, never a made-up line."""
    import urllib.request

    target = (url or "").strip()
    if not target:
        return ""
    name = platform or ""
    headers = dict(DICE_HEADERS)
    if name == "Dice":
        target = dice_detail_url(target)
    if name == "LinkedIn":
        match = re.search(r"(\d{6,})", target)
        if not match:
            return ""
        target = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{match.group(1)}"
    elif name == "SmartRecruiters" and "api.smartrecruiters.com" not in target:
        match = re.search(r"smartrecruiters\.com/([^/]+)/([^/?#]+)", target)
        if not match:
            return ""
        target = (
            "https://api.smartrecruiters.com/v1/companies/"
            f"{match.group(1)}/postings/{match.group(2)}"
        )
        headers["Accept"] = "application/json"
    request = urllib.request.Request(target, headers=headers)
    with urllib.request.urlopen(request, timeout=25) as response:
        page = response.read().decode("utf-8", "ignore")
    if name == "Dice":
        return dice_description_from_html(page)
    if name == "LinkedIn":
        return linkedin_description_from_html(page)
    if name == "Y Combinator":
        return yc_description_from_html(page)
    if name == "SmartRecruiters":
        try:
            payload = json.loads(page)
        except json.JSONDecodeError:
            return ""
        return smartrecruiters_description(payload)
    return ""


def parse_dice_markdown(text: str) -> list[dict]:
    jobs = []
    seen = set()
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        match = DICE_JOB_RE.search(line)
        if not match:
            continue
        title = match.group(1).strip()
        url = clean_url(match.group(2))
        if not title or url in seen:
            continue
        seen.add(url)
        company = "Unknown"
        location = "United States"
        posted = ""
        for nxt in lines[index + 1 : index + 8]:
            company_match = DICE_COMPANY_RE.search(nxt)
            if company_match and company == "Unknown":
                company = company_match.group(1).strip() or company
                continue
            stripped = nxt.strip()
            if "•" in stripped:
                place, _, when = stripped.partition("•")
                location = place.strip() or location
                posted = when.strip()
                break
        ident = url.rstrip("/").split("/")[-1]
        jobs.append(
            {
                "id": f"dice-{ident}",
                "title": title,
                "company": company,
                "location": location,
                "url": url,
                "posted_at": posted,
                "daysAgo": relative_days(posted),
                "platform": "Dice",
            }
        )
    return jobs


def _yc_title(role_line: str) -> str:
    lowered = role_line.lower()
    for role in YC_ROLE_TYPES:
        if role in lowered:
            pretty = role.title().replace("Ios", "iOS")
            if pretty.endswith("Manager") or pretty in {"Design", "Product", "Sales", "Ops"}:
                return pretty
            if pretty.endswith("Learning"):
                return "Machine Learning Engineer"
            return f"{pretty} Engineer"
    cleaned = re.sub(r"\$[\d.,KMkm\s\-–]+", "", role_line)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -/")
    return cleaned or "Software Engineer"


def parse_yc_markdown(text: str) -> list[dict]:
    jobs = []
    seen = set()
    lines = (text or "").splitlines()
    pending_company = ""
    pending_role = ""
    for line in lines:
        image = YC_IMAGE_RE.search(line)
        if image:
            pending_company = image.group(1).strip()
            pending_role = ""
            continue
        role = YC_ROLE_RE.match(line.strip())
        if role and pending_company:
            pending_role = role.group(2).strip()
            continue
        apply = YC_APPLY_RE.search(line)
        if not apply or not pending_company:
            continue
        raw_url = unquote(apply.group(1))
        job_id_match = YC_JOB_ID_RE.search(apply.group(1)) or YC_JOB_ID_RE.search(raw_url)
        job_id = job_id_match.group(1) if job_id_match else clean_url(raw_url)
        if job_id in seen:
            pending_company = ""
            pending_role = ""
            continue
        seen.add(job_id)
        location = pending_role or "United States"
        location = re.sub(r"\$[\d.,KMkm\s\-–]+.*$", "", location).strip()
        for role_name in YC_ROLE_TYPES:
            idx = location.lower().find(role_name)
            if idx > 0:
                location = location[:idx].strip(" /,-")
                break
        jobs.append(
            {
                "id": f"yc-{job_id}",
                "title": _yc_title(pending_role),
                "company": pending_company,
                "location": location or "United States",
                "url": f"https://www.workatastartup.com/jobs/{job_id}",
                "posted_at": "",
                "daysAgo": 0,
                "platform": "Y Combinator",
            }
        )
        pending_company = ""
        pending_role = ""
    return jobs


def parse_hn_comment(hit: dict) -> dict | None:
    raw = html.unescape(str(hit.get("comment_text") or hit.get("story_text") or ""))
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    first = text.split(".")[0][:180]
    company = "Unknown"
    title = first
    location = "Remote"
    if "|" in first:
        parts = [part.strip() for part in first.split("|") if part.strip()]
        if parts:
            company = re.sub(r"\s*\([^)]*\)", "", parts[0]).strip(" -")[:80] or company
        if len(parts) > 1:
            location = parts[1]
        if len(parts) > 2:
            title = parts[2]
        elif len(parts) > 1:
            title = parts[-1]
    elif "(" in first:
        company = first.split("(", 1)[0].strip()[:80] or company
        close = first.find(")")
        loc = first[first.find("(") + 1 : close if close > 0 else None]
        if loc:
            location = loc
    else:
        company = first.split(" is ")[0].split(" - ")[0][:80] or company
        title = "Software Engineer"
    object_id = str(hit.get("objectID") or "")
    created = hit.get("created_at") or ""
    if not created and hit.get("created_at_i"):
        created = datetime.fromtimestamp(int(hit["created_at_i"]), tz=timezone.utc).isoformat()
    return {
        "id": f"hn-{object_id}",
        "title": title[:120] or "Software Engineer",
        "company": company or "Unknown",
        "location": location or "Remote",
        "url": f"https://news.ycombinator.com/item?id={object_id}",
        "posted_at": str(created),
        "daysAgo": 0,
        "platform": "Hacker News",
        "search_text": text,
    }
