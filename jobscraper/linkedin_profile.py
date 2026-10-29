"""Fill the profile form from a public LinkedIn profile URL.

Uses the public profile page only. Fields LinkedIn does not show are empty strings.
"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser

from jobscraper.profile_autofill import (
    _COUNTRIES,
    _apply_location,
    _job_sort_key,
    _normalize_date,
    _split_name,
)

_PROFILE_RE = re.compile(
    r"(?:https?://)?(?:[\w.-]+\.)?linkedin\.com/in/([A-Za-z0-9\-_%]+)/?",
    re.I,
)
_DATE_LINE = re.compile(
    r"^(?:[A-Za-z]{3,9}\.?\s+)?(?:19|20)\d{2}\s*[-–—]\s*(?:Present|Current|Now|(?:[A-Za-z]{3,9}\.?\s+)?(?:19|20)\d{2})$",
    re.I,
)
_DURATION = re.compile(r"^\d+\s+(?:years?|months?|yrs?)$", re.I)
_NOISE = re.compile(
    r"(?:sign in|join now|full profile|follower|connection|see who|get introduced|"
    r"forgot password|user agreement|welcome back|email or phone|password|show or)",
    re.I,
)
_ABOUT_CUT = re.compile(
    r"\b(?:see more|welcome back|email or phone|password|join to view|sign in)\b",
    re.I,
)
_ROLE = re.compile(
    r"\b(?:manager|head|founder|owner|lead|leader|engineer|programmer|developer|"
    r"director|consultant|specialist|analyst|officer|architect|chair|partner)\b",
    re.I,
)
_DEGREE = re.compile(
    r"\b(?:bachelor|bachelors|master|masters|mba|phd|doctorate|associate|diploma|"
    r"b\.?s\.?|m\.?s\.?|b\.?a\.?|m\.?a\.?)\b",
    re.I,
)
_SPAN = re.compile(
    r"((?:[A-Za-z]{3,9}\.?\s+)?(?:19|20)\d{2})\s*[-–—]\s*"
    r"(Present|Current|Now|(?:[A-Za-z]{3,9}\.?\s+)?(?:19|20)\d{2})",
    re.I,
)
_SCHOOL = ("university", "college", "school", "institute", "academy")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
_MAX_BYTES = 3 * 1024 * 1024
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_CLASS_BUCKETS = {
    "top-card-layout__title": "name",
    "top-card-layout__headline": "headline",
    "top-card-layout__first-subline": "location",
    "experience__list": "experience",
    "education__list": "education",
    "skills__list": "skills",
}


class LinkedInProfileError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class _Sections(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buckets = {name: [] for name in ("name", "headline", "location", "about", "experience", "education", "skills")}
        self.depth = {name: 0 for name in self.buckets}
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        for name, depth in self.depth.items():
            if depth:
                self.depth[name] = depth + 1
        attr = {key: value or "" for key, value in attrs}
        classes = set(attr.get("class", "").split())
        for token, name in _CLASS_BUCKETS.items():
            if token in classes and self.depth[name] == 0:
                self.depth[name] = 1
        if attr.get("data-section") in {"summary", "about"} and self.depth["about"] == 0:
            self.depth["about"] = 1
        if any(self.depth.values()) and tag in {"br", "p", "div", "li", "h1", "h2", "h3", "section"}:
            self._write("\n")
        if tag in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1
        if any(self.depth.values()) and tag in {"p", "div", "li", "h1", "h2", "h3", "section"}:
            self._write("\n")
        for name, depth in self.depth.items():
            if depth:
                self.depth[name] = depth - 1

    def handle_data(self, data):
        if not self.skip:
            self._write(data)

    def _write(self, data: str) -> None:
        for name, depth in self.depth.items():
            if depth:
                self.buckets[name].append(data)

    def lines(self, name: str) -> list[str]:
        raw = "".join(self.buckets[name])
        lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
        return [line for line in lines if line and not _NOISE.search(line)]


def linkedin_profile_url(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise LinkedInProfileError("Send a LinkedIn profile URL.")
    if "://" not in text and text.lower().startswith("linkedin.com"):
        text = "https://" + text
    if "://" not in text and "linkedin.com/" in text.lower():
        text = "https://" + text.split("://")[-1]
    match = _PROFILE_RE.search(text.replace(" ", ""))
    if not match:
        raise LinkedInProfileError(
            "Send a LinkedIn profile link such as https://www.linkedin.com/in/username."
        )
    slug = match.group(1).strip("/")
    return f"https://www.linkedin.com/in/{slug}"


def fetch_linkedin_profile(url: str) -> str:
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.read(_MAX_BYTES).decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise LinkedInProfileError("LinkedIn profile was not found.", 404) from exc
        if exc.code in {429, 999}:
            raise LinkedInProfileError("LinkedIn is limiting requests. Try this profile again shortly.", 429) from exc
        raise LinkedInProfileError(f"LinkedIn returned {exc.code} for this profile.", 502) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LinkedInProfileError("Could not reach LinkedIn.", 502) from exc


def autofill_from_linkedin(value: str) -> dict:
    url = linkedin_profile_url(value)
    page = fetch_linkedin_profile(url)
    return profile_from_linkedin_html(page, url)


def profile_from_linkedin_html(page: str, url: str) -> dict:
    parser = _Sections()
    parser.feed(page or "")
    meta = _meta_map(page or "")
    og_about, og_company, og_school, og_location = _og_bits(
        meta.get("og:description") or meta.get("description") or ""
    )
    first = _field(meta.get("profile:first_name", ""))
    last = _field(meta.get("profile:last_name", ""))
    if first or last:
        name = f"{first} {last}".strip()
    else:
        name = " ".join(parser.lines("name")[:1])
        first, last = _split_name(name)
    if not name:
        raise LinkedInProfileError(
            "This LinkedIn profile is not public, so there is nothing to read.",
            422,
        )
    headline = _clean_summary(" ".join(parser.lines("headline")[:2]))
    about = _clean_summary(" ".join(parser.lines("about")))
    location = " ".join(parser.lines("location")[:1])
    location = re.split(r"\bcontact info\b", location, flags=re.I)[0].strip(" ·")
    if not location:
        location = og_location
    cards = _read_cards(page or "")
    jobs = _merge_jobs(_jobs_from_cards(cards), _experience(parser.lines("experience")))
    if og_company and not any(_same_org(job["company"], og_company) for job in jobs):
        jobs.append(_blank_job(og_company, ""))
    jobs = _dedupe_jobs(jobs)[:20]
    schools = _merge_schools(_schools_from_cards(cards), _education(parser.lines("education")))
    if og_school and not any(_same_org(school["school"], og_school) for school in schools):
        schools.append(_blank_school(og_school))
    schools = schools[:12]
    skills = _skills(parser.lines("skills"))
    for card in cards:
        if _section_kind(card["section"]) != "skills":
            continue
        skill = _field(card["h3"])
        if skill and skill.lower() not in {item.lower() for item in skills}:
            skills.append(skill)
    address = _blank_address()
    _place(address, location)
    summary = about or og_about or headline
    visible = "\n".join(
        [
            name,
            headline,
            location,
            summary,
            *[f"{job['job_title']} {job['company']}" for job in jobs],
            *[school["school"] for school in schools],
            *skills,
        ]
    )
    return {
        "ok": True,
        "source": {
            "filename": "",
            "content_type": "text/html",
            "pages": None,
            "char_count": len(visible.strip()),
        },
        "executive_summary": summary[:2000],
        "work_experience": jobs,
        "education": schools,
        "certifications": [],
        "skills": ", ".join(skills),
        "personal": {
            "first_name": first[:80],
            "last_name": last[:80],
            "preferred_name": "",
            "email": "",
            "phone_country_code": "",
            "phone_number": "",
            "nationality": "",
        },
        "national_address": address,
        "online_profiles": {
            "linkedin_url": url,
            "github_url": "",
            "portfolio_url": "",
            "personal_website": "",
        },
        "work_authorization": {
            "authorized_to_work": "",
            "need_iqama_or_work_visa": "",
            "work_authorization_type": "",
            "work_authorization_validity": "",
        },
        "logistics": {
            "desired_monthly_salary_sar": "",
            "notice_period": "",
            "willing_to_relocate": "",
            "open_to_remote": "",
            "meets_minimum_working_age": "",
            "how_heard": "",
        },
        "optional_identifiers": {
            "gender": "",
            "disability_status": "",
        },
        "custom_answers": [],
    }


def _blank_address() -> dict[str, str]:
    return {
        "building_number": "",
        "street": "",
        "district": "",
        "city": "",
        "region": "",
        "postal_code": "",
        "additional_number": "",
        "short_address": "",
        "country": "",
    }


def _blank_job(company: str, title: str, start: str = "", end: str = "") -> dict[str, str]:
    return {
        "company": company[:160],
        "job_title": title[:160],
        "start_date": start,
        "end_date": end,
        "responsibilities": "",
    }


def _blank_school(school: str, degree: str = "", field: str = "", year: str = "") -> dict[str, str]:
    return {
        "school": school[:160],
        "degree": degree[:120],
        "field_of_study": field[:120],
        "graduation_year": year,
        "grade": "",
    }


def _unescape(value: str) -> str:
    text = html.unescape(value or "")
    if "&" in text:
        text = html.unescape(text)
    return text


def _field(value: str) -> str:
    text = re.sub(r"\s+", " ", _unescape(value)).strip(" ·-–—")
    if not text or text.lower() in {"see more", "show more", "about"}:
        return ""
    stars = text.count("*")
    letters = len(re.sub(r"[^A-Za-z]", "", text))
    if stars >= 3 and stars >= letters:
        return ""
    return text


def _clean_summary(value: str) -> str:
    text = re.sub(r"\s+", " ", _unescape(value)).strip()
    text = _ABOUT_CUT.split(text)[0]
    text = re.split(r"\s+·\s+(?:Experience|Education|Location)\s*:", text)[0]
    text = re.sub(r"^about(?:\s+me)?\s+", "", text, flags=re.I)
    return text.strip(" ·.…-")


def _meta_map(page: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in re.finditer(r"<meta\b([^>]+)>", page, re.I):
        attrs = dict(re.findall(r'([\w:-]+)\s*=\s*"([^"]*)"', match.group(1)))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs:
            found[key] = _unescape(attrs["content"])
    return found


def _og_bits(description: str) -> tuple[str, str, str, str]:
    text = re.sub(r"\s+", " ", _unescape(description)).strip()
    if not text:
        return "", "", "", ""
    company = _labeled(text, "Experience")
    school = _labeled(text, "Education")
    location = _labeled(text, "Location")
    return _clean_summary(text), company, school, location


def _labeled(text: str, label: str) -> str:
    match = re.search(rf"{label}:\s*(.*?)\s*(?:·|$)", text, re.I)
    if not match:
        return ""
    value = match.group(1).strip(" .")
    if re.search(r"\b(?:connection|linkedin)\b", value, re.I):
        return ""
    return value


def _place(address: dict[str, str], location: str) -> None:
    _apply_location(address, location)
    city = address["city"].strip().lower()
    if city in _COUNTRIES:
        if not address["country"]:
            address["country"] = _COUNTRIES[city]
        address["city"] = ""


def _same_org(left: str, right: str) -> bool:
    a = re.sub(r"[^a-z0-9]", "", (left or "").lower())
    b = re.sub(r"[^a-z0-9]", "", (right or "").lower())
    return bool(a and b and (a in b or b in a))


def _section_kind(title: str) -> str:
    text = (title or "").lower()
    if "recommend" in text:
        return "recommendation"
    if "organ" in text:
        return "organization"
    if "skill" in text:
        return "skills"
    if "experience" in text or text.strip() == "education":
        return "experience"
    return ""


def _strip_duration(text: str) -> str:
    text = re.sub(r"\s+\d+\s+years?\b.*$", "", text, flags=re.I).strip()
    return re.sub(r"\s+\d+\s+months?\b.*$", "", text, flags=re.I).strip()


def _span_from(text: str) -> tuple[str, str]:
    match = _SPAN.search(_strip_duration(text))
    if not match:
        return "", ""
    end = match.group(2).strip()
    current = end.lower() in {"present", "current", "now"}
    return _normalize_date(match.group(1).strip()), _normalize_date(end, current=current)


def _usable(text: str) -> bool:
    return len(re.sub(r"[^A-Za-z0-9]", "", text)) >= 2


class _Cards(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.section = ""
        self.title_depth = 0
        self.title_buf: list[str] = []
        self.card: dict[str, str] | None = None
        self.card_depth = 0
        self.field = ""
        self.field_depth = 0
        self.ignore = 0
        self.cards: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        classes = set((dict(attrs).get("class") or "").split())
        if self.ignore:
            self.ignore += 1
            if tag in _VOID_TAGS:
                self.handle_endtag(tag)
            return
        if _skip_classes(classes):
            self.ignore = 1
            if tag in _VOID_TAGS:
                self.handle_endtag(tag)
            return
        if "core-section-container__title" in classes and self.title_depth == 0:
            self.title_depth = 1
            self.title_buf = []
        elif self.title_depth:
            self.title_depth += 1
        if self.card is not None:
            self.card_depth += 1
            if tag in {"h3", "h4"} and not self.field:
                self.field = tag
                self.field_depth = 1
            elif "text-color-text-low-emphasis" in classes and not self.field:
                self.field = "meta"
                self.field_depth = 1
            elif self.field:
                self.field_depth += 1
        elif "profile-section-card" in classes:
            self.card = {"section": self.section, "h3": "", "h4": "", "meta": ""}
            self.card_depth = 1
        if tag in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.ignore:
            self.ignore -= 1
            return
        if self.title_depth:
            self.title_depth -= 1
            if self.title_depth == 0:
                self.section = _field(" ".join(self.title_buf))
                self.title_buf = []
        if self.field:
            self.field_depth -= 1
            if self.field_depth <= 0:
                self.field = ""
        if self.card is not None:
            self.card_depth -= 1
            if self.card_depth <= 0:
                self.cards.append(self.card)
                self.card = None

    def handle_data(self, data):
        if self.ignore or not data.strip():
            return
        if self.title_depth:
            self.title_buf.append(data)
        if self.card is not None and self.field in {"h3", "h4", "meta"}:
            self.card[self.field] = _field(f"{self.card[self.field]} {data}")


def _skip_classes(classes: set[str]) -> bool:
    if "blur" in classes:
        return True
    for token in classes:
        if token in {"blurred-content", "blurred-list"} or "sign-in-modal" in token:
            return True
    return False


def _read_cards(page: str) -> list[dict[str, str]]:
    parser = _Cards()
    parser.feed(page)
    return parser.cards


def _pair(h3: str, h4: str) -> tuple[str, str]:
    h3_role = bool(_ROLE.search(h3))
    h4_role = bool(_ROLE.search(h4))
    if h3_role and not h4_role:
        return h4, h3
    return h3, h4


def _jobs_from_cards(cards: list[dict[str, str]]) -> list[dict[str, str]]:
    jobs: list[dict[str, str]] = []
    for card in cards:
        kind = _section_kind(card["section"])
        if kind not in {"experience", "organization", "recommendation"}:
            continue
        h3 = _field(card["h3"])
        h4 = _field(card["h4"])
        start, end = _span_from(card["meta"])
        if not start and not end:
            start, end = _span_from(h4)
            if start or end:
                h4 = ""
        if _is_education_card(h3, h4, card["meta"]):
            continue
        company, title = _pair(h3, h4)
        if not _usable(company) and not _usable(title):
            continue
        if kind == "organization" and not start and not end:
            continue
        if kind == "recommendation" and not h4 and not start and not end:
            continue
        jobs.append(_blank_job(company, title, start, end))
    return _dedupe_jobs(jobs)


def _is_education_card(h3: str, h4: str, meta: str) -> bool:
    if _is_school(h3) or _is_school(h4) or _DEGREE.search(h3) or _DEGREE.search(h4):
        return True
    plain = _strip_duration(meta)
    return bool(
        re.fullmatch(r"(?:19|20)\d{2}\s*[-–—]\s*(?:19|20)\d{2}", plain)
        and not _ROLE.search(h3)
        and not _ROLE.search(h4)
    )


def _schools_from_cards(cards: list[dict[str, str]]) -> list[dict[str, str]]:
    schools: list[dict[str, str]] = []
    for card in cards:
        if _section_kind(card["section"]) != "experience":
            continue
        h3 = _field(card["h3"])
        h4 = _field(card["h4"])
        if not _is_education_card(h3, h4, card["meta"]):
            continue
        school, degree, field = "", "", ""
        if _is_school(h3) or (_DEGREE.search(h4) and not _DEGREE.search(h3)):
            school = h3
            degree, field = _split_degree(h4) if h4 else ("", "")
        elif _is_school(h4) or _DEGREE.search(h3):
            school = "" if _DEGREE.search(h4) else h4
            degree, field = _split_degree(h3) if h3 else ("", "")
        elif _DEGREE.search(h4):
            degree, field = _split_degree(h4)
            school = h3
        start, end = _span_from(card["meta"])
        if end == "Present":
            year = "In Progress"
        elif end and len(end) == 4:
            year = end
        elif start and len(start) == 4 and not end:
            year = start
        else:
            year = end[:4] if len(end) >= 4 else ""
        if not _usable(school) and not _usable(degree):
            continue
        schools.append(_blank_school(school, degree, field, year))
    return schools


def _job_key(job: dict[str, str]) -> tuple[str, str]:
    return (job["job_title"].lower(), re.sub(r"[^a-z0-9]", "", job["company"].lower()))


def _dedupe_jobs(jobs: list[dict[str, str]]) -> list[dict[str, str]]:
    kept: list[dict[str, str]] = []
    index: dict[tuple[str, str], int] = {}
    for job in jobs:
        key = _job_key(job)
        if key in index:
            current = kept[index[key]]
            if not current["start_date"] and job["start_date"]:
                current["start_date"] = job["start_date"]
                current["end_date"] = job["end_date"]
            if not current["job_title"] and job["job_title"]:
                current["job_title"] = job["job_title"]
            continue
        index[key] = len(kept)
        kept.append(job)
    titled = [job for job in kept if job["job_title"] or job["start_date"] or job["end_date"]]
    if titled:
        kept = [
            job for job in kept
            if job["job_title"] or job["start_date"] or job["end_date"]
            or not any(_same_org(job["company"], other["company"]) for other in titled)
        ]
    kept.sort(key=_job_sort_key, reverse=True)
    return kept


def _merge_jobs(primary: list[dict[str, str]], extra: list[dict[str, str]]) -> list[dict[str, str]]:
    return _dedupe_jobs([*primary, *extra])


def _merge_schools(primary: list[dict[str, str]], extra: list[dict[str, str]]) -> list[dict[str, str]]:
    kept = list(primary)
    for school in extra:
        if any(
            _same_org(school["school"], item["school"]) and school["degree"].lower() == item["degree"].lower()
            for item in kept
        ):
            continue
        kept.append(school)
    kept.sort(
        key=lambda item: "9999" if item["graduation_year"] == "In Progress" else (item["graduation_year"] or "0000"),
        reverse=True,
    )
    return kept


def _experience(lines: list[str]) -> list[dict[str, str]]:
    jobs: list[dict[str, str]] = []
    for index, line in enumerate(lines):
        if not _DATE_LINE.match(line):
            continue
        company = _previous(lines, index)
        title = _previous(lines, index, skip=1)
        if company in {"-", "–", "—"}:
            company = ""
        if title in {"-", "–", "—"}:
            title = ""
        if not title and not company:
            continue
        start, end = _split_span(line)
        jobs.append({
            "company": company[:160],
            "job_title": title[:160],
            "start_date": start,
            "end_date": end,
            "responsibilities": "",
        })
    jobs.sort(key=_job_sort_key, reverse=True)
    return jobs[:20]


def _education(lines: list[str]) -> list[dict[str, str]]:
    schools: list[dict[str, str]] = []
    consumed: set[int] = set()
    for index, line in enumerate(lines):
        in_progress = line.lower() in {"in progress", "present", "current"}
        if not _DATE_LINE.match(line) and not in_progress:
            continue
        first_index = _previous_index(lines, index)
        second_index = _previous_index(lines, index, skip=1)
        first = lines[first_index] if first_index is not None else ""
        second = lines[second_index] if second_index is not None else ""
        for used in (index, first_index, second_index):
            if used is not None:
                consumed.add(used)
        degree, field, school = "", "", ""
        if _is_school(first) or not second:
            school = "" if first in {"-", "–", "—"} else first
        elif first in {"-", "–", "—"}:
            school = second
        else:
            degree, field = _split_degree(first)
            school = second if second and second not in {"-", "–", "—"} else ""
            if not _is_school(school) and _is_school(first):
                school = first
                degree, field = "", ""
        year = "In Progress" if in_progress else _end_year(line)
        if not school and not degree:
            continue
        schools.append({
            "school": school[:160],
            "degree": degree[:120],
            "field_of_study": field[:120],
            "graduation_year": year,
            "grade": "",
        })
    for index, line in enumerate(lines):
        if index in consumed or not _is_school(line):
            continue
        schools.append({
            "school": line[:160],
            "degree": "",
            "field_of_study": "",
            "graduation_year": "",
            "grade": "",
        })
    schools.sort(
        key=lambda item: "9999" if item["graduation_year"] == "In Progress" else (item["graduation_year"] or "0000"),
        reverse=True,
    )
    return schools[:12]


def _skills(lines: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if _DATE_LINE.match(line) or _DURATION.match(line) or line.lower() in {"skills", "top skills"}:
            continue
        for part in re.split(r"[,•·]+", line):
            skill = part.strip(" .")
            key = skill.lower()
            if 1 < len(skill) <= 60 and key not in seen:
                seen.add(key)
                found.append(skill)
    return found[:60]


def _previous_index(lines: list[str], index: int, skip: int = 0) -> int | None:
    seen = 0
    cursor = index - 1
    while cursor >= 0:
        line = lines[cursor]
        if _DATE_LINE.match(line) or _DURATION.match(line) or line.lower() in {"experience", "education", "skills"}:
            cursor -= 1
            continue
        if seen == skip:
            return cursor
        seen += 1
        cursor -= 1
    return None


def _previous(lines: list[str], index: int, skip: int = 0) -> str:
    found = _previous_index(lines, index, skip)
    if found is None:
        return ""
    return lines[found]


def _split_span(text: str) -> tuple[str, str]:
    match = re.search(
        r"(?P<start>.+?)\s*[-–—]\s*(?P<end>Present|Current|Now|.+)$",
        text,
        re.I,
    )
    if not match:
        return "", ""
    end = match.group("end").strip()
    current = end.lower() in {"present", "current", "now"}
    return _normalize_date(match.group("start").strip()), _normalize_date(end, current=current)


def _end_year(text: str) -> str:
    years = re.findall(r"(?:19|20)\d{2}", text)
    return years[-1] if years else ""


def _is_school(line: str) -> bool:
    lowered = line.lower()
    return any(token in lowered for token in _SCHOOL)


def _split_degree(text: str) -> tuple[str, str]:
    if " - " in text:
        degree, field = text.split(" - ", 1)
        return degree.strip(), field.strip()
    if "," in text:
        degree, field = text.split(",", 1)
        return degree.strip(), field.strip()
    return text.strip(), ""
