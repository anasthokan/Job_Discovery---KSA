"""Match-score weights saved by the admin recommendation-score screen.

The portal CRUD stores the percentages (skills, certifications, title,
years_level) and the integer score cutoff. This reader finds that table on
the MySQL server and caches it for a minute. Until the table is present, the
built-in split is 55 / 15 / 20 / 10 and the cutoff is 25.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

DEFAULT_WEIGHTS = {
    "skills": 55,
    "certifications": 15,
    "title": 20,
    "years_level": 10,
}
DEFAULT_MIN_SCORE = 25
_CACHE_SECONDS = 60
_cache: dict = {"at": 0.0, "config": None}
_logged: dict | None = None

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
    cached = _cache["config"]
    if cached is not None and now - _cache["at"] < _CACHE_SECONDS:
        return cached
    loaded = _load()
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
    return loaded


def clear_score_config_cache() -> None:
    _cache["at"] = 0.0
    _cache["config"] = None


def _load() -> dict:
    config = {
        **DEFAULT_WEIGHTS,
        "min_score": DEFAULT_MIN_SCORE,
        "source": "default",
    }
    try:
        from jobscraper.db import get_conn, mysql_configured

        if not mysql_configured():
            return config
        conn = get_conn()
        if conn is None:
            return config
        with conn.cursor() as cur:
            for table in _factor_tables(cur):
                weights = _read_weights(cur, table)
                if weights is None:
                    continue
                config.update(weights)
                config["source"] = f"{table[0]}.{table[1]}"
                cutoff = _read_min_score(cur, table[0], table[1])
                if cutoff is not None:
                    config["min_score"] = cutoff
                break
    except Exception:
        log.exception("recommendation score config fell back to defaults")
        return {
            **DEFAULT_WEIGHTS,
            "min_score": DEFAULT_MIN_SCORE,
            "source": "default",
        }
    return config


def _factor_tables(cur) -> list[tuple[str, str]]:
    cur.execute(
        """
        SELECT table_schema AS schema_name, table_name,
               SUM(LOWER(column_name) = 'weight') AS has_weight,
               SUM(LOWER(column_name) IN ('key', 'factor_key', 'code', 'slug')) AS has_key
        FROM information_schema.columns
        WHERE table_schema NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
        GROUP BY table_schema, table_name
        HAVING has_weight > 0 AND has_key > 0
        """
    )
    rows = cur.fetchall() or []
    ranked = sorted(rows, key=lambda row: _table_rank(str(_cell(row, "table_name") or "")))
    found: list[tuple[str, str]] = []
    for row in ranked:
        schema = str(_cell(row, "schema_name") or "")
        table = str(_cell(row, "table_name") or "")
        if _safe(schema) and _safe(table):
            found.append((schema, table))
    return found


def _table_rank(name: str) -> tuple[int, str]:
    text = name.lower()
    if "recommend" in text and "score" in text:
        return (0, text)
    if "recommend" in text:
        return (1, text)
    if "score" in text and "factor" in text:
        return (2, text)
    return (3, text)


def _read_weights(cur, table: tuple[str, str]) -> dict | None:
    from jobscraper.db import _quote_ident

    schema, name = table
    cur.execute(f"SELECT * FROM {_quote_ident(schema)}.{_quote_ident(name)}")
    found = {key: 0 for key in DEFAULT_WEIGHTS}
    seen = False
    raw_weights: list[float] = []
    parsed: list[tuple[str, float, bool]] = []
    for row in cur.fetchall() or []:
        key = _canonical_key(_cell(row, "key", "factor_key", "code", "slug", "label", "name"))
        if key not in found:
            continue
        weight = _number(_cell(row, "weight", "amount", "percent", "percentage"))
        if weight is None:
            continue
        parsed.append((key, weight, _active(row)))
        if _active(row):
            raw_weights.append(weight)
        seen = True
    if not seen:
        return None
    scale = 100.0 if raw_weights and max(raw_weights) <= 1 and sum(raw_weights) <= 1.5 else 1.0
    for key, weight, active in parsed:
        found[key] = int(round(weight * scale)) if active else 0
    if sum(found.values()) <= 0:
        return None
    return found


def _read_min_score(cur, schema: str, factor_table: str) -> int | None:
    from jobscraper.db import _quote_ident

    cur.execute(
        """
        SELECT table_name
        FROM information_schema.columns
        WHERE table_schema = %s AND LOWER(column_name) = 'score'
        """,
        (schema,),
    )
    names = []
    for row in cur.fetchall() or []:
        name = str(_cell(row, "table_name") or "")
        if not _safe(name) or name == factor_table:
            continue
        lowered = name.lower()
        if "whatsapp" in lowered or lowered in {"jobs", "job_discovery_candidates"}:
            continue
        if any(part in lowered for part in ("score", "recommend")):
            names.append(name)
    names.sort(key=lambda item: (0 if "value" in item.lower() else 1, item.lower()))
    scores: list[int] = []
    for name in names:
        cur.execute(f"SELECT * FROM {_quote_ident(schema)}.{_quote_ident(name)}")
        for row in cur.fetchall() or []:
            if not _active(row):
                continue
            number = _number(_cell(row, "score", "value", "min_score", "amount"))
            if number is None:
                continue
            scores.append(max(0, min(100, int(round(number)))))
        if scores:
            return min(scores)
    return None


def _canonical_key(value) -> str | None:
    text = " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").replace("/", " ").split())
    if not text:
        return None
    compact = text.replace(" ", "")
    spaced = text
    return _KEY_ALIAS.get(compact) or _KEY_ALIAS.get(spaced) or _KEY_ALIAS.get(text.replace(" ", "_"))


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
    value = _cell(row, "is_active", "isactive", "active", "status")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"0", "false", "no", "inactive", "off", "disabled"}:
        return False
    return True


def _safe(name: str) -> bool:
    return bool(name) and name.replace("_", "").isalnum()
