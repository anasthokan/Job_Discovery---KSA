"""Saudi Arabia regions and cities. The jobs.state column stores a region name."""

from __future__ import annotations

import re

# Region -> cities and Arabic names people actually search.
REGIONS: dict[str, list[str]] = {
    "Riyadh": ["riyadh", "الرياض"],
    "Makkah": ["jeddah", "jidda", "jedda", "makkah", "mecca", "taif", "جدة", "مكة", "الطائف"],
    "Madinah": ["madinah", "medina", "yanbu", "المدينة", "ينبع"],
    "Eastern Province": ["dammam", "khobar", "al khobar", "dhahran", "jubail", "qatif", "الدمام", "الخبر", "الظهران", "الجبيل"],
    "Qassim": ["buraidah", "buraydah", "unaizah", "بريدة", "القصيم"],
    "Asir": ["abha", "khamis mushait", "أبها"],
    "Tabuk": ["tabuk", "neom", "تبوك"],
    "Hail": ["hail", "ha'il", "حائل"],
    "Northern Borders": ["arar", "عرعر"],
    "Jazan": ["jazan", "jizan", "جازان"],
    "Najran": ["najran", "نجران"],
    "Al Bahah": ["al bahah", "al-baha", "al baha", "الباحة"],
    "Al Jawf": ["sakaka", "al jawf", "سكاكا", "الجوف"],
}

# City JobSpy / Indeed should search when the user picks a region.
REGION_SEARCH = {
    "Riyadh": "Riyadh",
    "Makkah": "Jeddah",
    "Madinah": "Madinah",
    "Eastern Province": "Dammam",
    "Qassim": "Buraidah",
    "Asir": "Abha",
    "Tabuk": "Tabuk",
    "Hail": "Hail",
    "Northern Borders": "Arar",
    "Jazan": "Jazan",
    "Najran": "Najran",
    "Al Bahah": "Al Bahah",
    "Al Jawf": "Sakaka",
}

CITY_HINTS = {
    "riyadh": "Riyadh",
    "jeddah": "Jeddah",
    "jidda": "Jeddah",
    "jedda": "Jeddah",
    "makkah": "Makkah",
    "mecca": "Makkah",
    "taif": "Taif",
    "madinah": "Madinah",
    "medina": "Madinah",
    "yanbu": "Yanbu",
    "dammam": "Dammam",
    "khobar": "Khobar",
    "al khobar": "Khobar",
    "dhahran": "Dhahran",
    "jubail": "Jubail",
    "qatif": "Qatif",
    "buraidah": "Buraidah",
    "buraydah": "Buraidah",
    "abha": "Abha",
    "tabuk": "Tabuk",
    "neom": "NEOM",
    "hail": "Hail",
    "jazan": "Jazan",
    "najran": "Najran",
    "remote": "Remote",
}

DROPDOWN_STATES = list(REGIONS.keys())

KSA_SCOPED_PLATFORMS = frozenset({
    "LinkedIn",
    "Indeed",
    "Google Jobs",
    "Bayt",
    "Dev Global Jobs",
})

_KSA_RE = re.compile(
    r"(?:saudi(?:\s+arabia)?|\bksa\b|kingdom of saudi|المملكة العربية السعودية|السعودية)",
    re.I,
)
_FOREIGN_RE = re.compile(
    r"\b(?:united states|u\.s\.a\.?|u\.s\.|usa|united kingdom|\buk\b|england|london|"
    r"india|bengaluru|bangalore|hyderabad|mumbai|pune|chennai|pakistan|"
    r"uae|united arab emirates|dubai|abu dhabi|sharjah|"
    r"qatar|doha|kuwait|bahrain|manama|oman|muscat|"
    r"egypt|cairo|jordan|amman|lebanon|beirut|"
    r"canada|toronto|australia|sydney|germany|berlin|france|paris|"
    r"singapore|philippines|manila)\b",
    re.I,
)
_PLAIN_REMOTE = {
    "remote", "hybrid", "distributed", "wfh", "work", "from", "home",
    "flexible", "anywhere", "unspecified", "na", "n", "a",
}


def region_name(value: str) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "all":
        return ""
    if text in REGIONS:
        return text
    lower = text.lower()
    for region, cities in REGIONS.items():
        if lower == region.lower():
            return region
        if lower in cities:
            return region
    hinted = CITY_HINTS.get(lower)
    if hinted and hinted != "Remote":
        return region_name(hinted)
    return ""


def muse_location(city: str, region: str) -> str:
    city = (city or "").strip()
    if city:
        return CITY_HINTS.get(city.lower(), city)
    if region == "Remote":
        return "Remote"
    return REGION_SEARCH.get(region or "", "")


def matches_city_filter(job_location: str, city: str) -> bool:
    if not city:
        return True
    needle = city.lower().strip()
    haystack = (job_location or "").lower()
    if needle in haystack:
        return True
    hint = CITY_HINTS.get(needle, "")
    return bool(hint and hint.lower() in haystack)


def _place_hit(text: str) -> str:
    lower = (text or "").lower()
    for region, cities in REGIONS.items():
        if region.lower() in lower:
            return region
        for city in cities:
            if city.isascii():
                if re.search(rf"\b{re.escape(city)}\b", lower):
                    return region
            elif city in lower:
                return region
    return ""


def parse_state(location: str) -> str:
    if not location:
        return "Saudi Arabia"
    found = _place_hit(location)
    if found:
        return found
    if _KSA_RE.search(location):
        if re.search(r"\bremote\b", location, re.I):
            return "Remote"
        return "Saudi Arabia"
    if re.search(r"\b(?:remote|work from home|wfh)\b", location, re.I):
        return "Remote"
    return location.strip()[:48] or "Saudi Arabia"


def _is_plain_remote(job_state: str, job_location: str) -> bool:
    raw = f"{job_state or ''} {job_location or ''}".strip().lower()
    if not raw:
        return True
    words = re.sub(r"[^a-z]+", " ", raw).split()
    return bool(words) and all(word in _PLAIN_REMOTE for word in words)


def is_ksa_job(job_state: str, job_location: str = "", *, allow_bare_remote: bool = False) -> bool:
    """Keep Saudi Arabia listings. Drop other countries."""
    state = (job_state or "").strip()
    location = job_location or ""
    text = f"{state} {location}".strip()
    if _place_hit(text) or state in REGIONS or _KSA_RE.search(text):
        return True
    if _FOREIGN_RE.search(text):
        return False
    if state == "Saudi Arabia":
        return True
    if allow_bare_remote and (state == "Remote" or _is_plain_remote(state, location)):
        return True
    return False


def normalize_scraped_location(
    job_state: str,
    job_location: str,
    platform: str = "",
    description: str = "",
) -> tuple[str, str] | None:
    """Return region/location for a KSA listing, or None when it should not be saved."""
    state = (job_state or "").strip()
    location = job_location or ""
    scoped = (platform or "") in KSA_SCOPED_PLATFORMS
    if scoped and _is_plain_remote(state, location):
        state = "Remote"
        location = "Remote, Saudi Arabia"
    elif (
        scoped
        and not _place_hit(f"{state} {location}")
        and not _KSA_RE.search(f"{state} {location}")
        and _KSA_RE.search(description or "")
        and not _FOREIGN_RE.search(f"{state} {location}")
    ):
        state = "Saudi Arabia"
        location = location or "Saudi Arabia"
    if not is_ksa_job(state, location, allow_bare_remote=scoped):
        return None
    return state, location


def matches_state_filter(job_state: str, job_location: str, selected: str) -> bool:
    if not is_ksa_job(job_state, job_location, allow_bare_remote=True):
        foreign = _FOREIGN_RE.search(f"{job_state} {job_location}")
        saudi = _place_hit(f"{job_state} {job_location}") or _KSA_RE.search(f"{job_state} {job_location}")
        if foreign and not saudi:
            return False
    if not selected or selected == "All":
        return True
    haystack = f"{job_state} {job_location}".lower()
    if selected == "Remote":
        return (job_state or "") == "Remote" or "remote" in haystack
    if job_state == selected or selected.lower() in haystack:
        return True
    for city in REGIONS.get(selected, []):
        if city in haystack:
            return True
    return False
