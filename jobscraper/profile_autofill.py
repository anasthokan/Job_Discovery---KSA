"""Map a parsed resume onto the profile autofill form.

Uses the existing resume parser and does not change its response.
Any form field that is not present on the resume is an empty string.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from jobscraper.locations import CITY_HINTS, region_name
from jobscraper.resume_parser import TITLE_HINTS, parse_resume_bytes, parse_resume_text

_DIAL_CODES = (
    "966", "971", "965", "974", "973", "968", "964", "962", "961", "963",
    "970", "967", "880", "977", "966", "212", "213", "216", "234", "249",
    "251", "254", "255", "256", "20", "91", "92", "94", "98", "90", "86",
    "81", "82", "84", "66", "65", "63", "62", "61", "60", "49", "48", "47",
    "46", "44", "43", "41", "39", "34", "33", "32", "31", "27", "1",
)

_MONTHS = {
    "jan": "01", "january": "01",
    "feb": "02", "february": "02",
    "mar": "03", "march": "03",
    "apr": "04", "april": "04",
    "may": "05",
    "jun": "06", "june": "06",
    "jul": "07", "july": "07",
    "aug": "08", "august": "08",
    "sep": "09", "sept": "09", "september": "09",
    "oct": "10", "october": "10",
    "nov": "11", "november": "11",
    "dec": "12", "december": "12",
}

_NATIONALITY = {
    "saudi": "Saudi",
    "saudi arabian": "Saudi",
    "ksa": "Saudi",
    "indian": "Indian",
    "pakistani": "Pakistani",
    "egyptian": "Egyptian",
    "filipino": "Filipino",
    "philippine": "Filipino",
    "jordanian": "Jordanian",
    "lebanese": "Lebanese",
    "syrian": "Syrian",
    "yemeni": "Yemeni",
    "sudanese": "Sudanese",
    "bangladeshi": "Bangladeshi",
    "nepalese": "Nepalese",
    "nepali": "Nepalese",
    "sri lankan": "Sri Lankan",
    "emirati": "Emirati",
    "kuwaiti": "Kuwaiti",
    "bahraini": "Bahraini",
    "qatari": "Qatari",
    "omani": "Omani",
    "american": "American",
    "british": "British",
    "canadian": "Canadian",
    "palestinian": "Palestinian",
    "indonesian": "Indonesian",
    "turkish": "Turkish",
    "moroccan": "Moroccan",
    "tunisian": "Tunisian",
    "algerian": "Algerian",
}

_COUNTRIES = {
    "saudi arabia": "Saudi Arabia",
    "saudi": "Saudi Arabia",
    "ksa": "Saudi Arabia",
    "kingdom of saudi arabia": "Saudi Arabia",
    "united states": "United States",
    "united states of america": "United States",
    "usa": "United States",
    "u.s.a.": "United States",
    "u.s.": "United States",
    "us": "United States",
    "united kingdom": "United Kingdom",
    "uk": "United Kingdom",
    "india": "India",
    "pakistan": "Pakistan",
    "egypt": "Egypt",
    "philippines": "Philippines",
    "jordan": "Jordan",
    "lebanon": "Lebanon",
    "united arab emirates": "United Arab Emirates",
    "uae": "United Arab Emirates",
    "qatar": "Qatar",
    "kuwait": "Kuwait",
    "bahrain": "Bahrain",
    "oman": "Oman",
    "canada": "Canada",
    "australia": "Australia",
}

_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

_SUMMARY_HEADS = {
    "summary",
    "professional summary",
    "executive summary",
    "profile",
    "about",
    "objective",
    "career objective",
}
_SKILL_HEADS = {
    "skills",
    "technical skills",
    "core skills",
    "key skills",
    "competencies",
    "core competencies",
    "technologies",
    "tech stack",
    "expertise",
}
_EXPERIENCE_HEADS = {
    "experience",
    "work experience",
    "professional experience",
    "employment",
    "employment history",
    "work history",
}
_MONTH_WORD = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)
_DATE_RANGE = re.compile(
    rf"(?P<start>(?:{_MONTH_WORD})\.?\s+(?:19|20)\d{{2}}|\d{{1,2}}[/-](?:19|20)\d{{2}}|(?:19|20)\d{{2}})"
    rf"\s*(?:[-–—]|to)\s+"
    rf"(?P<end>Present|Current|Now|(?:{_MONTH_WORD})\.?\s+(?:19|20)\d{{2}}|\d{{1,2}}[/-](?:19|20)\d{{2}}|(?:19|20)\d{{2}})",
    re.I,
)
_CERT_HEADS = {
    "certifications",
    "certification",
    "certificates",
    "certificate",
    "licenses",
    "licenses & certifications",
    "licenses and certifications",
    "professional certifications",
    "professional certificates",
}
_SECTION_HEADS = _SUMMARY_HEADS | _SKILL_HEADS | _EXPERIENCE_HEADS | _CERT_HEADS | {
    "key achievements",
    "achievements",
    "education",
    "academic background",
    "projects",
    "languages",
    "contact",
    "national address",
    "address",
}

_PORTFOLIO_HINTS = ("portfolio", "behance.", "dribbble.", "cargo.site")


def autofill_from_bytes(data: bytes, filename: str = "", content_type: str = "") -> dict[str, Any]:
    parsed = parse_resume_bytes(data, filename=filename, content_type=content_type)
    return autofill_from_parsed(parsed)


def autofill_from_text(text: str) -> dict[str, Any]:
    parsed = parse_resume_text(text)
    return autofill_from_parsed(parsed)


def autofill_from_parsed(parsed: dict[str, Any]) -> dict[str, Any]:
    """Shape `parse_resume_text` output into the profile form. Missing fields are ""."""
    raw = str(parsed.get("raw_text") or "")
    profile = parsed.get("profile") or {}
    personal = _personal(profile, raw)
    address = _address(profile, raw)
    online = _online(profile, raw)
    jobs = _jobs(parsed.get("experience") or [], raw)
    schools = _schools(parsed.get("education") or [], raw)
    certificates = _certifications(parsed, raw)
    summary = _summary(profile, raw)
    skills = _skills_text(parsed, raw)

    filled = {
        "executive_summary": summary,
        "work_experience": jobs,
        "education": schools,
        "certifications": certificates,
        "skills": skills,
        "personal": personal,
        "national_address": address,
        "online_profiles": online,
        "work_authorization": _work_authorization(raw, personal.get("nationality") or ""),
        "logistics": _logistics(raw),
        "optional_identifiers": _optional_identifiers(raw),
        "custom_answers": [],
    }
    source = parsed.get("source") or {}
    return {
        "ok": True,
        "source": {
            "filename": _text(source.get("filename")),
            "content_type": _text(source.get("content_type")),
            "pages": source.get("pages"),
            "char_count": int(source.get("char_count") or 0),
        },
        "executive_summary": filled["executive_summary"],
        "work_experience": filled["work_experience"],
        "education": filled["education"],
        "certifications": filled["certifications"],
        "skills": filled["skills"],
        "personal": filled["personal"],
        "national_address": filled["national_address"],
        "online_profiles": filled["online_profiles"],
        "work_authorization": filled["work_authorization"],
        "logistics": filled["logistics"],
        "optional_identifiers": filled["optional_identifiers"],
        "custom_answers": filled["custom_answers"],
    }


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clip(value: str, limit: int) -> str:
    return _text(value)[:limit]


def _label(text: str, labels: tuple[str, ...], limit: int = 160) -> str:
    pattern = re.compile(
        rf"(?:{'|'.join(re.escape(label) for label in labels)})\s*[:\-]\s*(.+)",
        re.I,
    )
    for line in text.splitlines():
        match = pattern.search(line.strip())
        if not match:
            continue
        value = match.group(1).strip(" .|")
        value = re.split(r"\s{2,}|\s\|\s", value, maxsplit=1)[0].strip()
        if value:
            return _clip(value, limit)
    return ""


def _fixed_digits(value: str, length: int) -> str:
    match = re.search(rf"\b(\d{{{length}}})\b", value or "")
    return match.group(1) if match else ""


def _heading_key(line: str) -> str:
    compact = re.sub(r"[\s|:.\-]+", " ", line).strip().lower().strip(":-")
    return compact


def _section_lines(raw: str, headings: set[str]) -> list[str]:
    capture = False
    lines: list[str] = []
    for line in raw.splitlines():
        key = _heading_key(line)
        if capture:
            if key in _SECTION_HEADS and key not in headings:
                break
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
            continue
        if key in headings:
            capture = True
    return lines


def _section_body(raw: str, headings: set[str]) -> str:
    text = " ".join(_section_lines(raw, headings))
    return re.sub(r"\s+", " ", text).strip()


def _skills_text(parsed: dict, raw: str) -> str:
    found: list[str] = []
    seen: set[str] = set()
    for skill in _skill_phrases(_section_lines(raw, _SKILL_HEADS)):
        key = skill.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(skill)
    if not found:
        for skill in parsed.get("skills") or []:
            text = _text(skill)
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                found.append(text)
    return ", ".join(found[:60])


def _skill_phrases(lines: list[str]) -> list[str]:
    """Join wrapped competency lines, then split on commas. Category labels are dropped."""
    grouped: list[str] = []
    bucket: list[str] = []
    labeled = any(line.endswith(":") for line in lines)
    for line in lines:
        if line.endswith(":"):
            if bucket:
                grouped.append(" ".join(bucket))
                bucket = []
            continue
        bucket.append(line.strip("• ").strip())
    if bucket:
        grouped.append(" ".join(bucket))
    chunks = grouped if labeled else [" ".join(lines)]
    phrases: list[str] = []
    for chunk in chunks:
        for part in re.split(r"[,;•·]+", chunk):
            skill = re.sub(r"^[\-–—*]\s*", "", part).strip(" .")
            skill = re.sub(
                r"^(?:languages|frameworks|tools|databases|cloud|soft skills)\s*[:\-]\s*",
                "",
                skill,
                flags=re.I,
            )
            if 1 < len(skill) <= 60 and "@" not in skill and not skill.lower().startswith("http"):
                phrases.append(skill)
    return phrases


def _summary(profile: dict, raw: str) -> str:
    extracted = _section_body(raw, _SUMMARY_HEADS)
    if len(extracted) >= 40:
        return _clip(extracted, 2000)
    found = re.split(r"\bkey achievements\b", _text(profile.get("summary")), maxsplit=1, flags=re.I)[0].strip()
    if len(found) < 40:
        return ""
    return _clip(found, 2000)


def _split_name(full: str) -> tuple[str, str]:
    cleaned = re.sub(r"\s+", " ", full).strip()
    cleaned = re.sub(r",?\s+(?:Ph\.?D\.?|M\.?S\.?c?\.?|B\.?S\.?c?\.?|MBA|PMP|CPA)$", "", cleaned, flags=re.I)
    parts = [part for part in cleaned.split(" ") if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _normalize_nationality(value: str) -> str:
    text = _text(value).strip(" .")
    if not text:
        return ""
    mapped = _NATIONALITY.get(text.lower())
    if mapped:
        return mapped
    if 1 < len(text) <= 40 and re.fullmatch(r"[A-Za-z][A-Za-z \-']+", text):
        return text.title()
    return ""


def _personal(profile: dict, raw: str) -> dict[str, str]:
    first, last = _split_name(_text(profile.get("full_name")))
    labeled_first = _label(raw, ("first name", "given name"), 80)
    labeled_last = _label(raw, ("last name", "family name", "surname"), 80)
    if labeled_first:
        first = labeled_first
    if labeled_last:
        last = labeled_last
    preferred = _label(raw, ("preferred name", "nickname", "known as", "goes by"), 80)
    nationality = _normalize_nationality(_label(raw, ("nationality", "citizenship", "الجنسية"), 40))
    if not nationality:
        match = re.search(
            r"\b(Saudi(?:\s+Arabian)?|Indian|Pakistani|Egyptian|Filipino|Jordanian|Lebanese|"
            r"Syrian|Yemeni|Sudanese|Bangladeshi|Nepalese|Emirati|American|British|Canadian)"
            r"\s+(?:national|citizen)\b",
            raw,
            re.I,
        )
        if match:
            nationality = _normalize_nationality(match.group(1))
    code, number = _split_phone(_text(profile.get("phone")))
    return {
        "first_name": _clip(first, 80),
        "last_name": _clip(last, 80),
        "preferred_name": preferred,
        "email": _clip(_text(profile.get("email")), 160),
        "phone_country_code": code,
        "phone_number": _clip(number, 32),
        "nationality": nationality,
    }


def _split_phone(raw: str) -> tuple[str, str]:
    text = _text(raw)
    if not text:
        return "", ""
    compact = re.sub(r"[^\d+]", "", text)
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    digits = re.sub(r"\D", "", compact)
    has_intl = compact.startswith("+") or text.startswith("+") or text.startswith("00")
    code = ""
    national = digits
    if has_intl or digits.startswith("966"):
        for dial in sorted(set(_DIAL_CODES), key=len, reverse=True):
            if not digits.startswith(dial) or len(digits) - len(dial) < 6:
                continue
            if not has_intl and dial != "966":
                continue
            code = "+" + dial
            national = digits[len(dial):]
            break
    if not code and len(digits) == 10 and digits.startswith("05"):
        code = "+966"
        national = digits[1:]
    if code and national.startswith("0"):
        national = national[1:]
    if not code:
        return "", text
    return code, _national_display(text, code, national)


def _national_display(original: str, code: str, national_digits: str) -> str:
    dial = code[1:]
    stripped = re.sub(rf"^\s*(?:\+|00)?\s*{dial}[\s.\-]*", "", original.strip())
    stripped = stripped.strip(" -")
    left = re.sub(r"\D", "", stripped)
    if left.lstrip("0") == national_digits.lstrip("0") and stripped:
        if stripped.startswith("0"):
            return stripped[1:].strip() or national_digits
        return stripped
    return national_digits


def _normalize_country(value: str) -> str:
    text = _text(value).strip(" .")
    if not text:
        return ""
    mapped = _COUNTRIES.get(text.lower())
    if mapped:
        return mapped
    if len(text) <= 3:
        return ""
    if len(text) <= 60 and re.fullmatch(r"[A-Za-z][A-Za-z \-'.]+", text):
        return text.title()
    return ""


def _saudi_context(raw: str, address: dict[str, str]) -> bool:
    if address.get("country") == "Saudi Arabia":
        return True
    if region_name(address.get("city") or "") or region_name(address.get("region") or ""):
        return True
    return bool(re.search(r"saudi(?:\s+arabia)?|\bksa\b|المملكة|السعودية", raw, re.I))


def _address(profile: dict, raw: str) -> dict[str, str]:
    address = {
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
    _apply_location(address, _text(profile.get("location")))
    street = _label(raw, ("street", "street name", "شارع"), 160)
    district = _label(raw, ("district", "neighbourhood", "neighborhood", "الحي"), 80)
    city = _label(raw, ("city", "المدينة"), 80)
    region = _label(raw, ("region", "province", "المنطقة"), 80)
    country = _normalize_country(_label(raw, ("country", "الدولة"), 60))
    building = _fixed_digits(_label(raw, ("building number", "building no", "building", "bldg", "رقم المبنى"), 40), 4)
    postal = _fixed_digits(_label(raw, ("postal code", "postcode", "zip code", "zip", "الرمز البريدي"), 20), 5)
    additional = _fixed_digits(_label(raw, ("additional number", "additional no", "الرقم الإضافي"), 20), 4)
    short = _label(raw, ("short address", "العنوان المختصر"), 20)
    short_match = re.fullmatch(r"[A-Za-z]{4}\d{4}", short.replace(" ", ""))
    if street:
        address["street"] = street
    if district:
        address["district"] = district
    if city:
        hinted = CITY_HINTS.get(city.lower())
        address["city"] = hinted if hinted and hinted != "Remote" else city
    if region:
        address["region"] = region_name(region) or region
    if country:
        address["country"] = country
    if building:
        address["building_number"] = building
    if postal:
        address["postal_code"] = postal
    if additional:
        address["additional_number"] = additional
    if short_match:
        address["short_address"] = short_match.group(0).upper()
    _apply_loose_national_address(address, raw)
    if address["city"] and not address["region"]:
        mapped = region_name(address["city"])
        if mapped:
            address["region"] = mapped
    if not country and (region_name(address["city"]) or region_name(address["region"])):
        address["country"] = "Saudi Arabia"
    return address


def _apply_location(address: dict[str, str], location: str) -> None:
    if not location:
        return
    parts = [part.strip() for part in re.split(r"[,|/]", location) if part.strip()]
    if not parts:
        return
    hinted = ""
    for part in parts:
        candidate = CITY_HINTS.get(part.lower())
        if candidate and candidate != "Remote":
            hinted = candidate
            break
    if hinted:
        address["city"] = hinted
        mapped = region_name(hinted)
        if mapped:
            address["region"] = mapped
        address["country"] = "Saudi Arabia"
        for part in parts:
            if part.lower() in {hinted.lower(), (address["region"] or "").lower()}:
                continue
            if _normalize_country(part) == "Saudi Arabia":
                continue
            if not address["district"] and not any(char.isdigit() for char in part):
                address["district"] = part
                break
        return
    if parts[0].lower() == "remote":
        return
    if not any(char.isdigit() for char in parts[0]):
        address["city"] = parts[0]
    if len(parts) < 2:
        return
    tail = parts[-1]
    country = _normalize_country(tail)
    if country:
        address["country"] = country
    elif tail.upper() in _US_STATES:
        address["country"] = "United States"
    mapped = region_name(parts[1] if len(parts) > 2 else tail)
    if mapped:
        address["region"] = mapped
        if not address["country"]:
            address["country"] = "Saudi Arabia"


def _apply_loose_national_address(address: dict[str, str], raw: str) -> None:
    saudi = _saudi_context(raw, address)
    if not address["short_address"]:
        for match in re.finditer(r"\b([A-Za-z]{4}\d{4})\b", raw):
            before = raw[max(0, match.start() - 40):match.start()].lower()
            if "http" in before or "github" in before or "linkedin" in before:
                continue
            context = raw[max(0, match.start() - 60):match.end() + 40].lower()
            if any(token in context for token in ("address", "national", "عنوان", "riyadh", "saudi", "building", "postal")):
                address["short_address"] = match.group(1).upper()
                break
    if address["postal_code"] and address["additional_number"]:
        return
    if not saudi and "postal" not in raw.lower() and "address" not in raw.lower():
        return
    for match in re.finditer(r"\b(\d{5})\s*-\s*(\d{4})\b", raw):
        context = raw[max(0, match.start() - 80):match.end() + 40].lower()
        if not any(token in context for token in ("address", "postal", "riyadh", "saudi", "building", "رمز", "إضافي")):
            continue
        if not address["postal_code"]:
            address["postal_code"] = match.group(1)
        if not address["additional_number"]:
            address["additional_number"] = match.group(2)
        break


def _keep_profile_urls(linkedin: str, portfolio: str, website: str) -> tuple[str, str, str]:
    def social(url: str) -> str:
        lowered = url.lower()
        if "linkedin.com" in lowered or "github.com" in lowered:
            return ""
        return url

    portfolio = social(portfolio)
    website = social(website)
    if portfolio and website and portfolio == website:
        website = ""
    return linkedin, portfolio, website


def _ensure_url(url: str) -> str:
    text = _text(url).rstrip(".,);")
    if not text:
        return ""
    if text.lower().startswith("www."):
        return "https://" + text
    if text.lower().startswith(("http://", "https://")):
        return text
    if "." in text:
        return "https://" + text
    return text


def _online(profile: dict, raw: str) -> dict[str, str]:
    linkedin = _ensure_url(_text(profile.get("linkedin")) or _label(raw, ("linkedin", "linkedin url"), 300))
    github = _ensure_url(_text(profile.get("github")) or _label(raw, ("github", "github url"), 300))
    portfolio = _ensure_url(_label(raw, ("portfolio", "portfolio url"), 300))
    website = _ensure_url(
        _label(raw, ("personal website", "personal site", "website", "web site"), 300)
        or _text(profile.get("website"))
    )
    linkedin, portfolio, website = _keep_profile_urls(linkedin, portfolio, website)
    if website and any(hint in website.lower() for hint in _PORTFOLIO_HINTS) and not portfolio:
        portfolio = website
        website = ""
    if portfolio and website and portfolio == website:
        website = ""
    for match in re.findall(r"https?://[^\s)>\]]+|www\.[^\s)>\]]+", raw, re.I):
        url = _ensure_url(match)
        lowered = url.lower()
        if not url or "linkedin.com" in lowered or "github.com" in lowered:
            continue
        if any(hint in lowered for hint in _PORTFOLIO_HINTS):
            if not portfolio:
                portfolio = url
            continue
        if not website:
            website = url
    return {
        "linkedin_url": _clip(linkedin, 300),
        "github_url": _clip(github, 300),
        "portfolio_url": _clip(portfolio, 300),
        "personal_website": _clip(website, 300),
    }


def _normalize_date(value: str, *, current: bool = False) -> str:
    if current:
        return "Present"
    text = _text(value)
    if not text:
        return ""
    if text.lower() in {"present", "current", "now"}:
        return "Present"
    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})(?:[-/.]\d{1,2})?", text)
    if match:
        month = int(match.group(2))
        if 1 <= month <= 12:
            return f"{match.group(1)}-{month:02d}"
        return match.group(1)
    match = re.fullmatch(r"(\d{1,2})[-/.](\d{4})", text)
    if match and 1 <= int(match.group(1)) <= 12:
        return f"{match.group(2)}-{int(match.group(1)):02d}"
    match = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{4})", text)
    if match and match.group(1).lower() in _MONTHS:
        return f"{match.group(2)}-{_MONTHS[match.group(1).lower()]}"
    if re.fullmatch(r"(?:19|20)\d{2}", text):
        return text
    return text


def _is_date_fragment(line: str) -> bool:
    text = _text(line).strip(" .")
    if not text or len(text) > 40:
        return False
    if text.lower() in {"present", "current", "now"}:
        return True
    patterns = (
        rf"(?:{_MONTH_WORD})\.?",
        rf"(?:{_MONTH_WORD})\.?\s+(?:19|20)\d{{2}}",
        rf"(?:{_MONTH_WORD})\.?\s+(?:19|20)\d{{2}}\s*(?:[-–—]|to)",
        rf"(?:[-–—]|to)\s+(?:{_MONTH_WORD})\.?\s+(?:19|20)\d{{2}}",
        rf"(?:[-–—]|to)\s+(?:19|20)\d{{2}}",
        rf"(?:19|20)\d{{2}}\s*(?:[-–—]|to)\s+(?:Present|Current|Now|(?:19|20)\d{{2}}|(?:{_MONTH_WORD})\.?\s+(?:19|20)\d{{2}})",
        rf"(?:19|20)\d{{2}}",
    )
    if any(re.fullmatch(pattern, text, re.I) for pattern in patterns):
        return True
    return bool(_DATE_RANGE.fullmatch(text))


def _join_pipes(lines: list[str]) -> list[str]:
    """Join a title, a lone '|', and the company when a PDF puts each on its own line."""
    joined: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if line.startswith("|") and joined:
            school = line.lstrip("|").strip()
            if school and not _is_date_fragment(school):
                joined[-1] = f"{joined[-1]} | {school}"
                index += 1
                continue
        if line == "|" and joined and index + 1 < len(lines):
            nxt = lines[index + 1].strip()
            if nxt and nxt not in {"|", "•"} and not _is_date_fragment(nxt):
                joined[-1] = f"{joined[-1]} | {nxt}"
                index += 2
                continue
        joined.append(re.sub(r"\s+\|\s+", " | ", line) if "|" in line else line)
        index += 1
    return joined


def _is_role_header(line: str) -> bool:
    if " | " not in line:
        return False
    parts = [part.strip(" -") for part in line.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return False
    title, company = parts[0], parts[1]
    if len(title) > 80 or len(company) > 80 or title.endswith(".") or company.endswith("."):
        return False
    if _is_date_fragment(title) or _is_date_fragment(company):
        return False
    words = title.split()
    if not (1 <= len(words) <= 10):
        return False
    lowered = title.lower()
    if any(hint in lowered for hint in TITLE_HINTS):
        return True
    return len(company.split()) <= 6


def _responsibility_text(lines: list[str]) -> str:
    bullets: list[str] = []
    current: list[str] = []
    for line in lines:
        marker = line.strip()
        if marker in {"•", "-", "*"} or marker.startswith(("• ", "- ", "* ")):
            if current:
                bullets.append(" ".join(current))
            text = marker.lstrip("•-* ").strip()
            current = [text] if text else []
            continue
        if current:
            current.append(line.strip())
        else:
            current = [line.strip()]
    if current:
        bullets.append(" ".join(part for part in current if part))
    return "\n".join(item for item in bullets if item)


def _jobs_from_raw(raw: str) -> list[dict[str, str]]:
    lines = _join_pipes(_section_lines(raw, _EXPERIENCE_HEADS))
    indexes = [index for index, line in enumerate(lines) if _is_role_header(line)]
    if not indexes:
        return []
    jobs: list[dict[str, str]] = []
    for number, start in enumerate(indexes):
        end = indexes[number + 1] if number + 1 < len(indexes) else len(lines)
        block = lines[start:end]
        parts = [part.strip() for part in block[0].split("|")]
        title = parts[0]
        company = parts[1] if len(parts) > 1 else ""
        date_bits: list[str] = []
        body: list[str] = []
        taking_dates = True
        for line in block[1:]:
            if taking_dates and _is_date_fragment(line):
                date_bits.append(line.strip(" ."))
                if _DATE_RANGE.search(" ".join(date_bits)):
                    taking_dates = False
                continue
            taking_dates = False
            body.append(line)
        match = _DATE_RANGE.search(" ".join(date_bits))
        start_date = _normalize_date(match.group("start")) if match else ""
        end_raw = match.group("end") if match else ""
        current = bool(end_raw and end_raw.lower() in {"present", "current", "now"})
        if not title and not company:
            continue
        jobs.append({
            "company": _clip(company, 160),
            "job_title": _clip(title, 160),
            "start_date": start_date,
            "end_date": _normalize_date(end_raw, current=current),
            "responsibilities": _clip(_responsibility_text(body), 4000),
        })
    jobs.sort(key=_job_sort_key, reverse=True)
    return jobs[:20]


def _jobs(rows: list[dict], raw: str = "") -> list[dict[str, str]]:
    direct = _jobs_from_raw(raw) if raw else []
    if direct:
        return direct
    jobs: list[dict[str, str]] = []
    for row in rows:
        title = _text(row.get("title"))
        company = _text(row.get("company"))
        if not title and not company:
            continue
        bullets = [_text(item) for item in (row.get("bullets") or []) if _text(item)]
        jobs.append({
            "company": _clip(company, 160),
            "job_title": _clip(title, 160),
            "start_date": _normalize_date(_text(row.get("start_date"))),
            "end_date": _normalize_date(_text(row.get("end_date")), current=bool(row.get("is_current"))),
            "responsibilities": _clip("\n".join(bullets), 4000),
        })
    jobs.sort(key=_job_sort_key, reverse=True)
    return jobs[:20]


_CERT_DATE = re.compile(
    rf"(?:,|\(|\s)+(?:issued\s+)?((?:{_MONTH_WORD})\.?\s+(?:19|20)\d{{2}}|(?:19|20)\d{{2}})\)?\s*$",
    re.I,
)
_CERT_ISSUER = re.compile(r"\s*(?:—|–|\|)\s*")


def _certifications(parsed: dict, raw: str) -> list[dict[str, str]]:
    lines = _join_cert_lines(_section_lines(raw, _CERT_HEADS))
    rows = _cert_rows(lines)
    if rows:
        return rows
    fallback = [_text(item) for item in (parsed.get("certifications") or []) if _text(item)]
    return _cert_rows(fallback)


def _join_cert_lines(lines: list[str]) -> list[str]:
    joined: list[str] = []
    for line in lines:
        marker = line.strip()
        if re.fullmatch(r"[\-–—*•.]+", marker):
            continue
        text = re.sub(r"^[\-–—*•]+\s*", "", marker).strip()
        if not text:
            continue
        if joined and re.search(r"(?:—|–|\|)\s*$", joined[-1]):
            issuer = re.sub(r"^[\-–—*•]+\s*", "", text).strip()
            joined[-1] = f"{joined[-1].rstrip(' —–|')} — {issuer}"
            continue
        joined.append(text)
    return joined


def _cert_rows(lines: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in lines:
        name, issuer, issued = _split_cert(line)
        if len(name) < 2:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "name": _clip(name, 160),
            "issuer": _clip(issuer, 160),
            "issue_date": issued,
        })
    return rows[:20]


def _split_cert(text: str) -> tuple[str, str, str]:
    body = re.sub(r"^[\-–—*•]+\s*", "", _text(text)).strip(" .")
    issued = ""
    dated = _CERT_DATE.search(body)
    if dated:
        issued = _normalize_date(dated.group(1))
        body = body[:dated.start()].strip(" ,.-")
    parts = _CERT_ISSUER.split(body, maxsplit=1)
    name = parts[0].strip(" .")
    issuer = parts[1].strip(" .") if len(parts) > 1 else ""
    if not issued and issuer:
        dated = _CERT_DATE.search(issuer)
        if dated and dated.start() > 0:
            issued = _normalize_date(dated.group(1))
            issuer = issuer[:dated.start()].strip(" ,.-")
    return name, issuer, issued


def _job_sort_key(job: dict[str, str]) -> str:
    for value in (job.get("end_date"), job.get("start_date")):
        if value == "Present":
            return "9999-99"
        if value and re.fullmatch(r"\d{4}(?:-\d{2})?", value):
            return value if "-" in value else value + "-12"
    return "0000-00"


def _merge_education(rows: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for row in rows:
        current = dict(row)
        school = _text(current.get("institution"))
        degree = _text(current.get("degree"))
        if merged:
            prev = merged[-1]
            prev_school = _text(prev.get("institution"))
            prev_degree = _text(prev.get("degree"))
            complements = (school and not degree and prev_degree and not prev_school) or (
                degree and not school and prev_school and not prev_degree
            )
            if complements:
                for key in ("degree", "field", "institution", "start_year", "end_year", "gpa"):
                    if not _text(prev.get(key)) and _text(current.get(key)):
                        prev[key] = current.get(key)
                continue
        merged.append(current)
    return merged


def _graduation_year(raw: str, school: str, degree: str, row: dict) -> str:
    for needle in (school, degree):
        if not needle or needle not in raw:
            continue
        window = raw[raw.find(needle):raw.find(needle) + 280]
        cleaned = re.sub(
            r"(?:date of birth|birth date|d\.?o\.?b\.?)\s*[:\-]\s*\d{4}(?:[-/]\d{1,2}){0,2}",
            " ",
            window,
            flags=re.I,
        )
        years = re.findall(r"(?:19|20)\d{2}", cleaned)
        if years:
            return years[-1]
    parsed = _text(row.get("end_year")) or _text(row.get("start_year"))
    match = re.search(r"(?:19|20)\d{2}", parsed)
    return match.group(0) if match else ""


def _degree_line(line: str) -> bool:
    return bool(re.match(
        r"^(?:Ph\.?D|MBA|M\.?Sc?|M\.?A|M\.?Tech|B\.?Sc?|B\.?A|B\.?Tech|B\.?E|BCA|MCA|"
        r"Bachelor|Master|Associate|Diploma)\b",
        line,
        re.I,
    ))


def _clean_degree(degree: str) -> tuple[str, str, str]:
    text = _text(degree)
    year = "In Progress" if re.search(r"pursuing|in progress", text, re.I) else ""
    text = re.sub(r"\s*\((?:pursuing|in progress)\)", "", text, flags=re.I).strip()
    field = ""
    inline = re.search(r"\(([A-Za-z][A-Za-z.]{1,12})\)\s*$", text)
    if inline:
        field = inline.group(1).strip()
        text = text[:inline.start()].strip()
    field_match = re.search(r"\bin\s+([A-Za-z][A-Za-z &/]{1,60})$", text, re.I)
    if field_match and "university" not in field_match.group(1).lower():
        field = field or field_match.group(1).strip()
        text = text[:field_match.start()].strip()
    return text, field, year


def _schools_from_raw(raw: str) -> list[dict[str, str]]:
    lines = _join_pipes(_section_lines(raw, {"education", "academic background", "academics", "qualifications"}))
    glued: list[str] = []
    for line in lines:
        if glued and line.lower() in {"university", "college", "institute", "school"} and " | " in glued[-1]:
            glued[-1] = f"{glued[-1]} {line}"
            continue
        glued.append(line)
    if not any(" | " in line or _degree_line(line) for line in glued):
        return []
    schools: list[dict[str, str]] = []
    index = 0
    while index < len(glued):
        line = glued[index]
        if " | " not in line and not _degree_line(line):
            index += 1
            continue
        degree = line
        school = ""
        if " | " in line:
            degree, school = [part.strip() for part in line.split("|", 1)]
        index += 1
        notes: list[str] = []
        while index < len(glued) and " | " not in glued[index] and not _degree_line(glued[index]):
            notes.append(glued[index])
            index += 1
        if not school:
            for note in notes:
                if any(token in note.lower() for token in ("university", "college", "institute", "school")):
                    school = note
                    break
        degree, field, year = _clean_degree(degree)
        if not year:
            for note in notes:
                if re.search(r"in progress|pursuing", note, re.I):
                    year = "In Progress"
                    break
                years = re.findall(r"(?:19|20)\d{2}", note)
                if years:
                    year = years[-1]
        if not degree and not school:
            continue
        schools.append({
            "school": _clip(school, 160),
            "degree": _clip(degree, 120),
            "field_of_study": _clip(field, 120),
            "graduation_year": year,
            "grade": _grade_near(raw, school, degree, ""),
        })
    schools.sort(key=lambda item: item.get("graduation_year") or "0000", reverse=True)
    return schools[:12]


def _schools(rows: list[dict], raw: str) -> list[dict[str, str]]:
    direct = _schools_from_raw(raw)
    if direct:
        return direct
    schools: list[dict[str, str]] = []
    for row in _merge_education(rows):
        school = _text(row.get("institution"))
        degree = _text(row.get("degree"))
        if not school and not degree:
            continue
        schools.append({
            "school": _clip(school, 160),
            "degree": _clip(degree, 120),
            "field_of_study": _clip(_text(row.get("field")), 120),
            "graduation_year": _graduation_year(raw, school, degree, row),
            "grade": _grade_near(raw, school, degree, _text(row.get("gpa"))),
        })
    schools.sort(key=lambda item: item.get("graduation_year") or "0000", reverse=True)
    return schools[:12]


def _window(raw: str, needle: str) -> str:
    if not needle or needle not in raw:
        return ""
    index = raw.find(needle)
    return raw[max(0, index - 180):index + 420]


def _trim_num(value: str) -> str:
    if "." not in value:
        return value
    return value.rstrip("0").rstrip(".") or value


def _grade_near(raw: str, school: str, degree: str, parsed_gpa: str) -> str:
    window = _window(raw, school) or _window(raw, degree)
    if window:
        percent = re.search(r"\b(\d{1,3}(?:\.\d+)?)\s*%", window)
        if percent and float(percent.group(1)) <= 100:
            return f"{_trim_num(percent.group(1))}%"
        over_five = re.search(r"\b(\d(?:\.\d+)?)\s*/\s*5(?:\.0+)?\b", window)
        if over_five:
            return f"{_trim_num(over_five.group(1))}/5"
        labeled = re.search(r"(?:grade|percentage|cgpa|gpa)\s*[:\-]\s*(\d{1,3}(?:\.\d+)?)(\s*/\s*[45](?:\.0+)?)?\s*%?", window, re.I)
        if labeled:
            number = float(labeled.group(1))
            if number <= 100:
                text = _trim_num(labeled.group(1))
                if labeled.group(2):
                    scale = "5" if "5" in labeled.group(2) else "4"
                    return f"{text}/{scale}"
                if "%" in labeled.group(0) or "percent" in labeled.group(0).lower() or number > 5:
                    return f"{text}%"
                return text
        over_four = re.search(r"\b(\d(?:\.\d+)?)\s*/\s*4(?:\.0+)?\b", window)
        if over_four:
            return f"{_trim_num(over_four.group(1))}/4"
    if parsed_gpa:
        return _trim_num(parsed_gpa)
    return ""


def _yes_no(value: str) -> str:
    text = _text(value).lower()
    if re.match(r"yes\b", text):
        return "Yes"
    if re.match(r"no\b", text):
        return "No"
    return ""


def _work_authorization(raw: str, nationality: str) -> dict[str, str]:
    result = {
        "authorized_to_work": "",
        "need_iqama_or_work_visa": "",
        "work_authorization_type": "",
        "work_authorization_validity": "",
    }
    saudi_national = nationality == "Saudi" or bool(
        re.search(r"\bsaudi\s+(?:national|citizen)\b", raw, re.I)
    )
    if saudi_national:
        result["authorized_to_work"] = "Yes"
        result["need_iqama_or_work_visa"] = "No"
        result["work_authorization_type"] = "Saudi national"
        result["work_authorization_validity"] = "Indefinite"
    elif re.search(
        r"\b(?:valid|transferable)\s+iqama\b|\biqama\s+holder\b|\biqama\s*[:\-]\s*transferable\b",
        raw,
        re.I,
    ):
        result["authorized_to_work"] = "Yes"
        result["need_iqama_or_work_visa"] = "No"
        result["work_authorization_type"] = "Iqama"
        result["work_authorization_validity"] = "Transferable"

    explicit_type = _label(raw, ("work authorization type", "authorization type"), 80)
    if explicit_type:
        result["work_authorization_type"] = explicit_type
    authorized = _label(raw, ("authorized to work", "authorised to work"), 20)
    if authorized:
        result["authorized_to_work"] = _yes_no(authorized) or result["authorized_to_work"]
    elif re.search(r"\bnot authorised to work\b|\bnot authorized to work\b", raw, re.I):
        result["authorized_to_work"] = "No"
    elif re.search(r"\bauthori[sz]ed to work\b", raw, re.I) and not result["authorized_to_work"]:
        result["authorized_to_work"] = "Yes"

    need = _label(raw, ("need iqama or work visa", "need iqama", "need work visa", "visa sponsorship"), 20)
    if need:
        result["need_iqama_or_work_visa"] = _yes_no(need) or result["need_iqama_or_work_visa"]
    elif re.search(r"\b(?:no|without)\s+(?:iqama|visa sponsorship|sponsorship)\s+(?:needed|required)\b", raw, re.I):
        result["need_iqama_or_work_visa"] = "No"
    elif re.search(r"\b(?:need(?:s|ed)?\s+(?:an?\s+)?)?(?:visa sponsorship|work visa)\s+(?:needed|required)\b", raw, re.I):
        result["need_iqama_or_work_visa"] = "Yes"

    validity = _label(raw, ("work authorization validity", "authorization validity", "iqama validity", "visa validity"), 40)
    if not validity:
        match = re.search(
            r"(?:iqama|work visa|visa)\s*(?:valid(?:ity)?|expir(?:y|es|ation)?|until)\s*[:\-]?\s*([^\n]{1,30})",
            raw,
            re.I,
        )
        if match:
            validity = match.group(1).strip(" .")
    if validity:
        if re.search(r"indefinite|permanent|no expir", validity, re.I):
            result["work_authorization_validity"] = "Indefinite"
        else:
            result["work_authorization_validity"] = _normalize_date(validity) or _clip(validity, 40)
    return result


def _salary(raw: str) -> str:
    patterns = (
        r"(?:desired|expected|target|monthly)\s+(?:monthly\s+)?salary(?:\s*\(\s*(?:SAR|SR|ريال)\s*\))?\s*[:\-]?\s*(?:SAR|SR|ريال)?\s*([0-9][0-9,]{2,8})",
        r"(?:salary|compensation)\s*[:\-]\s*(?:SAR|SR|ريال)\s*([0-9][0-9,]{2,8})",
        r"(?:SAR|SR)\s*([0-9][0-9,]{2,8})\s*(?:/|per)\s*month",
        r"([0-9][0-9,]{2,8})\s*(?:SAR|SR)\s*(?:/|per)\s*month",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.I)
        if not match:
            continue
        line_start = raw.rfind("\n", 0, match.start()) + 1
        line_end = raw.find("\n", match.end())
        line = raw[line_start:line_end if line_end != -1 else None]
        if re.search(r"\b(year|annual|annum|usd|\$|per hour|/hr)\b", line, re.I):
            continue
        digits = re.sub(r"\D", "", match.group(1))
        if digits:
            return digits
    return ""


def _notice(raw: str) -> str:
    labeled = _label(raw, ("notice period", "joining date", "available from"), 40)
    if labeled:
        return labeled
    match = re.search(r"\b(\d+\s*(?:day|days|week|weeks|month|months))\s+notice\b", raw, re.I)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    match = re.search(
        r"\b(?:available|can (?:join|start))\s+(immediately|immediate|in\s+\d+\s+(?:day|days|week|weeks|month|months))\b",
        raw,
        re.I,
    )
    if match:
        value = match.group(1).strip()
        if value.lower() == "immediate":
            return "Immediately"
        return value[:1].upper() + value[1:]
    return ""


def _logistics(raw: str) -> dict[str, str]:
    relocate = ""
    if re.search(r"\b(?:not willing to relocate|unwilling to relocate|cannot relocate|can't relocate)\b", raw, re.I):
        relocate = "No"
    elif re.search(r"\b(?:willing to relocate|open to relocation|ready to relocate)\b", raw, re.I):
        relocate = "Yes"
    else:
        labeled = _yes_no(_label(raw, ("willing to relocate", "relocate"), 10))
        if labeled:
            relocate = labeled

    remote = ""
    if re.search(r"\b(?:not open to remote|no remote|onsite only|on-site only)\b", raw, re.I):
        remote = "No"
    elif re.search(r"\b(?:open to remote(?: work)?|willing to work remotely|available for remote)\b", raw, re.I):
        remote = "Yes"
    else:
        labeled = _yes_no(_label(raw, ("open to remote work", "open to remote", "remote work"), 10))
        if labeled:
            remote = labeled

    heard = _label(raw, ("how did you hear about us", "how did you hear", "heard about us"), 80)
    return {
        "desired_monthly_salary_sar": _salary(raw),
        "notice_period": _notice(raw),
        "willing_to_relocate": relocate,
        "open_to_remote": remote,
        "meets_minimum_working_age": _minimum_age(raw),
        "how_heard": heard,
    }


def _parse_dob(token: str) -> date | None:
    text = _text(token)
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if match:
        return _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", text)
    if not match:
        return None
    first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if first > 12:
        return _safe_date(year, second, first)
    if second > 12:
        return _safe_date(year, first, second)
    return _safe_date(year, second, first)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _minimum_age(raw: str) -> str:
    labeled = _yes_no(_label(raw, ("meet labor law minimum working age", "minimum working age"), 10))
    if labeled:
        return labeled
    if re.search(r"\b(?:18\s*\+|over 18|above 18|at least 18)\b", raw, re.I):
        return "Yes"
    match = re.search(
        r"(?:date of birth|birth date|d\.?o\.?b\.?)\s*[:\-]\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4})",
        raw,
        re.I,
    )
    if not match:
        return ""
    born = _parse_dob(match.group(1))
    if born is None:
        return ""
    today = date.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    if age < 0 or age > 100:
        return ""
    return "Yes" if age >= 15 else "No"


def _optional_identifiers(raw: str) -> dict[str, str]:
    gender = _label(raw, ("gender", "الجنس"), 20).lower()
    if gender in {"m", "male", "ذكر"}:
        gender = "Male"
    elif gender in {"f", "female", "أنثى"}:
        gender = "Female"
    else:
        gender = ""
    disability = _label(raw, ("disability status", "disability", "الإعاقة"), 80)
    lowered = disability.lower()
    if lowered in {"no", "none", "n/a", "na", "not applicable", "no disability", "without disability"}:
        disability = "No"
    elif lowered in {"yes", "y"}:
        disability = "Yes"
    return {
        "gender": gender,
        "disability_status": _clip(disability, 80),
    }
