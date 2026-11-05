"""Recommendation weights and score cutoff from the deployed KsaJob API.

GET /api/v1/admin/portal/recommendation-score/?active=true
GET /api/v1/scores/

The first call needs KSAJOBS_ACCESS_TOKEN, or KSAJOBS_API_EMAIL and
KSAJOBS_API_PASSWORD for an admin account with the jobs console permission.
Until that answers, ranking stays at skills 55, certifications 15, title 20,
years/level 10, and a cutoff of 25. Results are cached for a minute.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger("jobscraper.score")

DEFAULT_API_BASE = "https://ksajobs-apis.atozeesolutions.com"
DEFAULT_WEIGHTS = {
    "skills": 55,
    "certifications": 15,
    "title": 20,
    "years_level": 10,
}
DEFAULT_MIN_SCORE = 25
_CACHE_SECONDS = 60
_TIMEOUT = 8

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "config": None}
_logged: dict | None = None
_env_token_rejected = False
_login_token = ""

_KEY_ALIAS = {
    "skill": "skills",
    "skills": "skills",
    "cert": "certifications",
    "certs": "certifications",
    "certification": "certifications",
    "certifications": "certifications",
    "title": "title",
    "titles": "title",
    "job title": "title",
    "job_title": "title",
    "years": "years_level",
    "years level": "years_level",
    "years/level": "years_level",
    "years / level": "years_level",
    "years-level": "years_level",
    "years_level": "years_level",
    "yearslevel": "years_level",
    "level": "years_level",
    "experience": "years_level",
}


def score_config() -> dict:
    """Active weights plus min_score. Safe to call on every ranked job."""
    global _logged
    now = time.monotonic()
    with _lock:
        cached = _cache["config"]
        if cached is not None and now - _cache["at"] < _CACHE_SECONDS:
            return cached
    loaded = _load()
    with _lock:
        previous = _cache["config"]
        if loaded.get("source") != "api" and previous and previous.get("source") == "api":
            _cache["at"] = now
            loaded = previous
        else:
            _cache["at"] = now
            _cache["config"] = loaded
        if loaded != _logged:
            log.info(
                "Recommendation weights skills=%s certifications=%s title=%s years_level=%s min_score=%s source=%s",
                loaded["skills"],
                loaded["certifications"],
                loaded["title"],
                loaded["years_level"],
                loaded["min_score"],
                loaded["source"],
            )
            _logged = dict(loaded)
        return dict(loaded)


def clear_score_config_cache() -> None:
    global _logged, _env_token_rejected, _login_token
    with _lock:
        _cache["at"] = 0.0
        _cache["config"] = None
        _logged = None
        _env_token_rejected = False
        _login_token = ""


def weights_from_payload(payload) -> dict | None:
    """Map a recommendation-score response onto the four ranking factors."""
    found = {key: 0 for key in DEFAULT_WEIGHTS}
    raw: list[tuple[str, float]] = []
    for row in _records(payload):
        if not isinstance(row, dict) or not _active(row):
            continue
        key = _canonical_key(_cell(row, "key", "factorKey", "factor_key", "code", "slug", "label", "name"))
        weight = _number(_cell(row, "weight", "amount", "percent", "percentage"))
        if key not in found or weight is None:
            continue
        raw.append((key, weight))
    if not raw:
        return None
    active_values = [weight for _, weight in raw]
    scale = 100.0 if max(active_values) <= 1 and sum(active_values) <= 1.5 else 1.0
    for key, weight in raw:
        found[key] = int(round(weight * scale))
    if sum(found.values()) <= 0:
        return None
    return found


def min_score_from_payload(payload) -> int | None:
    """Lowest stored score. That is the match cutoff on the admin score list."""
    scores: list[int] = []
    for row in _records(payload):
        if isinstance(row, dict):
            if not _active(row):
                continue
            number = _number(_cell(row, "score", "value", "minScore", "min_score", "amount"))
        else:
            number = _number(row)
        if number is None:
            continue
        scores.append(max(0, min(100, int(round(number)))))
    if not scores:
        return None
    return min(scores)


def _load() -> dict:
    config = {
        **DEFAULT_WEIGHTS,
        "min_score": DEFAULT_MIN_SCORE,
        "source": "default",
    }
    if not _access_token():
        log.warning(
            "KSAJOBS_ACCESS_TOKEN is empty, so recommendation weights stay at the built-in defaults."
        )
        return config
    linked = False
    factors = _get_json("/api/v1/admin/portal/recommendation-score/?active=true")
    if factors is not None:
        weights = weights_from_payload(factors)
        if weights:
            config.update(weights)
            linked = True
    scores = _get_json("/api/v1/scores/")
    if scores is not None:
        cutoff = min_score_from_payload(scores)
        if cutoff is not None:
            config["min_score"] = cutoff
            linked = True
    if linked:
        config["source"] = "api"
    return config


def _get_json(path: str, *, retry: bool = True):
    token = _access_token()
    if not token:
        return None
    url = _base() + path
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "JobDiscovery-KSA",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and retry and _refresh_token():
            return _get_json(path, retry=False)
        log.warning("KsaJob API %s returned %s", path, exc.code)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        log.warning("KsaJob API %s could not be read", path, exc_info=True)
    return None


def _refresh_token() -> bool:
    global _env_token_rejected, _login_token
    with _lock:
        _env_token_rejected = True
        _login_token = ""
    return bool(_login())


def _access_token() -> str:
    with _lock:
        rejected = _env_token_rejected
        cached = _login_token
    if not rejected:
        env_token = _env("KSAJOBS_ACCESS_TOKEN")
        if env_token:
            return env_token
    if cached:
        return cached
    return _login()


def _login() -> str:
    global _login_token
    email = _env("KSAJOBS_API_EMAIL")
    password = _env("KSAJOBS_API_PASSWORD")
    if not email or not password:
        return ""
    body = json.dumps({"email": email, "password": password}).encode("utf-8")
    request = urllib.request.Request(
        _base() + "/api/v1/auth/login/",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "JobDiscovery-KSA",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        log.warning("KsaJob API login failed", exc_info=True)
        return ""
    token = _find_token(payload)
    if token:
        with _lock:
            _login_token = token
    return token


def _find_token(payload) -> str:
    if isinstance(payload, dict):
        for key in ("accessToken", "access_token", "token", "jwt"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = _find_token(value)
            if found:
                return found
    return ""


def _records(payload) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "items", "factors", "scores"):
            if isinstance(data.get(key), list):
                return data[key]
        if any(key in data for key in ("key", "weight", "score", "publicId")):
            return [data]
    return []


def _canonical_key(value) -> str | None:
    text = " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").replace("/", " ").split())
    if not text:
        return None
    compact = text.replace(" ", "")
    return _KEY_ALIAS.get(compact) or _KEY_ALIAS.get(text) or _KEY_ALIAS.get(text.replace(" ", "_"))


def _cell(row: dict, *names: str):
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _active(row: dict) -> bool:
    value = _cell(row, "isActive", "is_active", "active", "status")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"0", "false", "no", "inactive", "off", "disabled"}:
        return False
    return True


def _base() -> str:
    return (_env("KSAJOBS_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()
