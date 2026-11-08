"""MySQL persistence for scraped job listings.

Credentials come from environment / `.env`:
  MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

log = logging.getLogger("jobscraper.db")

_LOCK = threading.Lock()
# One MySQL connection per thread. A single shared connection is closed by one
# request while another request is still reading, which crashes uvicorn and
# IIS then returns 502.
_local = threading.local()
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
# Scraped listings go into the ksajob database, table `jobs`.
JOBS_TABLE = "jobs"
RUNS_TABLE = "job_discovery_scrape_runs"
CANDIDATES_TABLE = "job_discovery_candidates"
COMPANIES_TABLE = "companies"

CREATE_JOBS_SQL = f"""
CREATE TABLE IF NOT EXISTS {JOBS_TABLE} (
  id BIGINT NOT NULL AUTO_INCREMENT,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  public_id CHAR(32) NOT NULL,
  title VARCHAR(512) NOT NULL,
  location VARCHAR(512) NOT NULL,
  description LONGTEXT NOT NULL,
  posted_at DATETIME(6) NULL,
  platform_posted_at DATETIME(6) NULL,
  is_active TINYINT(1) NOT NULL,
  company_ref_id BIGINT NULL,
  platform VARCHAR(120) NOT NULL,
  days_ago INT UNSIGNED NULL,
  company VARCHAR(255) NULL,
  first_seen_at DATETIME(6) NULL,
  last_seen_at DATETIME(6) NULL,
  skills JSON NOT NULL,
  source_id VARCHAR(255) NULL,
  state VARCHAR(255) NOT NULL,
  url VARCHAR(1000) NOT NULL,
  company_career_url VARCHAR(1000) NOT NULL DEFAULT '',
  raw_description LONGTEXT NOT NULL,
  job_type VARCHAR(32) NOT NULL DEFAULT 'fulltime',
  work_model VARCHAR(16) NOT NULL DEFAULT 'unknown',
  experience_level VARCHAR(32) NOT NULL DEFAULT 'unknown',
  years_experience SMALLINT NULL,
  visa_sponsorship VARCHAR(16) NOT NULL DEFAULT 'unknown',
  clearance_required VARCHAR(16) NOT NULL DEFAULT 'unknown',
  saudi_national_required VARCHAR(16) NOT NULL DEFAULT 'no',
  compensation VARCHAR(255) NOT NULL DEFAULT 'salary not disclosed',
  compensation_checked TINYINT(1) NOT NULL DEFAULT 0,
  listing_inferred TINYINT(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uk_jobs_public_id (public_id),
  UNIQUE KEY uk_jobs_source_id (source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

CREATE_COMPANIES_SQL = f"""
CREATE TABLE IF NOT EXISTS {COMPANIES_TABLE} (
  id BIGINT NOT NULL AUTO_INCREMENT,
  public_id CHAR(32) NOT NULL,
  name VARCHAR(255) NOT NULL,
  normalized_name VARCHAR(255) NOT NULL,
  website VARCHAR(512) NOT NULL DEFAULT '',
  sponsorship_notes VARCHAR(512) NOT NULL DEFAULT '',
  logo_url VARCHAR(512) NOT NULL DEFAULT '',
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_companies_public_id (public_id),
  UNIQUE KEY uk_companies_normalized (normalized_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

CREATE_CANDIDATES_SQL = f"""
CREATE TABLE IF NOT EXISTS {CANDIDATES_TABLE} (
  candidate_id VARCHAR(64) NOT NULL,
  profile JSON NOT NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (candidate_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

CREATE_RUNS_SQL = f"""
CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
  id BIGINT NOT NULL AUTO_INCREMENT,
  started_at DATETIME NOT NULL,
  finished_at DATETIME NULL,
  job_count INT NOT NULL DEFAULT 0,
  new_count INT NOT NULL DEFAULT 0,
  duplicate_count INT NOT NULL DEFAULT 0,
  keywords VARCHAR(512) NULL,
  state VARCHAR(128) NULL,
  platform VARCHAR(128) NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'running',
  error TEXT NULL,
  PRIMARY KEY (id),
  KEY idx_jd_scrape_runs_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


class UpsertStats(NamedTuple):
    """How many listings a save wrote, split into first-time vs already-known."""

    saved: int = 0
    new: int = 0
    duplicates: int = 0

    def __bool__(self) -> bool:
        return self.saved > 0

    def __int__(self) -> int:
        return self.saved

# Columns this scraper writes. Other NOT NULL columns on the shared jobs table
# (for example sponsors_work_visa) still need a default or MySQL error 1364 aborts the save.
_UPSERT_COLUMNS = {
    "public_id",
    "source_id",
    "title",
    "company",
    "company_ref_id",
    "skills",
    "state",
    "location",
    "platform",
    "days_ago",
    "url",
    "company_career_url",
    "description",
    "raw_description",
    "posted_at",
    "platform_posted_at",
    "is_active",
    "job_type",
    "work_model",
    "experience_level",
    "years_experience",
    "visa_sponsorship",
    "clearance_required",
    "saudi_national_required",
    "compensation",
    "compensation_checked",
    "listing_inferred",
    "first_seen_at",
    "last_seen_at",
    "created_at",
    "updated_at",
}


def _row_value(row: dict, name: str):
    for key, value in row.items():
        if str(key).lower() == name:
            return value
    return None


def _sql_default(data_type: str, column_type: str) -> str:
    kind = (data_type or "").lower()
    if kind in {"tinyint", "smallint", "mediumint", "int", "bigint", "decimal", "float", "double", "bit", "year", "bool", "boolean"}:
        return "0"
    if kind == "date":
        return "'1970-01-01'"
    if kind in {"datetime", "timestamp"}:
        return "CURRENT_TIMESTAMP"
    if kind == "json":
        return "('{}')"
    if kind == "enum":
        match = re.search(r"enum\(\s*'((?:[^'\\]|\\.)*)'", column_type or "", re.I)
        if match:
            return "'" + match.group(1).replace("'", "''") + "'"
    return "''"


# This MySQL server rejects defaults on these types (error 1101). The INSERT sends a literal instead.
_NO_COLUMN_DEFAULT = {
    "tinytext",
    "text",
    "mediumtext",
    "longtext",
    "tinyblob",
    "blob",
    "mediumblob",
    "longblob",
    "json",
    "geometry",
    "point",
    "linestring",
    "polygon",
    "multipoint",
    "multilinestring",
    "multipolygon",
    "geometrycollection",
}


def _default_omitted_columns(cur) -> None:
    """Shared jobs columns we do not insert must have a default, or strict mode rejects the row."""
    cur.execute(
        """
        SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, EXTRA
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND IS_NULLABLE = 'NO'
          AND COLUMN_DEFAULT IS NULL
        """,
        (JOBS_TABLE,),
    )
    for column in cur.fetchall() or []:
        name = str(_row_value(column, "column_name") or "")
        extra = str(_row_value(column, "extra") or "").lower()
        if not name or name in _UPSERT_COLUMNS:
            continue
        if "auto_increment" in extra or "generated" in extra:
            continue
        kind = str(_row_value(column, "data_type") or "").lower()
        if kind in _NO_COLUMN_DEFAULT:
            continue
        default = _sql_default(kind, str(_row_value(column, "column_type") or ""))
        try:
            cur.execute(f"ALTER TABLE {JOBS_TABLE} ALTER COLUMN {_quote_ident(name)} SET DEFAULT {default}")
        except Exception:
            log.exception("Could not set default on %s.%s", JOBS_TABLE, name)
            continue
        log.info("Set default %s on %s.%s", default, JOBS_TABLE, name)


def _omitted_required_columns(cur) -> list[tuple[str, str]]:
    """NOT NULL jobs columns this INSERT does not send. Strict mode rejects the row without them."""
    cur.execute(
        """
        SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, EXTRA
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND IS_NULLABLE = 'NO'
          AND COLUMN_DEFAULT IS NULL
        """,
        (JOBS_TABLE,),
    )
    extras = []
    for column in cur.fetchall() or []:
        name = str(_row_value(column, "column_name") or "")
        extra = str(_row_value(column, "extra") or "").lower()
        if not name or name in _UPSERT_COLUMNS:
            continue
        if "auto_increment" in extra or "generated" in extra:
            continue
        literal = _sql_default(str(_row_value(column, "data_type") or ""), str(_row_value(column, "column_type") or ""))
        extras.append((name, literal))
    return extras


def _upsert_sql(cur) -> str:
    extras = _omitted_required_columns(cur)
    if not extras:
        return UPSERT_SQL
    columns = ", " + ", ".join(_quote_ident(name) for name, _literal in extras)
    values = ", " + ", ".join(literal for _name, literal in extras)
    sql = UPSERT_SQL.replace(") VALUES (", columns + ") VALUES (", 1)
    return sql.replace(") AS new", values + ") AS new", 1)


_MISSING_DEFAULT = re.compile(r"Field '([^']+)' doesn't have a default value")


def _column_literal(cur, table: str, name: str) -> str:
    cur.execute(f"SHOW COLUMNS FROM {table} LIKE %s", (name,))
    row = cur.fetchone() or {}
    column_type = str(_row_value(row, "type") or "")
    base = column_type.split("(", 1)[0].strip().lower()
    return _sql_default(base, column_type)


def _inject_column(sql: str, name: str, literal: str) -> str:
    column = _quote_ident(name)
    updated, count = re.subn(r"\)\s*VALUES\s*\(", lambda _match: f", {column}) VALUES (", sql, count=1)
    if count != 1:
        return sql
    updated, count = re.subn(r"\)(\s*AS\s+new\b)", lambda match: f", {literal})" + match.group(1), updated, count=1)
    if count == 1:
        return updated
    updated, count = re.subn(
        r"\)(\s*ON\s+DUPLICATE\b)",
        lambda match: f", {literal})" + match.group(1),
        updated,
        count=1,
    )
    return updated if count == 1 else sql


def _write_row(cur, sql: str, params, *, table: str, many: bool = False) -> None:
    """Insert, and if MySQL rejects a required column, send that column and try again."""
    seen: set[str] = set()
    for _attempt in range(8):
        try:
            if many:
                cur.executemany(sql, params)
            else:
                cur.execute(sql, params)
            return
        except Exception as exc:
            code = exc.args[0] if getattr(exc, "args", None) else None
            match = _MISSING_DEFAULT.search(str(exc))
            if code != 1364 or not match:
                raise
            field = match.group(1)
            if field in seen:
                raise
            seen.add(field)
            literal = _column_literal(cur, table, field)
            try:
                cur.execute(
                    f"ALTER TABLE {table} ALTER COLUMN {_quote_ident(field)} SET DEFAULT {literal}"
                )
                log.info("Set default %s on %s.%s after save error", literal, table, field)
            except Exception:
                log.exception("Could not set default on %s.%s", table, field)
            sql = _inject_column(sql, field, literal)
            log.warning("Retrying %s insert with %s = %s", table, field, literal)


# jobs.id is a bigint autoincrement. The scraper id is stored in source_id.
UPSERT_SQL = f"""
INSERT INTO {JOBS_TABLE} (
  public_id, source_id, title, company, company_ref_id, skills, state, location,
  platform, days_ago, url, description, raw_description, posted_at, platform_posted_at, is_active,
  job_type, work_model, experience_level, years_experience, visa_sponsorship, clearance_required,
  saudi_national_required, compensation, company_career_url, compensation_checked, listing_inferred, first_seen_at, last_seen_at, created_at, updated_at
) VALUES (
  %s, %s, %s, %s, %s, %s, %s, %s,
  %s, %s, %s, %s, %s, %s, %s, 1,
  %s, %s, %s, %s, %s, %s,
  %s, %s, %s, 1, 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
) AS new
ON DUPLICATE KEY UPDATE
  title = new.title,
  company = new.company,
  company_ref_id = COALESCE(new.company_ref_id, {JOBS_TABLE}.company_ref_id),
  skills = new.skills,
  state = new.state,
  location = new.location,
  platform = new.platform,
  days_ago = new.days_ago,
  url = IF(
    new.url LIKE '%%example.com%%' OR TRIM(new.url) = '',
    {JOBS_TABLE}.url,
    new.url
  ),
  description = IF(
    CHAR_LENGTH(new.description) > CHAR_LENGTH({JOBS_TABLE}.description),
    new.description,
    {JOBS_TABLE}.description
  ),
  raw_description = IF(
    JSON_VALID(new.raw_description)
      AND JSON_VALID({JOBS_TABLE}.raw_description)
      AND CHAR_LENGTH({JOBS_TABLE}.raw_description) > 2
      AND CHAR_LENGTH(new.raw_description) > 2,
    JSON_MERGE_PATCH({JOBS_TABLE}.raw_description, new.raw_description),
    IF(
      CHAR_LENGTH(new.raw_description) > 2,
      new.raw_description,
      IF(CHAR_LENGTH({JOBS_TABLE}.raw_description) > 2, {JOBS_TABLE}.raw_description, '{{}}')
    )
  ),
  posted_at = COALESCE(new.posted_at, {JOBS_TABLE}.posted_at),
  platform_posted_at = IF(
    new.platform_posted_at IS NULL,
    {JOBS_TABLE}.platform_posted_at,
    IF(
      {JOBS_TABLE}.platform_posted_at IS NULL
        OR TIME({JOBS_TABLE}.platform_posted_at) = '00:00:00'
        OR TIME(new.platform_posted_at) <> '00:00:00',
      new.platform_posted_at,
      {JOBS_TABLE}.platform_posted_at
    )
  ),
  job_type = new.job_type,
  work_model = new.work_model,
  experience_level = new.experience_level,
  years_experience = new.years_experience,
  visa_sponsorship = IF(
    new.visa_sponsorship = 'no',
    'no',
    IF(
      new.visa_sponsorship = 'yes' OR {JOBS_TABLE}.visa_sponsorship = 'yes',
      'yes',
      new.visa_sponsorship
    )
  ),
  clearance_required = new.clearance_required,
  saudi_national_required = new.saudi_national_required,
  compensation = IF(
    new.compensation <> 'salary not disclosed',
    new.compensation,
    IF(
      {JOBS_TABLE}.compensation IS NULL
        OR {JOBS_TABLE}.compensation = ''
        OR {JOBS_TABLE}.compensation = 'salary not disclosed',
      'salary not disclosed',
      {JOBS_TABLE}.compensation
    )
  ),
  company_career_url = IF(
    TRIM(new.company_career_url) = '',
    {JOBS_TABLE}.company_career_url,
    new.company_career_url
  ),
  compensation_checked = 1,
  listing_inferred = 1,
  is_active = 1,
  last_seen_at = UTC_TIMESTAMP(6),
  updated_at = UTC_TIMESTAMP(6)
"""


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def mysql_config() -> dict | None:
    host = _env("MYSQL_HOST")
    user = _env("MYSQL_USER")
    database = _env("MYSQL_DATABASE")
    if not host or not user or not database:
        return None
    if not _DB_NAME_RE.fullmatch(database):
        log.error("MYSQL_DATABASE must be letters, numbers, underscore only")
        return None
    try:
        port = int(_env("MYSQL_PORT", "3306") or "3306")
    except ValueError:
        port = 3306
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": os.getenv("MYSQL_PASSWORD") or "",
        "database": database,
        "charset": "utf8mb4",
        "autocommit": True,
        "connect_timeout": int(_env("MYSQL_CONNECT_TIMEOUT", "5") or "5"),
        "read_timeout": int(_env("MYSQL_READ_TIMEOUT", "20") or "20"),
        "write_timeout": int(_env("MYSQL_WRITE_TIMEOUT", "60") or "60"),
    }


def mysql_configured() -> bool:
    return mysql_config() is not None


def _connect(cfg: dict, *, with_database: bool = True):
    import pymysql

    kwargs = {
        "host": cfg["host"],
        "port": cfg["port"],
        "user": cfg["user"],
        "password": cfg["password"],
        "charset": cfg["charset"],
        "autocommit": cfg["autocommit"],
        "connect_timeout": cfg["connect_timeout"],
        "read_timeout": cfg["read_timeout"],
        "write_timeout": cfg["write_timeout"],
        "cursorclass": pymysql.cursors.DictCursor,
    }
    if with_database:
        kwargs["database"] = cfg["database"]
    return pymysql.connect(**kwargs)


def _ensure_database(cfg: dict) -> None:
    conn = _connect(cfg, with_database=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{cfg['database']}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        conn.close()


def _tune_session(conn) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION wait_timeout=300")
            cur.execute("SET SESSION interactive_timeout=300")
            cur.execute("SET SESSION max_execution_time=20000")
    except Exception:
        pass


def get_conn():
    cfg = mysql_config()
    if not cfg:
        return None
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(conn, "open", False):
        try:
            conn.ping(reconnect=False)
            return conn
        except Exception:
            close_conn()
    conn = _connect(cfg)
    _tune_session(conn)
    _local.conn = conn
    return conn


def close_conn() -> None:
    conn = getattr(_local, "conn", None)
    _local.conn = None
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def _ensure_job_columns(cur) -> None:
    cur.execute(f"SHOW COLUMNS FROM {JOBS_TABLE} LIKE 'job_type'")
    if not cur.fetchone():
        cur.execute(
            f"ALTER TABLE {JOBS_TABLE} "
            "ADD COLUMN job_type VARCHAR(32) NOT NULL DEFAULT 'fulltime'"
        )
    for name, ddl in (
        ("work_model", "VARCHAR(16) NOT NULL DEFAULT 'unknown'"),
        ("experience_level", "VARCHAR(32) NOT NULL DEFAULT 'unknown'"),
        ("years_experience", "SMALLINT NULL"),
        ("visa_sponsorship", "VARCHAR(16) NOT NULL DEFAULT 'unknown'"),
        ("clearance_required", "VARCHAR(16) NOT NULL DEFAULT 'unknown'"),
        ("compensation", "VARCHAR(255) NOT NULL DEFAULT 'salary not disclosed'"),
        ("compensation_checked", "TINYINT(1) NOT NULL DEFAULT 0"),
        ("listing_inferred", "TINYINT(1) NOT NULL DEFAULT 0"),
        ("platform_posted_at", "DATETIME(6) NULL"),
    ):
        cur.execute(f"SHOW COLUMNS FROM {JOBS_TABLE} LIKE '{name}'")
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE {JOBS_TABLE} ADD COLUMN {name} {ddl}")
            if name == "platform_posted_at":
                # Only copy precise stamps. Date-only midnight values make daysAgo
                # look like "14 hours ago" instead of LinkedIn's "6 hours ago".
                cur.execute(
                    f"UPDATE {JOBS_TABLE} SET platform_posted_at = posted_at "
                    f"WHERE platform_posted_at IS NULL AND posted_at IS NOT NULL "
                    f"AND TIME(posted_at) <> '00:00:00'"
                )
    cur.execute(f"SHOW COLUMNS FROM {JOBS_TABLE} LIKE 'platform_posted_at'")
    if cur.fetchone():
        cur.execute(
            f"UPDATE {JOBS_TABLE} SET platform_posted_at = NULL "
            f"WHERE platform = 'LinkedIn' AND platform_posted_at IS NOT NULL "
            f"AND TIME(platform_posted_at) = '00:00:00'"
        )
    cur.execute(f"SHOW COLUMNS FROM {JOBS_TABLE} LIKE 'description'")
    description_column = cur.fetchone()
    description_type = ""
    if description_column:
        for key, value in description_column.items():
            if str(key).lower() == "type":
                description_type = str(value or "").lower()
                break
    if not description_column:
        cur.execute(
            f"ALTER TABLE {JOBS_TABLE} "
            "ADD COLUMN description LONGTEXT NOT NULL DEFAULT ('')"
        )
    elif not description_type.startswith("longtext"):
        cur.execute(
            f"ALTER TABLE {JOBS_TABLE} MODIFY COLUMN description LONGTEXT NOT NULL"
        )
    cur.execute(f"SHOW COLUMNS FROM {JOBS_TABLE} LIKE 'raw_description'")
    raw_column = cur.fetchone()
    if not raw_column:
        cur.execute(
            f"ALTER TABLE {JOBS_TABLE} "
            "ADD COLUMN raw_description LONGTEXT NOT NULL DEFAULT ('')"
        )
    elif str(raw_column.get("Null") or "").upper() == "YES":
        cur.execute(
            f"UPDATE {JOBS_TABLE} SET raw_description = '' WHERE raw_description IS NULL"
        )
        cur.execute(
            f"ALTER TABLE {JOBS_TABLE} MODIFY COLUMN raw_description LONGTEXT NOT NULL"
        )
    cur.execute(f"SHOW COLUMNS FROM {JOBS_TABLE} LIKE 'saudi_national_required'")
    if not cur.fetchone():
        cur.execute(
            f"ALTER TABLE {JOBS_TABLE} "
            "ADD COLUMN saudi_national_required VARCHAR(16) NOT NULL DEFAULT 'no'"
        )
    cur.execute(f"SHOW COLUMNS FROM {JOBS_TABLE} LIKE 'company_career_url'")
    if not cur.fetchone():
        cur.execute(
            f"ALTER TABLE {JOBS_TABLE} "
            "ADD COLUMN company_career_url VARCHAR(1000) NOT NULL DEFAULT ''"
        )
        _backfill_company_career_urls(cur)
    _default_omitted_columns(cur)


def _ensure_run_columns(cur) -> None:
    for name, ddl in (
        ("new_count", "INT NOT NULL DEFAULT 0"),
        ("duplicate_count", "INT NOT NULL DEFAULT 0"),
    ):
        cur.execute(f"SHOW COLUMNS FROM {RUNS_TABLE} LIKE '{name}'")
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE {RUNS_TABLE} ADD COLUMN {name} {ddl}")


def init_db() -> bool:
    cfg = mysql_config()
    if not cfg:
        log.info("MySQL not configured; jobs stay in data/jobs.json")
        return False
    try:
        _ensure_database(cfg)
        conn = get_conn()
        if conn is None:
            return False
        with conn.cursor() as cur:
            cur.execute(CREATE_JOBS_SQL)
            cur.execute(CREATE_RUNS_SQL)
            cur.execute(CREATE_COMPANIES_SQL)
            cur.execute(CREATE_CANDIDATES_SQL)
            from jobscraper.sources import CREATE_SOURCES_SQL
            from jobscraper.whatsapp_alerts import CREATE_ALERTS_SQL

            cur.execute(CREATE_SOURCES_SQL)
            cur.execute(CREATE_ALERTS_SQL)
            _ensure_job_columns(cur)
            _ensure_run_columns(cur)
        log.info("MySQL ready (%s/%s)", cfg["host"], cfg["database"])
        return True
    except Exception:
        close_conn()
        log.exception("MySQL init failed")
        return False


def backfill_listing_fields() -> int:
    """Fill work model, experience, visa, and Saudi-national flags on older rows."""
    if not mysql_configured():
        return 0
    from jobscraper.job_fields import infer_listing_fields

    conn = get_conn()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, title, company, location, state, description, job_type "
                f"FROM {JOBS_TABLE} WHERE listing_inferred = 0"
            )
            rows = cur.fetchall() or []
            if not rows:
                return 0
            payload = []
            for row in rows:
                fields = infer_listing_fields(
                    title=row.get("title") or "",
                    location=row.get("location") or "",
                    state=row.get("state") or "",
                    description=row.get("description") or "",
                    job_type=row.get("job_type") or "",
                )
                payload.append(
                    (
                        fields["work_model"],
                        str(fields["experience_level"] or "unknown")[:32],
                        fields["years_experience"],
                        fields["visa_sponsorship"],
                        fields["clearance_required"],
                        fields["saudi_national_required"],
                        fields["job_type"],
                        row["id"],
                    )
                )
            cur.executemany(
                f"UPDATE {JOBS_TABLE} SET work_model=%s, experience_level=%s, "
                f"years_experience=%s, visa_sponsorship=%s, clearance_required=%s, "
                f"saudi_national_required=%s, job_type=%s, listing_inferred=1 WHERE id=%s",
                payload,
            )
        log.info("Backfilled listing fields on %s jobs", len(payload))
        return len(payload)
    except Exception:
        close_conn()
        log.exception("Listing field backfill failed")
        return 0


def clear_copied_api_raw_descriptions() -> int:
    """Remove raw_description values that were copied from our own jobs API fields.

    The real value is the source payload captured while scraping. Those copies
    are marked by api_id, which source boards do not send.
    """
    if not mysql_configured():
        return 0
    conn = get_conn()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {JOBS_TABLE} SET raw_description = '' "
                f"WHERE raw_description <> '' "
                f"AND CAST(raw_description AS CHAR) LIKE %s",
                ('%"api_id"%',),
            )
            cleared = int(cur.rowcount or 0)
        if cleared:
            log.info("Cleared copied API fields from raw_description on %s jobs", cleared)
        return cleared
    except Exception:
        close_conn()
        log.exception("raw_description cleanup failed")
        return 0


_STARTUP_MAINTENANCE_STARTED = False


def _is_placeholder_url(url: str) -> bool:
    """jobs.example.com and other example.com links are demo rows, not postings."""
    host = urlparse(str(url or "").strip()).netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host == "example.com" or host.endswith(".example.com")


def _is_placeholder_source(source_id: str) -> bool:
    return str(source_id or "").strip().lower().startswith("seed-")


def repair_placeholder_urls() -> int:
    """Replace an example.com link when the scrape saved the real posting URL."""
    if not mysql_configured():
        return 0
    conn = get_conn()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {JOBS_TABLE} "
                f"SET url = JSON_UNQUOTE(JSON_EXTRACT(raw_description, '$.job_url')) "
                f"WHERE (url LIKE %s OR url LIKE %s) "
                f"AND JSON_VALID(raw_description) "
                f"AND JSON_UNQUOTE(JSON_EXTRACT(raw_description, '$.job_url')) LIKE 'http%%' "
                f"AND JSON_UNQUOTE(JSON_EXTRACT(raw_description, '$.job_url')) NOT LIKE %s",
                ("%://jobs.example.com/%", "%://example.com/%", "%example.com%"),
            )
            fixed = int(cur.rowcount or 0)
        if fixed:
            log.info("Replaced %s placeholder job URLs from the saved source link", fixed)
        return fixed
    except Exception:
        close_conn()
        log.exception("placeholder URL repair failed")
        return 0


def delete_placeholder_jobs() -> int:
    """Remove demo rows whose apply link is not a real posting."""
    if not mysql_configured():
        return 0
    conn = get_conn()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {JOBS_TABLE} "
                f"WHERE url LIKE %s OR url LIKE %s OR source_id LIKE %s",
                ("%://jobs.example.com/%", "%://example.com/%", "seed-%"),
            )
            removed = int(cur.rowcount or 0)
        if removed:
            log.info("Removed %s placeholder job rows", removed)
        return removed
    except Exception:
        close_conn()
        log.exception("placeholder job cleanup failed")
        return 0


def _store_full_description(cur, row: dict, text: str, platform_posted_at: str = "") -> None:
    """Save posting text and the fields that are read from it."""
    from jobscraper.job_fields import infer_listing_fields

    body = str(text or "").strip()
    posted = _posted_mysql(platform_posted_at)
    if not body and not posted:
        return
    if posted and len(body) < 120:
        cur.execute(
            f"UPDATE {JOBS_TABLE} SET "
            f"platform_posted_at = %s, "
            f"posted_at = COALESCE(%s, posted_at) "
            f"WHERE id = %s AND ("
            f"platform_posted_at IS NULL OR TIME(platform_posted_at) = '00:00:00'"
            f")",
            (posted, posted, row["id"]),
        )
        return
    fields = infer_listing_fields(
        title=str(row.get("title") or ""),
        location=str(row.get("location") or ""),
        state=str(row.get("state") or ""),
        description=body,
        job_type=row.get("job_type") or "",
    )
    compensation = _compensation_text({"description": body}, body)
    payload = json.dumps({"jobDescription": body}, ensure_ascii=False)
    cur.execute(
        f"UPDATE {JOBS_TABLE} SET description = %s, "
        f"raw_description = IF("
        f"JSON_VALID(raw_description) AND CHAR_LENGTH(raw_description) > 2 "
        f"AND CAST(raw_description AS CHAR) NOT LIKE %s, "
        f"JSON_MERGE_PATCH(raw_description, %s), "
        f"IF(CHAR_LENGTH(raw_description) > 2 AND CAST(raw_description AS CHAR) NOT LIKE %s, "
        f"raw_description, %s)), "
        f"work_model = %s, experience_level = %s, years_experience = %s, "
        f"visa_sponsorship = %s, clearance_required = %s, saudi_national_required = %s, "
        f"job_type = %s, "
        f"compensation = IF(%s <> 'salary not disclosed', %s, compensation), "
        f"listing_inferred = 1, "
        f"platform_posted_at = IF("
        f"%s IS NULL, platform_posted_at, "
        f"IF(platform_posted_at IS NULL OR TIME(platform_posted_at) = '00:00:00' "
        f"OR TIME(%s) <> '00:00:00', %s, platform_posted_at)"
        f"), "
        f"posted_at = COALESCE(%s, posted_at) "
        f"WHERE id = %s AND ("
        f"CHAR_LENGTH(description) < CHAR_LENGTH(%s) OR "
        f"(%s IS NOT NULL AND (platform_posted_at IS NULL OR TIME(platform_posted_at) = '00:00:00'))"
        f")",
        (
            body,
            '%"api_id"%',
            payload,
            '%"api_id"%',
            payload,
            str(fields["work_model"] or "unknown")[:16],
            str(fields["experience_level"] or "unknown")[:32],
            fields["years_experience"],
            str(fields["visa_sponsorship"] or "unknown")[:16],
            str(fields["clearance_required"] or "unknown")[:16],
            str(fields["saudi_national_required"] or "no")[:16],
            fields["job_type"],
            compensation,
            compensation,
            posted,
            posted,
            posted,
            posted,
            row["id"],
            body,
            posted,
        ),
    )


def fill_descriptions_from_raw() -> int:
    """Copy a full posting out of raw_description when the description column is shorter."""
    if not mysql_configured():
        return 0
    conn = get_conn()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, title, location, state, job_type, "
                f"JSON_UNQUOTE(JSON_EXTRACT(raw_description, '$.jobDescription')) AS body "
                f"FROM {JOBS_TABLE} "
                f"WHERE JSON_VALID(raw_description) "
                f"AND JSON_EXTRACT(raw_description, '$.jobDescription') IS NOT NULL "
                f"AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(raw_description, '$.jobDescription'))) "
                f"> CHAR_LENGTH(description)"
            )
            rows = list(cur.fetchall() or [])
            filled = 0
            for row in rows:
                text = str(row.get("body") or "").strip()
                if len(text) < 120:
                    continue
                _store_full_description(cur, row, text)
                if cur.rowcount:
                    filled += 1
        if filled:
            log.info("Copied full descriptions from raw_description on %s jobs", filled)
        return filled
    except Exception:
        close_conn()
        log.exception("raw description copy failed")
        return 0


def backfill_compensation() -> int:
    """Read salary from postings saved before compensation existed."""
    if not mysql_configured():
        return 0
    conn = get_conn()
    if conn is None:
        return 0
    updated = 0
    try:
        with conn.cursor() as cur:
            while updated < 8000:
                cur.execute(
                    f"""
                    SELECT id,
                           LEFT(description, 12000) AS description,
                           IF(JSON_VALID(raw_description), JSON_UNQUOTE(JSON_EXTRACT(raw_description, '$.min_amount')), NULL) AS min_amount,
                           IF(JSON_VALID(raw_description), JSON_UNQUOTE(JSON_EXTRACT(raw_description, '$.max_amount')), NULL) AS max_amount,
                           IF(JSON_VALID(raw_description), JSON_UNQUOTE(JSON_EXTRACT(raw_description, '$.currency')), NULL) AS currency,
                           IF(JSON_VALID(raw_description), JSON_UNQUOTE(JSON_EXTRACT(raw_description, '$.interval')), NULL) AS salary_interval,
                           IF(JSON_VALID(raw_description), JSON_EXTRACT(raw_description, '$.pay'), NULL) AS pay,
                           IF(JSON_VALID(raw_description), JSON_EXTRACT(raw_description, '$.salary'), NULL) AS salary,
                           IF(JSON_VALID(raw_description), JSON_EXTRACT(raw_description, '$.salaryRange'), NULL) AS salary_range,
                           IF(JSON_VALID(raw_description), JSON_EXTRACT(raw_description, '$.baseSalary'), NULL) AS base_salary,
                           IF(JSON_VALID(raw_description), JSON_EXTRACT(raw_description, '$.pay_input_ranges'), NULL) AS pay_input_ranges,
                           LEFT(IF(JSON_VALID(raw_description), JSON_UNQUOTE(JSON_EXTRACT(raw_description, '$.jobDescription')), ''), 12000) AS job_description
                    FROM {JOBS_TABLE}
                    WHERE compensation_checked = 0
                    ORDER BY id
                    LIMIT 200
                    """
                )
                rows = list(cur.fetchall() or [])
                if not rows:
                    break
                payload = []
                for row in rows:
                    raw = {
                        "min_amount": row.get("min_amount"),
                        "max_amount": row.get("max_amount"),
                        "currency": row.get("currency"),
                        "interval": row.get("salary_interval"),
                        "pay": row.get("pay"),
                        "salary": row.get("salary"),
                        "salaryRange": row.get("salary_range"),
                        "baseSalary": row.get("base_salary"),
                        "pay_input_ranges": row.get("pay_input_ranges"),
                    }
                    body = str(row.get("description") or "")
                    extra = str(row.get("job_description") or "")
                    text = _compensation_text({"raw": raw, "description": body}, body)
                    if text == "salary not disclosed" and extra and extra != body:
                        text = _compensation_text({"raw": raw, "description": extra}, extra)
                    payload.append((text, row["id"]))
                cur.executemany(
                    f"UPDATE {JOBS_TABLE} SET compensation = %s, compensation_checked = 1 "
                    f"WHERE id = %s AND compensation_checked = 0",
                    payload,
                )
                updated += len(payload)
                if len(rows) < 200:
                    break
        if updated:
            log.info("Backfilled compensation on %s jobs", updated)
        return updated
    except Exception:
        close_conn()
        log.exception("Compensation backfill failed")
        return updated


def _startup_maintenance() -> None:
    """Backfill and cleanup after uvicorn is already listening."""
    try:
        from jobscraper.whatsapp_alerts import queue_unsent_group_jobs, start_group_sweep

        # Do this before the description backfill. That work can take many
        # minutes, and the group should not wait for it.
        queue_unsent_group_jobs()
        start_group_sweep()
    except Exception:
        log.exception("WhatsApp group sweep failed to start")
    try:
        repair_placeholder_urls()
        delete_placeholder_jobs()
        fill_descriptions_from_raw()
        backfill_listing_fields()
        backfill_compensation()
        clear_copied_api_raw_descriptions()
    except Exception:
        log.exception("Startup maintenance failed")
    start_dice_description_backfill()
    try:
        from jobscraper.whatsapp_alerts import queue_recent_unsent

        queue_recent_unsent()
    except Exception:
        log.exception("WhatsApp catch-up failed")


def start_startup_maintenance() -> None:
    global _STARTUP_MAINTENANCE_STARTED
    if _STARTUP_MAINTENANCE_STARTED or not mysql_configured():
        return
    _STARTUP_MAINTENANCE_STARTED = True
    threading.Thread(
        target=_startup_maintenance,
        name="startup-maintenance",
        daemon=True,
    ).start()


_FILL_LOCK = threading.Lock()
_FILL_RUNNING = False
_FILL_AGAIN = False


def start_dice_description_backfill() -> None:
    """Keep filling title-only descriptions for as long as the API is running.

    One pass is not enough. The morning scrape saves LinkedIn cards with no
    body, and LinkedIn often refuses a burst of requests. This worker stays
    up, writes the newest blanks first, and tries the refused ones again.
    """
    global _FILL_AGAIN, _FILL_RUNNING
    if not mysql_configured():
        return
    with _FILL_LOCK:
        if _FILL_RUNNING:
            _FILL_AGAIN = True
            return
        _FILL_RUNNING = True

    def _run() -> None:
        global _FILL_AGAIN, _FILL_RUNNING
        try:
            while True:
                _FILL_AGAIN = False
                filled, retryable = _fill_short_dice_descriptions()
                if _FILL_AGAIN:
                    continue
                pause = 180 if retryable else 600
                log.info(
                    "Description refill sleeping %ss (filled=%s, still blank=%s)",
                    pause,
                    filled,
                    retryable,
                )
                _wait_before_next_fill(pause)
        finally:
            with _FILL_LOCK:
                _FILL_RUNNING = False

    threading.Thread(target=_run, name="posting-descriptions", daemon=True).start()


def _wait_before_next_fill(seconds: int) -> None:
    """Sleep, but wake immediately when a scrape has just saved new rows."""
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _FILL_AGAIN:
            return
        time.sleep(2)


def _mark_description_unavailable(cur, row_id: int) -> None:
    """Stop retrying a Headcount row whose employer page has no readable description."""
    marker = '{"description_unavailable":true}'
    cur.execute(
        f"UPDATE {JOBS_TABLE} SET raw_description = IF("
        f"JSON_VALID(raw_description) AND CHAR_LENGTH(raw_description) > 2 "
        f"AND CAST(raw_description AS CHAR) NOT LIKE %s, "
        f"JSON_MERGE_PATCH(raw_description, %s), %s) "
        f"WHERE id = %s",
        ('%"api_id"%', marker, marker, row_id),
    )


def _backfill_company_career_urls(cur) -> None:
    """Copy employer posting links that are already stored onto the new column."""
    board = (
        "%linkedin.com%",
        "%indeed.com%",
        "%bayt.com%",
        "%glassdoor.%",
        "%dice.com%",
        "%google.com%",
        "%ycombinator.com%",
        "%devglobaljobs.com%",
        "%example.com%",
    )
    not_board = " AND ".join(["url NOT LIKE %s"] * len(board))
    cur.execute(
        f"UPDATE {JOBS_TABLE} SET company_career_url = LEFT(url, 1000) "
        f"WHERE (company_career_url IS NULL OR company_career_url = '') "
        f"AND url LIKE 'http%%' AND {not_board}",
        board,
    )
    direct_not_board = " AND ".join(
        ["JSON_UNQUOTE(JSON_EXTRACT(raw_description, '$.job_url_direct')) NOT LIKE %s"] * len(board)
    )
    cur.execute(
        f"UPDATE {JOBS_TABLE} SET company_career_url = "
        f"LEFT(JSON_UNQUOTE(JSON_EXTRACT(raw_description, '$.job_url_direct')), 1000) "
        f"WHERE (company_career_url IS NULL OR company_career_url = '') "
        f"AND JSON_VALID(raw_description) "
        f"AND JSON_UNQUOTE(JSON_EXTRACT(raw_description, '$.job_url_direct')) LIKE 'http%%' "
        f"AND {direct_not_board}",
        board,
    )


def _store_company_career_url(cur, row_id: int, url: str) -> None:
    """Save an employer apply link. A later scrape must not wipe one we already have."""
    from jobscraper.agent_reach import employer_career_url

    career = employer_career_url(url)
    if not career:
        return
    cur.execute(
        f"UPDATE {JOBS_TABLE} SET company_career_url = %s "
        f"WHERE id = %s AND (company_career_url IS NULL OR TRIM(company_career_url) = '')",
        (career, row_id),
    )


def _mark_career_url_checked(cur, row_id: int) -> None:
    """Remember a LinkedIn page that has no employer apply link (Easy Apply)."""
    marker = '{"career_url_checked":true}'
    cur.execute(
        f"UPDATE {JOBS_TABLE} SET raw_description = IF("
        f"JSON_VALID(raw_description) AND CHAR_LENGTH(raw_description) > 2 "
        f"AND CAST(raw_description AS CHAR) NOT LIKE %s, "
        f"JSON_MERGE_PATCH(raw_description, %s), %s) "
        f"WHERE id = %s",
        ('%"api_id"%', marker, marker, row_id),
    )


def _mark_posting_missing(cur, row_id: int) -> None:
    """Remember a removed posting so the refill does not request it every pass."""
    marker = '{"posting_missing":true}'
    cur.execute(
        f"UPDATE {JOBS_TABLE} SET raw_description = IF("
        f"JSON_VALID(raw_description) AND CHAR_LENGTH(raw_description) > 2 "
        f"AND CAST(raw_description AS CHAR) NOT LIKE %s, "
        f"JSON_MERGE_PATCH(raw_description, %s), %s) "
        f"WHERE id = %s",
        ('%"api_id"%', marker, marker, row_id),
    )


def _fill_missing_career_urls(conn) -> None:
    """Match board listings to the employer career posting.

    LinkedIn's public page no longer includes the offsite Apply URL. A short
    batch searches the company career page by title so description refill is
    not blocked.
    """
    from jobscraper.career_portal import find_company_career_url

    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, title, company, location FROM {JOBS_TABLE} "
                f"WHERE platform IN ('LinkedIn', 'Indeed') "
                f"AND (company_career_url IS NULL OR company_career_url = '') "
                f"AND CHAR_LENGTH(description) >= 400 "
                f"AND CAST(raw_description AS CHAR) NOT LIKE %s "
                f"AND CAST(raw_description AS CHAR) NOT LIKE %s "
                f"ORDER BY id DESC LIMIT 8",
                ("%career_url_checked%", "%posting_missing%"),
            )
            rows = list(cur.fetchall() or [])
    except Exception:
        log.exception("Could not list jobs missing a company career URL")
        return
    if not rows:
        return
    saved = 0
    for row in rows:
        try:
            career = find_company_career_url(
                str(row.get("company") or ""),
                str(row.get("title") or ""),
                str(row.get("location") or ""),
            )
        except Exception:
            log.exception("Company career search failed for %s", row.get("company"))
            continue
        try:
            with conn.cursor() as cur:
                if career:
                    _store_company_career_url(cur, int(row["id"]), career)
                    saved += 1
                elif career == "":
                    _mark_career_url_checked(cur, int(row["id"]))
        except Exception:
            log.exception("Could not save company career URL for job %s", row.get("id"))
    if saved:
        log.info("Saved %s company career URLs", saved)


def _fill_short_dice_descriptions() -> tuple[int, int]:
    import time
    import urllib.error

    from jobscraper.agent_reach import fetch_posting_details

    conn = get_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, url, title, platform, location, state, job_type, platform_posted_at "
                f"FROM {JOBS_TABLE} "
                f"WHERE TRIM(url) <> '' "
                f"AND url NOT LIKE %s "
                f"AND CAST(raw_description AS CHAR) NOT LIKE %s "
                f"AND CAST(raw_description AS CHAR) NOT LIKE %s "
                f"AND ("
                f"("
                f"platform IN ('LinkedIn', 'Bayt', 'Indeed') "
                f"AND ("
                f"CHAR_LENGTH(description) < 400 OR description = title "
                f"OR platform_posted_at IS NULL "
                f"OR (platform = 'LinkedIn' AND TIME(platform_posted_at) = '00:00:00')"
                f")"
                f") OR ("
                f"platform IN ('Headcount Labs', 'Greenhouse', 'Lever', 'SmartRecruiters', 'Workday') "
                f"AND (CHAR_LENGTH(description) < 400 OR description = title)"
                f")"
                f") "
                f"ORDER BY id DESC",
                ("%example.com%", "%posting_missing%", "%description_unavailable%"),
            )
            rows = list(cur.fetchall() or [])
    except Exception:
        close_conn()
        log.exception("Could not list short job descriptions")
        return 0, 0
    try:
        _fill_missing_career_urls(conn)
    except Exception:
        log.exception("Company career URL fill failed")
    if not rows:
        return 0, 0
    log.info("Filling %s short descriptions from posting pages", len(rows))
    filled = 0
    retryable = 0
    consecutive_failures = 0
    from jobscraper.sources import is_platform_active

    for row in rows:
        platform = str(row.get("platform") or "")
        if not is_platform_active(platform):
            continue
        time.sleep(1.0)
        url = str(row.get("url") or "").strip()
        if not url or _is_placeholder_url(url):
            continue
        details = {"description": "", "platform_posted_at": ""}
        gone = False
        for attempt in range(4):
            try:
                details = fetch_posting_details(platform, url)
                break
            except urllib.error.HTTPError as exc:
                if exc.code in {404, 410}:
                    try:
                        with conn.cursor() as cur:
                            _mark_posting_missing(cur, int(row["id"]))
                    except Exception:
                        log.exception("Could not mark removed posting %s", url)
                    gone = True
                    break
                if exc.code == 429 and attempt < 3:
                    time.sleep(12)
                    continue
                details = {"description": "", "platform_posted_at": ""}
                break
            except Exception:
                log.exception("Description fetch failed for %s", url)
                details = {"description": "", "platform_posted_at": ""}
                break
        if gone:
            continue
        career = str(details.get("company_career_url") or "").strip()
        if career or (platform == "LinkedIn" and details.get("career_url_known")):
            try:
                with conn.cursor() as cur:
                    if career:
                        _store_company_career_url(cur, int(row["id"]), career)
                    else:
                        _mark_career_url_checked(cur, int(row["id"]))
            except Exception:
                log.exception("Could not save company career URL for %s", url)
        if platform in {"Headcount Labs", "Greenhouse", "Lever", "SmartRecruiters", "Workday"} and details.get("supported") is False:
            try:
                with conn.cursor() as cur:
                    _mark_description_unavailable(cur, int(row["id"]))
            except Exception:
                log.exception("Could not skip description for %s", url)
            continue
        text = str(details.get("description") or "")
        platform_posted = str(details.get("platform_posted_at") or "")
        if (not text or len(text) < 120) and not platform_posted:
            retryable += 1
            consecutive_failures += 1
            if consecutive_failures >= 8:
                log.warning("Description fill is being rate limited. Pausing before the next posting.")
                time.sleep(45)
                consecutive_failures = 0
            continue
        consecutive_failures = 0
        try:
            with conn.cursor() as cur:
                _store_full_description(cur, row, text, platform_posted)
            filled += 1
        except Exception:
            close_conn()
            conn = get_conn()
            if conn is None:
                break
            log.exception("Job description update failed")
    log.info("Filled %s descriptions from posting pages", filled)
    return filled, retryable


def _posting_skills(job: dict, title: str, description: str) -> list[str]:
    """Keep board skills and add skills named in the posting itself."""
    from jobscraper.recommend import normalize_skills
    from jobscraper.resume_parser import SKILL_CANONICAL, SKILL_PATTERN

    found = list(job.get("skills") or [])
    blob = f"{title}\n{description[:4000]}"
    for match in SKILL_PATTERN.findall(blob):
        found.append(SKILL_CANONICAL[match.lower()])
    return normalize_skills(found)[:30]


def _as_skills_json(value) -> str:
    if not value:
        items = []
    elif isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
    else:
        items = [part.strip() for part in str(value).split(",") if part.strip()]
    return json.dumps(items, ensure_ascii=False)


def _clip(value, limit: int, default: str = "") -> str:
    text = str(value or "").strip()
    return (text or default)[:limit]


def _public_id(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _company_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())[:255]


def _posted_mysql(value) -> str | None:
    """Accept ISO dates, epoch stamps, or board labels like '19 hours ago'."""
    from jobscraper.agent_reach import absolute_posted_at

    stamp = absolute_posted_at(value)
    return stamp or None


def _compensation_text(job: dict, description: str) -> str:
    from jobscraper.job_fields import SALARY_NOT_DISCLOSED, compensation_text

    text = compensation_text(job, description).strip() or SALARY_NOT_DISCLOSED
    return text[:255]


def _listing_fields(job: dict, description: str) -> dict:
    from jobscraper.job_fields import infer_listing_fields

    fields = infer_listing_fields(
        title=str(job.get("title") or ""),
        location=str(job.get("location") or ""),
        state=str(job.get("state") or ""),
        description=description,
        job_type=job.get("job_type") or "",
    )
    level = str(fields["experience_level"] or "unknown")
    return {
        "job_type": fields["job_type"],
        "work_model": str(fields["work_model"] or "unknown")[:16],
        "experience_level": level[:32],
        "years_experience": fields["years_experience"],
        "visa_sponsorship": str(fields["visa_sponsorship"] or "unknown")[:16],
        "clearance_required": str(fields["clearance_required"] or "unknown")[:16],
        "saudi_national_required": str(fields["saudi_national_required"] or "no")[:16],
    }


def _json_ready(value):
    """Turn API payloads into values json.dumps can store."""
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "ignore")
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if hasattr(value, "item"):
        try:
            return _json_ready(value.item())
        except Exception:
            pass
    return str(value)


_API_JOB_KEYS = {
    "id",
    "title",
    "company",
    "skills",
    "state",
    "location",
    "platform",
    "daysAgo",
    "url",
    "company_career_url",
    "description",
    "job_type",
    "work_model",
    "experience_level",
    "years_experience",
    "visa_sponsorship",
    "clearance_required",
    "saudi_national_required",
    "compensation",
    "posted_at",
    "platform_posted_at",
    "platformPostedAt",
    "saved_at",
    "savedAt",
    "api_id",
    "job_id",
    "search_text",
    "raw",
    "raw_description",
}


def _is_copied_api_job(value: dict) -> bool:
    """True when this object is our jobs API row, not a scraped source payload."""
    if "api_id" in value:
        return True
    copied = {"visa_sponsorship", "saudi_national_required", "daysAgo"}
    return copied.issubset(value) and set(value).issubset(_API_JOB_KEYS)


def _is_empty_source_value(value) -> bool:
    if value is None or value == [] or value == {}:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, float) and value != value:
        return True
    return False


def source_extras(value) -> dict:
    """Scraped fields that are not already stored in our job columns."""
    if isinstance(value, str):
        text = value.strip()
        if not text or text in {"{}", "[]", "null"}:
            return {}
        if text[:1] in "{[":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {"text": text[:500000]}
            return source_extras(parsed)
        return {"text": text[:500000]}
    if isinstance(value, list):
        items = [item for item in (_json_ready(item) for item in value) if not _is_empty_source_value(item)]
        return {"items": items} if items else {}
    if not isinstance(value, dict):
        if _is_empty_source_value(value):
            return {}
        ready = _json_ready(value)
        return {} if _is_empty_source_value(ready) else {"value": ready}
    if _is_copied_api_job(value):
        return {}
    extra = {}
    for key, item in _json_ready(value).items():
        if str(key) in _API_JOB_KEYS:
            continue
        if _is_empty_source_value(item):
            continue
        extra[str(key)] = item
    return extra


def source_extras_json(value) -> str:
    """JSON object of scraped fields outside our columns. Empty object when there are none."""
    try:
        return json.dumps(source_extras(value), ensure_ascii=False)[:500000]
    except (TypeError, ValueError):
        return "{}"


def _raw_description_json(job: dict) -> str:
    """Scrape fields that are not already our job columns."""
    raw = job.get("raw")
    if raw is None or raw == "":
        raw = job.get("raw_description")
    return source_extras_json(raw)


def _years_value(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row_from_item(job: dict) -> dict | None:
    source_id = _clip(job.get("id"), 255)
    title = _clip(job.get("title"), 512)
    company = _clip(job.get("company"), 255)
    if not source_id or not title or not company:
        return None
    url = _clip(job.get("url"), 1000)
    if _is_placeholder_url(url) or _is_placeholder_source(source_id):
        return None
    from jobscraper.agent_reach import company_career_url_for_job

    career_url = _clip(company_career_url_for_job(job), 1000)
    description = str(job.get("description") or job.get("search_text") or "").strip()
    # LinkedIn cards often repeat the title and nothing else. Saving that
    # makes About the Role look filled when the posting was never loaded.
    if description and title and description.casefold() == title.casefold():
        description = ""
    skills = _as_skills_json(_posting_skills(job, title, description))
    try:
        days_ago = int(job.get("daysAgo") or 0)
    except (TypeError, ValueError):
        days_ago = 0
    return {
        "public_id": _public_id(source_id),
        "source_id": source_id,
        "title": title,
        "company": company,
        "company_key": _company_key(company),
        "skills": skills,
        "state": _clip(job.get("state"), 255),
        "location": _clip(job.get("location"), 512, "Saudi Arabia"),
        "platform": _clip(job.get("platform"), 120, "Unknown"),
        "days_ago": max(days_ago, 0),
        "url": url,
        "company_career_url": career_url,
        "description": description,
        "raw_description": _raw_description_json(job),
        "posted_at": _posted_mysql(
            job.get("platform_posted_at") or job.get("platformPostedAt") or job.get("posted_at")
        ),
        "platform_posted_at": _posted_mysql(
            job.get("platform_posted_at") or job.get("platformPostedAt") or job.get("posted_at")
        ),
        **_listing_fields(job, description),
        "compensation": _compensation_text(job, description),
    }


def _company_ids(cur, names: dict[str, str]) -> dict[str, int]:
    """Map normalized company name -> companies.id, inserting a row when missing."""
    found: dict[str, int] = {}
    for key, name in names.items():
        if not key or key in found:
            continue
        cur.execute(
            f"SELECT id FROM {COMPANIES_TABLE} WHERE normalized_name = %s OR LOWER(name) = %s LIMIT 1",
            (key, key),
        )
        row = cur.fetchone()
        if row:
            found[key] = int(row["id"])
            continue
        _write_row(
            cur,
            f"""
            INSERT INTO {COMPANIES_TABLE} (
              public_id, name, normalized_name, website,
              sponsorship_notes, logo_url, created_at, updated_at
            ) VALUES (%s, %s, %s, '', '', '', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
            ON DUPLICATE KEY UPDATE updated_at = {COMPANIES_TABLE}.updated_at
            """,
            (_public_id("company:" + key), name, key),
            table=COMPANIES_TABLE,
        )
        cur.execute(
            f"SELECT id FROM {COMPANIES_TABLE} WHERE public_id = %s OR normalized_name = %s LIMIT 1",
            (_public_id("company:" + key), key),
        )
        row = cur.fetchone()
        if row:
            found[key] = int(row["id"])
    return found


def _chunks(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _known_listings(cur, rows: list[dict]) -> tuple[set[str], set[str]]:
    """Source ids and URLs already stored. A repeat scrape must not alert again."""
    known_ids: set[str] = set()
    known_urls: set[str] = set()
    source_ids = [row["source_id"] for row in rows if row.get("source_id")]
    urls = [row["url"] for row in rows if row.get("url")]
    for chunk in _chunks(source_ids, 200):
        marks = ", ".join(["%s"] * len(chunk))
        cur.execute(
            f"SELECT source_id FROM {JOBS_TABLE} WHERE source_id IN ({marks})",
            chunk,
        )
        for hit in cur.fetchall() or []:
            if hit.get("source_id"):
                known_ids.add(str(hit["source_id"]))
    for chunk in _chunks(urls, 100):
        marks = ", ".join(["%s"] * len(chunk))
        cur.execute(
            f"SELECT url FROM {JOBS_TABLE} WHERE url IN ({marks})",
            chunk,
        )
        for hit in cur.fetchall() or []:
            if hit.get("url"):
                known_urls.add(str(hit["url"]))
    return known_ids, known_urls


def upsert_jobs(jobs: list[dict], *, notify: bool = True) -> UpsertStats:
    if not jobs or not mysql_configured():
        return UpsertStats()
    rows = [row for row in (_row_from_item(job) for job in jobs) if row]
    if not rows:
        return UpsertStats()
    unique = {}
    for row in rows:
        unique[row["source_id"]] = row
    by_url = {}
    for row in unique.values():
        by_url[row["url"] or f"id:{row['source_id']}"] = row
    rows = list(by_url.values())
    conn = get_conn()
    if conn is None:
        return UpsertStats()
    fresh: list[dict] = []
    duplicate_count = 0
    try:
        with _LOCK:
            with conn.cursor() as cur:
                try:
                    known_ids, _known_urls = _known_listings(cur, rows)
                    fresh = [row for row in rows if row["source_id"] not in known_ids]
                    duplicate_count = len(rows) - len(fresh)
                except Exception:
                    log.exception("Could not detect new vs duplicate jobs")
                    # Fail closed for alerts: never treat unknown rows as brand new.
                    fresh = []
                    duplicate_count = 0
                _default_omitted_columns(cur)
                company_ids = _company_ids(
                    cur,
                    {row["company_key"]: row["company"] for row in rows if row["company_key"]},
                )
                payload = [
                    (
                        row["public_id"],
                        row["source_id"],
                        row["title"],
                        row["company"],
                        company_ids.get(row["company_key"]),
                        row["skills"],
                        row["state"],
                        row["location"],
                        row["platform"],
                        row["days_ago"],
                        row["url"],
                        row["description"],
                        row["raw_description"] or "",
                        row["posted_at"],
                        row["platform_posted_at"],
                        row["job_type"] if row["job_type"] in {"fulltime", "parttime", "contract", "internship"} else "fulltime",
                        row["work_model"],
                        row["experience_level"],
                        row["years_experience"],
                        row["visa_sponsorship"],
                        row["clearance_required"],
                        row["saudi_national_required"],
                        row["compensation"],
                        row["company_career_url"],
                    )
                    for row in rows
                ]
                _write_row(cur, _upsert_sql(cur), payload, table=JOBS_TABLE, many=True)
    except Exception:
        close_conn()
        log.exception("MySQL upsert failed")
        raise
    if notify and fresh:
        try:
            from jobscraper.whatsapp_alerts import queue_job_alerts

            queue_job_alerts(fresh)
        except Exception:
            log.exception("Could not queue WhatsApp alerts")
    saved = len(rows)
    new_count = max(0, saved - duplicate_count)
    return UpsertStats(saved=saved, new=new_count, duplicates=duplicate_count)


def _parse_skills(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "ignore")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(parsed, list):
            return [str(v).strip() for v in parsed if str(v).strip()]
    return []


def _days_ago_from_iso(value: str | None, fallback: int = 0) -> int:
    if not value:
        return fallback
    cleaned = str(value).strip().replace("Z", "+00:00")
    try:
        posted = datetime.fromisoformat(cleaned)
    except ValueError:
        return fallback
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - posted).total_seconds() // 86400))


def _parse_raw_description(value):
    """Always an object of scrape fields that are not our columns."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "ignore")
    return source_extras(value)


def _fmt_mysql_dt(value) -> str | None:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    text = str(value or "").strip()
    return text or None


def _job_payload(row: dict, *, include_raw: bool) -> dict:
    platform_posted = _fmt_mysql_dt(row.get("platform_posted_at")) or _fmt_mysql_dt(row.get("posted_at"))
    posted = platform_posted or _fmt_mysql_dt(row.get("posted_at"))
    saved = (
        _fmt_mysql_dt(row.get("first_seen_at"))
        or _fmt_mysql_dt(row.get("created_at"))
        or None
    )
    days = _days_ago_from_iso(platform_posted or posted, int(row.get("days_ago") or 0))
    payload = {
        "id": row.get("id"),
        "title": row.get("title"),
        "company": row.get("company"),
        "skills": _parse_skills(row.get("skills")),
        "state": row.get("state") or "",
        "location": row.get("location") or "",
        "platform": row.get("platform") or "",
        "daysAgo": days,
        "url": row.get("url") or "",
        "company_career_url": str(row.get("company_career_url") or "").strip() or None,
        "description": row.get("description") or "",
        "job_type": row.get("job_type") or "fulltime",
        "work_model": row.get("work_model") or "unknown",
        "experience_level": row.get("experience_level") or "unknown",
        "years_experience": _years_value(row.get("years_experience")),
        "visa_sponsorship": row.get("visa_sponsorship") or "unknown",
        "clearance_required": row.get("clearance_required") or "unknown",
        "saudi_national_required": "yes" if row.get("saudi_national_required") == "yes" else "no",
        "compensation": str(row.get("compensation") or "").strip() or "salary not disclosed",
        "posted_at": posted,
        "platform_posted_at": platform_posted,
        "platformPostedAt": platform_posted,
        "saved_at": saved,
        "savedAt": saved,
    }
    if row.get("api_id") is not None:
        payload["job_id"] = int(row["api_id"])
    if include_raw:
        payload["raw_description"] = _parse_raw_description(row.get("raw_description"))
    return payload


def _job_from_row(row: dict) -> dict:
    return _job_payload(row, include_raw=True)


def attach_descriptions(jobs: list[dict]) -> None:
    """Replace any shortened description with the full text stored in MySQL."""
    ids = []
    seen = set()
    for job in jobs:
        try:
            job_id = int(job["job_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if job_id not in seen:
            seen.add(job_id)
            ids.append(job_id)
    found = _descriptions_by_id(ids)
    for job in jobs:
        try:
            job_id = int(job["job_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if job_id in found:
            job["description"] = found[job_id]


def _descriptions_by_id(ids: list[int]) -> dict[int, str]:
    if not ids or not mysql_configured():
        return {}
    found: dict[int, str] = {}
    conn = get_conn()
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            for start in range(0, len(ids), 50):
                chunk = ids[start : start + 50]
                slots = ", ".join(["%s"] * len(chunk))
                cur.execute(
                    f"SELECT id, description FROM {JOBS_TABLE} WHERE id IN ({slots})",
                    chunk,
                )
                for row in cur.fetchall() or []:
                    found[int(row["id"])] = str(row.get("description") or "")
        return found
    except Exception:
        close_conn()
        log.exception("description lookup failed")
        return found


def attach_raw_descriptions(jobs: list[dict]) -> None:
    """Fill raw_description from MySQL for these rows only."""
    ids: list[int] = []
    seen: set[int] = set()
    for job in jobs:
        try:
            job_id = int(job["job_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if job_id not in seen:
            seen.add(job_id)
            ids.append(job_id)
    found = _raw_descriptions_by_id(ids)
    for job in jobs:
        try:
            job_id = int(job["job_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if job_id in found:
            job["raw_description"] = found[job_id]
        else:
            job["raw_description"] = source_extras(job.get("raw_description"))


def _raw_descriptions_by_id(ids: list[int]) -> dict[int, object]:
    if not ids or not mysql_configured():
        return {}
    found: dict[int, object] = {}
    conn = get_conn()
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            for start in range(0, len(ids), 100):
                chunk = ids[start : start + 100]
                slots = ", ".join(["%s"] * len(chunk))
                cur.execute(
                    f"SELECT id, raw_description FROM {JOBS_TABLE} WHERE id IN ({slots})",
                    chunk,
                )
                for row in cur.fetchall() or []:
                    found[int(row["id"])] = _parse_raw_description(row.get("raw_description"))
        return found
    except Exception:
        close_conn()
        log.exception("raw_description lookup failed")
        return found


def load_jobs(description_chars: int = 800) -> list[dict]:
    load_jobs.last_error = None
    if not mysql_configured():
        return []
    try:
        preview = int(description_chars)
    except (TypeError, ValueError):
        preview = 800
    preview = max(200, min(preview, 8000))
    try:
        conn = get_conn()
        if conn is None:
            return []
        with conn.cursor() as cur:
            # raw_description stays off this query. It is a LONGTEXT, and loading it
            # for every row stalls MySQL. Description here is a bounded preview;
            # the jobs API replaces it with the full column for the rows it returns.
            # Recommend asks for a longer slice so a skill past the first 800
            # characters can still match.
            cur.execute(
                f"SELECT id AS api_id, "
                f"COALESCE(NULLIF(source_id, ''), public_id, CAST(id AS CHAR)) AS id, "
                f"title, company, skills, state, location, platform, "
                f"days_ago, url, company_career_url, LEFT(description, %s) AS description, "
                f"job_type, work_model, experience_level, "
                f"years_experience, visa_sponsorship, clearance_required, saudi_national_required, "
                f"compensation, posted_at, platform_posted_at, first_seen_at, created_at "
                f"FROM {JOBS_TABLE} "
                f"WHERE url NOT LIKE %s AND url NOT LIKE %s "
                f"AND (source_id IS NULL OR source_id NOT LIKE %s) "
                f"ORDER BY last_seen_at DESC",
                (preview, "%://jobs.example.com/%", "%://example.com/%", "seed-%"),
            )
            rows = cur.fetchall() or []
        load_jobs.last_error = None
        return [_job_from_row(row) for row in rows]
    except Exception as exc:
        load_jobs.last_error = str(exc)[:300]
        log.exception("MySQL load_jobs failed")
        close_conn()
        return []


def load_job(job_ref: str) -> dict | None:
    """One job by numeric id, public_id, or source id, including the full description."""
    ref = (job_ref or "").strip()
    if not ref or not mysql_configured():
        return None
    conn = get_conn()
    if conn is None:
        return None
    clauses = ["public_id = %s", "source_id = %s"]
    params: list = [ref, ref]
    if ref.isdigit():
        clauses.insert(0, "id = %s")
        params.insert(0, int(ref))
    sql = (
        "SELECT id AS api_id, "
        "COALESCE(NULLIF(source_id, ''), public_id, CAST(id AS CHAR)) AS id, "
        "title, company, skills, state, location, platform, "
        "days_ago, url, company_career_url, description, "
        "job_type, work_model, experience_level, "
        "years_experience, visa_sponsorship, clearance_required, saudi_national_required, "
        "compensation, posted_at, platform_posted_at, first_seen_at, created_at "
        f"FROM {JOBS_TABLE} WHERE " + " OR ".join(clauses) + " LIMIT 1"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if not row:
            return None
        return _job_from_row(row)
    except Exception:
        log.exception("MySQL load_job failed for %s", ref)
        close_conn()
        raise


def job_count() -> int:
    if not mysql_configured():
        return 0
    try:
        conn = get_conn()
        if conn is None:
            return 0
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM {JOBS_TABLE}")
            row = cur.fetchone() or {}
        return int(row.get("n") or 0)
    except Exception:
        close_conn()
        return 0


def _profile_from_cell(value) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "ignore")
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def save_candidate(candidate_id: str, profile: dict) -> None:
    conn = get_conn()
    if conn is None:
        raise RuntimeError("MySQL is not configured")
    payload = json.dumps(profile, ensure_ascii=False)
    with conn.cursor() as cur:
        cur.execute(CREATE_CANDIDATES_SQL)
        cur.execute(
            f"INSERT INTO {CANDIDATES_TABLE} (candidate_id, profile, created_at, updated_at) "
            "VALUES (%s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)) AS new "
            "ON DUPLICATE KEY UPDATE profile = new.profile, updated_at = UTC_TIMESTAMP(6)",
            (candidate_id, payload),
        )


_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _quote_ident(name: str) -> str:
    if not _IDENT_RE.fullmatch(name or ""):
        raise ValueError(f"Unsafe SQL name: {name}")
    return f"`{name}`"


def load_resume_file_text(candidate_id: str) -> str:
    """Raw text of a resume file saved on the user, without profile or job-ad text."""
    found = load_resume_file(candidate_id)
    return str((found or {}).get("text") or "")


def load_resume_file(candidate_id: str) -> dict | None:
    """Original uploaded resume bytes + text when a file is stored for this user."""
    if not mysql_configured():
        return None
    conn = get_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            if str(candidate_id).isdigit():
                cur.execute("SELECT id FROM users WHERE id = %s LIMIT 1", (int(candidate_id),))
            else:
                cur.execute("SELECT id FROM users WHERE public_id = %s LIMIT 1", (candidate_id,))
            user = cur.fetchone()
            if not user:
                return None
            user_id = int(user["id"])
            profile_row = _profile_row(cur, user_id)
            return _resume_file_from_storage(cur, user_id, profile_row.get("profile_id"))
    except Exception:
        log.exception("load_resume_file failed for %s", candidate_id)
        close_conn()
        raise


def load_app_user(candidate_id: str) -> dict | None:
    """Read a users row (numeric id or public_id) into a recommend profile."""
    if not mysql_configured():
        return None
    conn = get_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            if str(candidate_id).isdigit():
                cur.execute(
                    "SELECT id, email, first_name, last_name FROM users WHERE id = %s LIMIT 1",
                    (int(candidate_id),),
                )
            else:
                cur.execute(
                    "SELECT id, email, first_name, last_name FROM users WHERE public_id = %s LIMIT 1",
                    (candidate_id,),
                )
            user = cur.fetchone()
            if not user:
                return None
            user_id = int(user["id"])
            profile_row = _profile_row(cur, user_id)
            profile_id = profile_row.get("profile_id")
            skills = _user_skill_values(cur, user_id, profile_id)
            skills.extend(_linked_names(cur, user_id, profile_id))
            certifications = _user_certification_entries(cur, user_id, profile_id)
            certifications.extend(_user_labeled_values(cur, user_id, profile_id, "certif"))
            certifications.extend(_user_labeled_values(cur, user_id, profile_id, "license"))
            certifications.extend(_linked_names(cur, user_id, profile_id, tokens=("certif", "license")))
            work_history = _user_experience(cur, user_id, profile_id)
            education = _user_education(cur, user_id, profile_id)
            achievements = _user_achievements(cur, user_id, profile_id)
            file_text = _resume_text_from_stored_files(cur, user_id, profile_row.get("profile_id"))
            extra_text = _user_text(cur, user_id, profile_id)
    except Exception:
        log.exception("load_app_user failed for %s", candidate_id)
        close_conn()
        raise
    professional_summary = str(profile_row.get("professional_summary") or "").strip()
    headline = str(profile_row.get("headline") or "").strip()
    # Keep the uploaded file when it is a real resume. Otherwise keep every text
    # source so a full CV mixed with saved job titles can still be cleaned later.
    if _resume_section_count(file_text) >= 2:
        resume_body = file_text
    else:
        resume_body = "\n".join(
            part for part in (file_text, headline, professional_summary, extra_text) if part
        )
    return {
        "first_name": user.get("first_name") or "",
        "last_name": user.get("last_name") or "",
        "email": user.get("email") or "",
        "phone": profile_row.get("phone") or "",
        "city": profile_row.get("city") or "",
        "linkedin_url": profile_row.get("linkedin_url") or "",
        "skills": skills,
        "certifications": certifications,
        "work_history": work_history,
        "education": education,
        "achievements": achievements,
        "professional_summary": professional_summary,
        "headline": headline,
        "resume_document": file_text,
        "resume_text": resume_body,
    }


def _profile_row(cur, user_id: int) -> dict:
    try:
        cur.execute(
            "SELECT id AS profile_id, phone, headline, professional_summary, linkedin_url, city "
            "FROM profiles WHERE user_id = %s LIMIT 1",
            (user_id,),
        )
        return cur.fetchone() or {}
    except Exception:
        log.exception("profiles lookup failed for user %s", user_id)
        return {}


def _user_skill_values(cur, user_id: int, profile_id) -> list[str]:
    return _user_labeled_values(cur, user_id, profile_id, "skill")


def _user_labeled_values(cur, user_id: int, profile_id, token: str) -> list[str]:
    like = f"%{token}%"
    cur.execute(
        """
        SELECT c.TABLE_NAME AS table_name,
               c.COLUMN_NAME AS skill_column,
               u.COLUMN_NAME AS owner_column
        FROM information_schema.COLUMNS c
        JOIN information_schema.COLUMNS u
          ON u.TABLE_SCHEMA = c.TABLE_SCHEMA
         AND u.TABLE_NAME = c.TABLE_NAME
         AND u.COLUMN_NAME IN ('user_id', 'profile_id')
        WHERE c.TABLE_SCHEMA = DATABASE()
          AND (c.COLUMN_NAME LIKE %s OR c.TABLE_NAME LIKE %s)
          AND c.DATA_TYPE IN ('varchar', 'char', 'text', 'mediumtext', 'longtext', 'json')
          AND c.COLUMN_NAME NOT IN ('user_id', 'profile_id', 'id')
        """,
        (like, like),
    )
    found: list[str] = []
    for row in cur.fetchall() or []:
        owner = row.get("owner_column")
        key = user_id if owner == "user_id" else profile_id
        if key is None:
            continue
        table = str(row.get("table_name") or "")
        column = str(row.get("skill_column") or "")
        if not _IDENT_RE.fullmatch(table) or not _IDENT_RE.fullmatch(column) or owner not in {"user_id", "profile_id"}:
            continue
        try:
            cur.execute(
                f"SELECT {_quote_ident(column)} AS skill FROM {_quote_ident(table)} "
                f"WHERE {_quote_ident(owner)} = %s LIMIT 40",
                (key,),
            )
            skill_rows = cur.fetchall() or []
        except Exception:
            log.exception("skill lookup failed on %s.%s", table, column)
            continue
        for skill_row in skill_rows:
            value = skill_row.get("skill")
            if isinstance(value, (bytes, bytearray)):
                value = value.decode("utf-8", "ignore")
            if isinstance(value, list):
                found.extend(str(item) for item in value if item)
                continue
            if isinstance(value, str) and value.strip().startswith("["):
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    found.extend(str(item) for item in parsed if item)
                    continue
            if value:
                found.append(str(value))
    return found


def _owner_links(cur, column_like: str | None = None, column_names: tuple[str, ...] = ()) -> list[dict]:
    filters = ["u.COLUMN_NAME IN ('user_id', 'profile_id', 'candidate_id')"]
    params: list[str] = []
    if column_like:
        filters.append("c.COLUMN_NAME LIKE %s")
        params.append(column_like)
    if column_names:
        marks = ", ".join(["%s"] * len(column_names))
        filters.append(f"c.COLUMN_NAME IN ({marks})")
        params.extend(column_names)
    cur.execute(
        f"""
        SELECT c.TABLE_NAME AS table_name,
               c.COLUMN_NAME AS value_column,
               u.COLUMN_NAME AS owner_column
        FROM information_schema.COLUMNS c
        JOIN information_schema.COLUMNS u
          ON u.TABLE_SCHEMA = c.TABLE_SCHEMA
         AND u.TABLE_NAME = c.TABLE_NAME
         AND u.COLUMN_NAME IN ('user_id', 'profile_id', 'candidate_id')
        WHERE c.TABLE_SCHEMA = DATABASE()
          AND {" AND ".join(filters)}
        """,
        params,
    )
    return list(cur.fetchall() or [])


def _rows_for_owner(cur, table: str, column: str, owner: str, key) -> list[dict]:
    if not _IDENT_RE.fullmatch(table) or not _IDENT_RE.fullmatch(column) or not _IDENT_RE.fullmatch(owner):
        return []
    try:
        cur.execute(
            f"SELECT {_quote_ident(column)} AS value FROM {_quote_ident(table)} "
            f"WHERE {_quote_ident(owner)} = %s LIMIT 30",
            (key,),
        )
        return list(cur.fetchall() or [])
    except Exception:
        log.exception("lookup failed on %s.%s", table, column)
        return []


def _linked_names(cur, user_id: int, profile_id, tokens: tuple[str, ...] = ("skill", "role")) -> list[str]:
    """Resolve skill_id / certification_id / target_role_id links to display names."""
    catalogs = []
    try:
        cur.execute(
            """
            SELECT TABLE_NAME AS table_name, COLUMN_NAME AS name_column
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME LIKE %s
              AND COLUMN_NAME IN ('name', 'title', 'label', 'skill_name')
            """,
            ("%skill%",),
        )
        catalogs.extend(cur.fetchall() or [])
        for token in tokens:
            if token == "skill":
                continue
            cur.execute(
                """
                SELECT TABLE_NAME AS table_name, COLUMN_NAME AS name_column
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME LIKE %s
                  AND COLUMN_NAME IN ('name', 'title', 'label', 'skill_name')
                """,
                (f"%{token}%",),
            )
            catalogs.extend(cur.fetchall() or [])
        if "role" in tokens:
            cur.execute(
                """
                SELECT TABLE_NAME AS table_name, COLUMN_NAME AS name_column
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME LIKE %s
                  AND COLUMN_NAME IN ('name', 'title', 'label')
                """,
                ("%target_role%",),
            )
            catalogs.extend(cur.fetchall() or [])
    except Exception:
        log.exception("catalog lookup failed")
        return []
    found: list[str] = []
    try:
        links = _owner_links(cur, column_like="%id")
    except Exception:
        log.exception("link lookup failed")
        return found
    for link in links:
        owner = link.get("owner_column")
        key = user_id if owner == "user_id" else profile_id
        if key is None or owner == "candidate_id":
            key = user_id if owner == "candidate_id" else key
        fk = str(link.get("value_column") or "")
        fk_tokens = [token for token in tokens if token in fk]
        if not fk_tokens:
            continue
        table = str(link.get("table_name") or "")
        for catalog in catalogs:
            cat_table = str(catalog.get("table_name") or "")
            cat_name = str(catalog.get("name_column") or "")
            if cat_table == table or not _IDENT_RE.fullmatch(cat_table) or not _IDENT_RE.fullmatch(cat_name):
                continue
            if not any(token in cat_table.lower() for token in fk_tokens):
                continue
            if not _IDENT_RE.fullmatch(table) or not _IDENT_RE.fullmatch(fk) or owner not in {"user_id", "profile_id", "candidate_id"}:
                continue
            if key is None:
                continue
            try:
                cur.execute(
                    f"SELECT cat.{_quote_ident(cat_name)} AS skill "
                    f"FROM {_quote_ident(table)} link "
                    f"JOIN {_quote_ident(cat_table)} cat ON cat.id = link.{_quote_ident(fk)} "
                    f"WHERE link.{_quote_ident(owner)} = %s LIMIT 40",
                    (key,),
                )
                for row in cur.fetchall() or []:
                    if row.get("skill"):
                        found.append(str(row["skill"]))
            except Exception:
                continue
    return found


_EXPERIENCE_TABLE = re.compile(r"experience|employment|work_histor|position|career", re.I)
_TITLE_COLUMNS = ("job_title", "role_title", "position", "title", "role")
_COMPANY_COLUMNS = (
    "company",
    "company_name",
    "employer",
    "employer_name",
    "organization",
    "organization_name",
    "organisation",
)
_START_COLUMNS = ("start_date", "started_at", "from_date", "start")
_END_COLUMNS = ("end_date", "ended_at", "to_date", "end")


def _first_named_column(columns: set[str], options: tuple[str, ...]) -> str | None:
    for name in options:
        if name in columns:
            return name
    return None


def _user_experience(cur, user_id: int, profile_id) -> list[dict]:
    """Job titles and dates from the user's experience rows, when that table exists."""
    try:
        cur.execute(
            """
            SELECT DISTINCT c.TABLE_NAME AS table_name, u.COLUMN_NAME AS owner_column
            FROM information_schema.COLUMNS c
            JOIN information_schema.COLUMNS u
              ON u.TABLE_SCHEMA = c.TABLE_SCHEMA
             AND u.TABLE_NAME = c.TABLE_NAME
             AND u.COLUMN_NAME IN ('user_id', 'profile_id')
            WHERE c.TABLE_SCHEMA = DATABASE()
              AND c.COLUMN_NAME IN ('job_title', 'title', 'position', 'role', 'role_title')
            """
        )
        links = list(cur.fetchall() or [])
    except Exception:
        log.exception("experience lookup failed")
        return []
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        table = str(link.get("table_name") or "")
        owner = str(link.get("owner_column") or "")
        if not _EXPERIENCE_TABLE.search(table) or (table, owner) in seen:
            continue
        seen.add((table, owner))
        key = {"user_id": user_id, "profile_id": profile_id}.get(owner)
        if key is None or not _IDENT_RE.fullmatch(table) or not _IDENT_RE.fullmatch(owner):
            continue
        try:
            cur.execute(
                """
                SELECT COLUMN_NAME AS column_name
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                """,
                (table,),
            )
            columns = {str(row.get("column_name") or "") for row in cur.fetchall() or []}
        except Exception:
            continue
        title_col = _first_named_column(columns, _TITLE_COLUMNS)
        if not title_col:
            continue
        company_col = _first_named_column(columns, _COMPANY_COLUMNS)
        start_col = _first_named_column(columns, _START_COLUMNS)
        end_col = _first_named_column(columns, _END_COLUMNS)
        desc_col = "description" if "description" in columns else None
        selected = [f"{_quote_ident(title_col)} AS job_title"]
        if company_col:
            selected.append(f"{_quote_ident(company_col)} AS company")
        if start_col:
            selected.append(f"{_quote_ident(start_col)} AS start_date")
        if end_col:
            selected.append(f"{_quote_ident(end_col)} AS end_date")
        if desc_col:
            selected.append(f"LEFT({_quote_ident(desc_col)}, 8000) AS description")
        try:
            cur.execute(
                f"SELECT {', '.join(selected)} FROM {_quote_ident(table)} "
                f"WHERE {_quote_ident(owner)} = %s LIMIT 20",
                (key,),
            )
            rows = cur.fetchall() or []
        except Exception:
            log.exception("experience read failed on %s", table)
            continue
        for row in rows:
            title = str(row.get("job_title") or "").strip()
            if not title:
                continue
            found.append(
                {
                    "company": str(row.get("company") or "").strip(),
                    "job_title": title[:400],
                    "start_date": str(row.get("start_date") or "")[:40],
                    "end_date": str(row.get("end_date") or "")[:40],
                    "description": str(row.get("description") or "").strip(),
                }
            )
            if len(found) >= 20:
                return found
    return found


_TEXT_COLUMNS = (
    "headline",
    "professional_summary",
    "summary",
    "description",
    "answer",
    "job_title",
    "title",
    "resume_text",
    "extracted_text",
    "raw_text",
    "parsed_text",
    "cv_text",
    "plain_text",
    "content",
    "body",
    "about",
    "bio",
    "degree",
    "field_of_study",
)


def _user_text(cur, user_id: int, profile_id) -> str:
    chunks: list[str] = []
    try:
        links = _owner_links(cur, column_names=_TEXT_COLUMNS)
    except Exception:
        log.exception("profile text lookup failed")
        return ""
    for link in links:
        owner = str(link.get("owner_column") or "")
        key = {"user_id": user_id, "profile_id": profile_id, "candidate_id": user_id}.get(owner)
        if key is None:
            continue
        for row in _rows_for_owner(
            cur,
            str(link.get("table_name") or ""),
            str(link.get("value_column") or ""),
            owner,
            key,
        ):
            value = row.get("value")
            if isinstance(value, str) and value.strip():
                chunks.append(value.strip()[:4000])
    return "\n".join(chunks)[:20000]


_RESUME_PATH_COLUMNS = (
    "file",
    "file_path",
    "path",
    "resume",
    "resume_path",
    "resume_file",
    "resume_url",
    "cv",
    "cv_file",
    "cv_path",
    "cv_url",
    "document",
    "document_path",
    "upload",
    "attachment",
    "storage_path",
    "disk_path",
    "original_path",
    "file_name",
    "filename",
    "original_name",
    "url",
)
_RESUME_SECTION_MARKERS = (
    "professional summary",
    "key achievements",
    "professional experience",
    "work experience",
    "core competencies",
    "certifications",
    "education",
    "experience",
    "skills",
)
_JOB_TITLE_ROLE_RE = re.compile(
    r"(?i)\b(engineers?|developers?|managers?|analysts?|architects?|interns?|consultants?|specialists?)\b"
)


def _resume_section_count(text: str) -> int:
    lowered = (text or "").lower()
    return sum(1 for marker in _RESUME_SECTION_MARKERS if marker in lowered)


def _looks_like_job_title_dump(text: str) -> bool:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 4:
        return False
    titles = sum(1 for line in lines if _JOB_TITLE_ROLE_RE.search(line) and len(line) < 120)
    dated = sum(1 for line in lines if re.search(r"(?i)\b(?:19|20)\d{2}\b|present", line))
    return titles >= 4 and dated <= 1 and titles >= max(3, len(lines) // 3)


def _user_certification_entries(cur, user_id: int, profile_id) -> list[str]:
    """Certification name and issuer from the same row, so issuers are not separate skills."""
    try:
        cur.execute(
            """
            SELECT DISTINCT c.TABLE_NAME AS table_name, u.COLUMN_NAME AS owner_column
            FROM information_schema.COLUMNS c
            JOIN information_schema.COLUMNS u
              ON u.TABLE_SCHEMA = c.TABLE_SCHEMA
             AND u.TABLE_NAME = c.TABLE_NAME
             AND u.COLUMN_NAME IN ('user_id', 'profile_id')
            WHERE c.TABLE_SCHEMA = DATABASE()
              AND (c.TABLE_NAME LIKE %s OR c.TABLE_NAME LIKE %s)
            """,
            ("%certif%", "%license%"),
        )
        links = list(cur.fetchall() or [])
    except Exception:
        log.exception("certification lookup failed")
        return []
    found: list[str] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        table = str(link.get("table_name") or "")
        owner = str(link.get("owner_column") or "")
        if (table, owner) in seen or not _IDENT_RE.fullmatch(table) or not _IDENT_RE.fullmatch(owner):
            continue
        seen.add((table, owner))
        key = {"user_id": user_id, "profile_id": profile_id}.get(owner)
        if key is None:
            continue
        try:
            cur.execute(
                """
                SELECT COLUMN_NAME AS column_name
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                """,
                (table,),
            )
            columns = {str(row.get("column_name") or "") for row in cur.fetchall() or []}
        except Exception:
            continue
        name_col = _first_named_column(columns, ("name", "title", "certificate", "certification", "credential"))
        issuer_col = _first_named_column(columns, ("issuer", "issuing_organization", "organization", "authority"))
        if not name_col:
            continue
        selected = [f"{_quote_ident(name_col)} AS name"]
        if issuer_col and issuer_col != name_col:
            selected.append(f"{_quote_ident(issuer_col)} AS issuer")
        try:
            cur.execute(
                f"SELECT {', '.join(selected)} FROM {_quote_ident(table)} "
                f"WHERE {_quote_ident(owner)} = %s LIMIT 20",
                (key,),
            )
            rows = cur.fetchall() or []
        except Exception:
            continue
        for row in rows:
            name = str(row.get("name") or "").strip()
            issuer = str(row.get("issuer") or "").strip()
            if not name:
                continue
            if issuer and issuer.lower() not in name.lower():
                found.append(f"{name} — {issuer}")
            else:
                found.append(name)
    return found


def _user_achievements(cur, user_id: int, profile_id) -> list[str]:
    """Achievement rows when the profile stores them apart from the job description."""
    try:
        cur.execute(
            """
            SELECT DISTINCT c.TABLE_NAME AS table_name, u.COLUMN_NAME AS owner_column
            FROM information_schema.COLUMNS c
            JOIN information_schema.COLUMNS u
              ON u.TABLE_SCHEMA = c.TABLE_SCHEMA
             AND u.TABLE_NAME = c.TABLE_NAME
             AND u.COLUMN_NAME IN ('user_id', 'profile_id')
            WHERE c.TABLE_SCHEMA = DATABASE()
              AND (c.TABLE_NAME LIKE %s OR c.TABLE_NAME LIKE %s)
            """,
            ("%achiev%", "%accomplishment%"),
        )
        links = list(cur.fetchall() or [])
    except Exception:
        log.exception("achievement lookup failed")
        return []
    found: list[str] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        table = str(link.get("table_name") or "")
        owner = str(link.get("owner_column") or "")
        if (table, owner) in seen or not _IDENT_RE.fullmatch(table) or not _IDENT_RE.fullmatch(owner):
            continue
        seen.add((table, owner))
        key = {"user_id": user_id, "profile_id": profile_id}.get(owner)
        if key is None:
            continue
        try:
            cur.execute(
                """
                SELECT COLUMN_NAME AS column_name
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                """,
                (table,),
            )
            columns = {str(row.get("column_name") or "") for row in cur.fetchall() or []}
        except Exception:
            continue
        title_col = _first_named_column(columns, ("title", "name", "label", "achievement"))
        detail_col = _first_named_column(columns, ("description", "details", "detail", "body", "summary"))
        if not title_col and not detail_col:
            continue
        selected = []
        if title_col:
            selected.append(f"{_quote_ident(title_col)} AS title")
        if detail_col and detail_col != title_col:
            selected.append(f"{_quote_ident(detail_col)} AS detail")
        try:
            cur.execute(
                f"SELECT {', '.join(selected)} FROM {_quote_ident(table)} "
                f"WHERE {_quote_ident(owner)} = %s LIMIT 20",
                (key,),
            )
            rows = cur.fetchall() or []
        except Exception:
            continue
        for row in rows:
            title = str(row.get("title") or "").strip()
            detail = str(row.get("detail") or "").strip()
            line = " — ".join(part for part in (title, detail) if part)
            if line:
                found.append(line[:500])
    return found


def _user_education(cur, user_id: int, profile_id) -> list[dict]:
    """School / degree rows for the user when an education table exists."""
    try:
        links = _owner_links(cur, column_like="%educ%")
        links.extend(_owner_links(cur, column_names=("school", "degree", "field_of_study", "institution")))
    except Exception:
        log.exception("education lookup failed")
        return []
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        table = str(link.get("table_name") or "")
        owner = str(link.get("owner_column") or "")
        if (table, owner) in seen or not _IDENT_RE.fullmatch(table) or not _IDENT_RE.fullmatch(owner):
            continue
        if not re.search(r"educ|school|academic", table, re.I):
            continue
        seen.add((table, owner))
        key = {"user_id": user_id, "profile_id": profile_id, "candidate_id": user_id}.get(owner)
        if key is None:
            continue
        try:
            cur.execute(
                f"""
                SELECT COLUMN_NAME AS column_name
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                """,
                (table,),
            )
            columns = {str(row.get("column_name") or "") for row in cur.fetchall() or []}
        except Exception:
            continue
        school_col = next((name for name in ("school", "institution", "university", "college") if name in columns), None)
        degree_col = next((name for name in ("degree", "qualification") if name in columns), None)
        field_col = next((name for name in ("field_of_study", "field", "major") if name in columns), None)
        year_col = next((name for name in ("graduation_year", "end_year", "year") if name in columns), None)
        if not school_col and not degree_col:
            continue
        selected = []
        if school_col:
            selected.append(f"{_quote_ident(school_col)} AS school")
        if degree_col:
            selected.append(f"{_quote_ident(degree_col)} AS degree")
        if field_col:
            selected.append(f"{_quote_ident(field_col)} AS field_of_study")
        if year_col:
            selected.append(f"{_quote_ident(year_col)} AS graduation_year")
        try:
            cur.execute(
                f"SELECT {', '.join(selected)} FROM {_quote_ident(table)} "
                f"WHERE {_quote_ident(owner)} = %s LIMIT 10",
                (key,),
            )
            rows = cur.fetchall() or []
        except Exception:
            continue
        for row in rows:
            school = str(row.get("school") or "").strip()
            degree = str(row.get("degree") or "").strip()
            field = str(row.get("field_of_study") or "").strip()
            if not school and not degree and not field:
                continue
            found.append(
                {
                    "school": school,
                    "degree": degree,
                    "field_of_study": field,
                    "graduation_year": str(row.get("graduation_year") or "").strip(),
                }
            )
            if len(found) >= 10:
                return found
    return found


def _resume_text_from_stored_files(cur, user_id: int, profile_id) -> str:
    found = _resume_file_from_storage(cur, user_id, profile_id)
    return str((found or {}).get("text") or "")


def _resume_file_from_storage(cur, user_id: int, profile_id) -> dict | None:
    """Return the best uploaded resume file bytes/text for this user."""
    try:
        from jobscraper.resume_parser import parse_resume_bytes
    except Exception:
        return None
    candidates: list[dict] = []
    for item in _resume_values_from_path_columns(cur, user_id, profile_id):
        packed = _resume_payload_from_value(item.get("value"), parse_resume_bytes, hint=item.get("hint") or "")
        if packed:
            candidates.append(packed)
    for item in _resume_values_from_blobs(cur, user_id, profile_id):
        packed = _resume_payload_from_value(item.get("value"), parse_resume_bytes, hint=item.get("hint") or "resume.pdf")
        if packed:
            candidates.append(packed)
    for item in _resume_values_from_media(cur, user_id, profile_id):
        packed = _resume_payload_from_value(item.get("value"), parse_resume_bytes, hint=item.get("hint") or "resume.pdf")
        if packed:
            candidates.append(packed)
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda item: (_resume_section_count(item.get("text") or ""), len(item.get("text") or ""), len(item.get("content") or b"")),
    )
    best["text"] = str(best.get("text") or "")[:20000]
    return best


def _resume_values_from_path_columns(cur, user_id: int, profile_id) -> list[dict]:
    found: list[dict] = []
    try:
        links = _owner_links(cur, column_names=_RESUME_PATH_COLUMNS)
    except Exception:
        log.exception("resume path lookup failed")
        return found
    for link in links:
        owner = str(link.get("owner_column") or "")
        key = {"user_id": user_id, "profile_id": profile_id, "candidate_id": user_id}.get(owner)
        if key is None:
            continue
        table = str(link.get("table_name") or "")
        if table in _CATALOG_SKIP_TABLES or table == JOBS_TABLE:
            continue
        column = str(link.get("value_column") or "")
        for row in _rows_for_owner(cur, table, column, owner, key):
            value = row.get("value")
            if value is None or value == "":
                continue
            hint = str(value) if isinstance(value, str) else f"{table}.{column}"
            found.append({"value": value, "hint": hint})
    return found


def _resume_values_from_blobs(cur, user_id: int, profile_id) -> list[dict]:
    found: list[dict] = []
    try:
        cur.execute(
            """
            SELECT c.TABLE_NAME AS table_name,
                   c.COLUMN_NAME AS value_column,
                   u.COLUMN_NAME AS owner_column
            FROM information_schema.COLUMNS c
            JOIN information_schema.COLUMNS u
              ON u.TABLE_SCHEMA = c.TABLE_SCHEMA
             AND u.TABLE_NAME = c.TABLE_NAME
             AND u.COLUMN_NAME IN ('user_id', 'profile_id')
            WHERE c.TABLE_SCHEMA = DATABASE()
              AND c.DATA_TYPE IN ('blob', 'mediumblob', 'longblob')
              AND (c.COLUMN_NAME LIKE %s OR c.COLUMN_NAME LIKE %s OR c.COLUMN_NAME LIKE %s)
            """,
            ("%resume%", "%file%", "%document%"),
        )
        blobs = list(cur.fetchall() or [])
    except Exception:
        log.exception("resume blob lookup failed")
        return found
    for link in blobs:
        owner = str(link.get("owner_column") or "")
        key = user_id if owner == "user_id" else profile_id
        if key is None:
            continue
        table = str(link.get("table_name") or "")
        column = str(link.get("value_column") or "")
        if not _IDENT_RE.fullmatch(table) or not _IDENT_RE.fullmatch(column) or not _IDENT_RE.fullmatch(owner):
            continue
        try:
            cur.execute(
                f"SELECT {_quote_ident(column)} AS value FROM {_quote_ident(table)} "
                f"WHERE {_quote_ident(owner)} = %s LIMIT 1",
                (key,),
            )
            row = cur.fetchone() or {}
        except Exception:
            continue
        value = row.get("value")
        if value:
            found.append({"value": value, "hint": f"{table}.{column}.pdf"})
    return found


def _resume_values_from_media(cur, user_id: int, profile_id) -> list[dict]:
    """Spatie/Laravel-style media rows linked to the user or profile."""
    found: list[dict] = []
    try:
        cur.execute(
            """
            SELECT TABLE_NAME AS table_name
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
              AND (
                TABLE_NAME IN ('media', 'files', 'uploads', 'documents', 'attachments', 'user_files', 'profile_files', 'resumes', 'user_resumes')
                OR TABLE_NAME LIKE %s
                OR TABLE_NAME LIKE %s
              )
            """,
            ("%resume%", "%cv%"),
        )
        tables = [str(row.get("table_name") or "") for row in cur.fetchall() or []]
    except Exception:
        return found
    for table in tables:
        if not _IDENT_RE.fullmatch(table):
            continue
        try:
            cur.execute(
                """
                SELECT COLUMN_NAME AS column_name
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                """,
                (table,),
            )
            columns = {str(row.get("column_name") or "") for row in cur.fetchall() or []}
        except Exception:
            continue
        path_col = next((name for name in ("file_name", "filename", "path", "file_path", "url", "name") if name in columns), None)
        if not path_col:
            continue
        where = []
        params: list = []
        if "model_id" in columns:
            where.append("model_id IN (%s, %s)")
            params.extend([user_id, profile_id if profile_id is not None else user_id])
        if "user_id" in columns:
            where.append("user_id = %s")
            params.append(user_id)
        if "profile_id" in columns and profile_id is not None:
            where.append("profile_id = %s")
            params.append(profile_id)
        if not where:
            continue
        selected = [f"{_quote_ident(path_col)} AS path_value"]
        if "id" in columns:
            selected.append("id")
        if "disk" in columns:
            selected.append("disk")
        if "collection_name" in columns:
            selected.append("collection_name")
        if "mime_type" in columns:
            selected.append("mime_type")
        sql = (
            f"SELECT {', '.join(selected)} FROM {_quote_ident(table)} "
            f"WHERE ({' OR '.join(where)}) LIMIT 20"
        )
        try:
            cur.execute(sql, params)
            rows = cur.fetchall() or []
        except Exception:
            continue
        for row in rows:
            collection = str(row.get("collection_name") or "").lower()
            mime = str(row.get("mime_type") or "").lower()
            path_value = str(row.get("path_value") or "").strip()
            if not path_value:
                continue
            if collection and not any(token in collection for token in ("resume", "cv", "document", "file", "default")):
                if "pdf" not in path_value.lower() and "word" not in mime and "pdf" not in mime:
                    continue
            if not any(path_value.lower().endswith(ext) for ext in (".pdf", ".docx", ".txt")) and "pdf" not in mime:
                if "id" in row and path_value:
                    pass
                else:
                    continue
            candidates = [path_value]
            media_id = row.get("id")
            disk = str(row.get("disk") or "public")
            if media_id is not None:
                candidates.extend(
                    [
                        f"{media_id}/{path_value}",
                        f"{disk}/{media_id}/{path_value}",
                        f"public/{media_id}/{path_value}",
                        f"storage/app/public/{media_id}/{path_value}",
                        f"storage/app/{disk}/{media_id}/{path_value}",
                    ]
                )
            for candidate in candidates:
                found.append({"value": candidate, "hint": path_value})
    return found


def _resume_payload_from_value(value, parse_resume_bytes, hint: str = "") -> dict | None:
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if len(cleaned) > 200 and _resume_section_count(cleaned) >= 2:
            return {"text": cleaned[:20000], "content": None, "filename": hint or "resume.txt", "media_type": "text/plain"}
        path = _resolve_resume_path(cleaned)
        if path is None:
            return None
        try:
            content = path.read_bytes()
            parsed = parse_resume_bytes(content, filename=path.name)
        except Exception:
            return None
        return {
            "text": str(parsed.get("raw_text") or "")[:20000],
            "content": content,
            "filename": path.name,
            "media_type": "application/pdf" if path.suffix.lower() == ".pdf" else (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if path.suffix.lower() == ".docx"
                else "text/plain"
            ),
        }
    if isinstance(value, (bytes, bytearray)) and len(value) > 100:
        filename = hint if str(hint).lower().endswith((".pdf", ".docx", ".txt")) else "resume.pdf"
        try:
            content = bytes(value)
            parsed = parse_resume_bytes(content, filename=filename)
        except Exception:
            return None
        return {
            "text": str(parsed.get("raw_text") or "")[:20000],
            "content": content,
            "filename": filename,
            "media_type": "application/pdf" if filename.lower().endswith(".pdf") else (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if filename.lower().endswith(".docx")
                else "application/octet-stream"
            ),
        }
    return None


def _resume_storage_roots() -> list[Path]:
    roots: list[Path] = []
    env_root = (os.getenv("RESUME_STORAGE_ROOT") or "").strip()
    if env_root:
        roots.append(Path(env_root))
    roots.extend(
        [
            _ROOT,
            _ROOT / "uploads",
            _ROOT / "storage",
            _ROOT / "storage" / "app",
            _ROOT / "storage" / "app" / "public",
            _ROOT / "public",
            _ROOT / "public" / "uploads",
            _ROOT / "media",
            _ROOT / "resumes",
            Path(r"C:\inetpub\wwwroot"),
            Path(r"C:\Projects"),
        ]
    )
    found = []
    seen = set()
    for root in roots:
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(root)
    return found


def _resolve_resume_path(raw: str) -> Path | None:
    value = (raw or "").strip().replace("\\", "/")
    if not value:
        return None
    if "://" in value:
        value = value.split("://", 1)[1]
        value = value.split("/", 1)[1] if "/" in value else value
    candidates = [Path(value)]
    if not Path(value).is_absolute():
        for root in _resume_storage_roots():
            candidates.append(root / value)
            candidates.append(root / value.lstrip("/"))
            name = Path(value).name
            if name and name != value:
                candidates.append(root / "resumes" / name)
                candidates.append(root / "uploads" / name)
                candidates.append(root / "storage" / "app" / "public" / name)
    for path in candidates:
        try:
            if path.suffix.lower() in {".pdf", ".docx", ".txt"} and path.is_file():
                return path
        except Exception:
            continue
    return None


def _text_from_resume_value(value, parse_resume_bytes) -> str:
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return ""
        # Some apps store the extracted resume text itself.
        if len(cleaned) > 200 and _resume_section_count(cleaned) >= 2:
            return cleaned[:20000]
        path = _resolve_resume_path(cleaned)
        if path is None:
            return ""
        try:
            parsed = parse_resume_bytes(path.read_bytes(), filename=path.name)
        except Exception:
            return ""
        return str(parsed.get("raw_text") or "")[:20000]
    if isinstance(value, (bytes, bytearray)) and len(value) > 100:
        try:
            parsed = parse_resume_bytes(bytes(value), filename="resume.pdf")
        except Exception:
            return ""
        return str(parsed.get("raw_text") or "")[:20000]
    return ""


def load_candidate(candidate_id: str) -> dict | None:
    if not mysql_configured():
        return None
    conn = get_conn()
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(CREATE_CANDIDATES_SQL)
        cur.execute(
            f"SELECT profile FROM {CANDIDATES_TABLE} WHERE candidate_id = %s",
            (candidate_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return _profile_from_cell(row.get("profile"))


_TERM_SKIP = {
    "skill",
    "skills",
    "certification",
    "certifications",
    "certificate",
    "certificates",
    "license",
    "licenses",
    "n/a",
    "na",
    "none",
    "null",
    "other",
    "others",
    "true",
    "false",
    "unknown",
    "yes",
    "no",
}
_CATALOG_SKIP_TABLES = {
    "jobs",
    "companies",
    "job_discovery_candidates",
    "job_discovery_scrape_runs",
    "job_discovery_everify_employers",
    "job_discovery_h1b_sponsors",
    "job_whatsapp_alerts",
}


def _clean_search_term(value) -> str | None:
    text = str(value or "").strip(" \t\r\n,;|/")
    if not text or len(text) < 2 or len(text) > 80 or len(text.split()) > 6:
        return None
    if text.lower() in _TERM_SKIP:
        return None
    return text


def _collect_term_values(raw, bucket: list[str]) -> None:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str) and raw.strip().startswith(("{", "[")):
        parsed = _profile_from_cell(raw) if raw.strip().startswith("{") else None
        if parsed is None and raw.strip().startswith("["):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
        if parsed is not None:
            _collect_term_values(parsed, bucket)
            return
    if isinstance(raw, list):
        for item in raw:
            _collect_term_values(item, bucket)
        return
    if isinstance(raw, dict):
        for key in ("name", "title", "label", "skill", "skill_name", "certification"):
            if raw.get(key):
                _collect_term_values(raw.get(key), bucket)
        return
    term = _clean_search_term(raw)
    if term:
        bucket.append(term)


def _unique_terms(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _title_search_term(value) -> str | None:
    from jobscraper.recommend import search_phrase

    return search_phrase(value)


def _append_related(chosen: list[str], limit: int) -> list[str]:
    """Add stored related-skill names when the list still has room."""
    from jobscraper.jd_to_cv import SKILL_RELATED

    seen = {item.lower() for item in chosen}
    extra: list[str] = []
    for term in chosen:
        for related in sorted(SKILL_RELATED.get(term.lower(), ())):
            phrase = _title_search_term(related)
            if not phrase or phrase.lower() in seen:
                continue
            seen.add(phrase.lower())
            extra.append(phrase)
            if len(chosen) + len(extra) >= limit:
                return chosen + extra
    return chosen + extra


def _balance_terms(
    certs: list[str],
    skills: list[str],
    limit: int,
    titles: list[str] | None = None,
) -> list[str]:
    titles = [item for item in (_title_search_term(value) for value in (titles or [])) if item]
    titles = _unique_terms(titles)
    used = {item.lower() for item in titles}
    certs = [item for item in _unique_terms(certs) if item.lower() not in used]
    used.update(item.lower() for item in certs)
    skills = [item for item in _unique_terms(skills) if item.lower() not in used]
    chosen: list[str] = []
    seen: set[str] = set()
    buckets = (titles, certs, skills)
    indexes = [0, 0, 0]

    def add(term: str) -> None:
        key = term.lower()
        if key in seen or len(chosen) >= limit:
            return
        seen.add(key)
        chosen.append(term)

    while len(chosen) < limit and any(indexes[i] < len(buckets[i]) for i in range(3)):
        for index, bucket in enumerate(buckets):
            if indexes[index] < len(bucket):
                add(bucket[indexes[index]])
                indexes[index] += 1
    return _append_related(chosen, limit)


def _catalog_columns(cur, skills: list[str], certs: list[str]) -> None:
    cur.execute(
        """
        SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND DATA_TYPE IN ('varchar', 'char', 'text', 'mediumtext', 'longtext', 'json')
          AND (
            TABLE_NAME LIKE %s OR COLUMN_NAME LIKE %s
            OR TABLE_NAME LIKE %s OR COLUMN_NAME LIKE %s
          )
          AND COLUMN_NAME NOT IN (
            'id', 'user_id', 'profile_id', 'skill_id', 'certification_id',
            'created_at', 'updated_at', 'password', 'email', 'url'
          )
        """,
        ("%skill%", "%skill%", "%certif%", "%certif%"),
    )
    for row in cur.fetchall() or []:
        table = str(row.get("table_name") or "")
        column = str(row.get("column_name") or "")
        if table in _CATALOG_SKIP_TABLES or not _IDENT_RE.fullmatch(table) or not _IDENT_RE.fullmatch(column):
            continue
        bucket = certs if "certif" in table.lower() or "certif" in column.lower() else skills
        try:
            cur.execute(
                f"SELECT DISTINCT {_quote_ident(column)} AS term FROM {_quote_ident(table)} "
                f"WHERE {_quote_ident(column)} IS NOT NULL LIMIT 80"
            )
            for item in cur.fetchall() or []:
                _collect_term_values(item.get("term"), bucket)
        except Exception:
            log.exception("catalog read failed on %s.%s", table, column)


def _catalog_job_skills(cur, skills: list[str]) -> None:
    try:
        cur.execute(
            f"SELECT skills FROM {JOBS_TABLE} "
            "WHERE skills IS NOT NULL AND JSON_LENGTH(skills) > 0 LIMIT 2000"
        )
        rows = cur.fetchall() or []
    except Exception:
        log.exception("job skill catalog read failed")
        return
    for row in rows:
        _collect_term_values(row.get("skills"), skills)


def _catalog_candidate_profiles(cur, skills: list[str], certs: list[str], titles: list[str]) -> None:
    try:
        cur.execute(f"SELECT profile FROM {CANDIDATES_TABLE} LIMIT 500")
        rows = cur.fetchall() or []
    except Exception:
        log.exception("candidate catalog read failed")
        return
    for row in rows:
        profile = _profile_from_cell(row.get("profile")) or {}
        _collect_term_values(profile.get("skills"), skills)
        _collect_term_values(profile.get("certifications"), certs)
        for job in profile.get("work_history") or []:
            if isinstance(job, dict):
                title = _title_search_term(job.get("job_title") or job.get("title"))
                if title:
                    titles.append(title)


def _catalog_experience_titles(cur, titles: list[str]) -> None:
    """Past job titles from every user's experience rows."""
    try:
        cur.execute(
            """
            SELECT DISTINCT c.TABLE_NAME AS table_name, c.COLUMN_NAME AS title_column
            FROM information_schema.COLUMNS c
            WHERE c.TABLE_SCHEMA = DATABASE()
              AND c.COLUMN_NAME IN ('job_title', 'title', 'position', 'role', 'role_title')
            """
        )
        columns = list(cur.fetchall() or [])
    except Exception:
        log.exception("experience title catalog failed")
        return
    seen: set[str] = set()
    for column in columns:
        table = str(column.get("table_name") or "")
        title_col = str(column.get("title_column") or "")
        if not _EXPERIENCE_TABLE.search(table) or table in seen:
            continue
        if not _IDENT_RE.fullmatch(table) or not _IDENT_RE.fullmatch(title_col):
            continue
        seen.add(table)
        try:
            cur.execute(
                f"SELECT DISTINCT {_quote_ident(title_col)} AS title FROM {_quote_ident(table)} "
                f"WHERE {_quote_ident(title_col)} IS NOT NULL LIMIT 40"
            )
            for row in cur.fetchall() or []:
                title = _title_search_term(row.get("title"))
                if title:
                    titles.append(title)
        except Exception:
            log.exception("experience title read failed on %s", table)


def _catalog_posting_terms(cur, skills: list[str], certs: list[str]) -> None:
    """Read skills and certifications written inside stored postings."""
    from jobscraper.candidates import certifications_from_profile
    from jobscraper.resume_parser import SKILL_CANONICAL, SKILL_PATTERN

    try:
        cur.execute(
            f"SELECT title, LEFT(description, 1500) AS description FROM {JOBS_TABLE} "
            "WHERE CHAR_LENGTH(description) > 40 LIMIT 1500"
        )
        rows = cur.fetchall() or []
    except Exception:
        log.exception("posting catalog read failed")
        return
    for row in rows:
        blob = f"{row.get('title') or ''}\n{row.get('description') or ''}"
        for match in SKILL_PATTERN.findall(blob):
            skills.append(SKILL_CANONICAL[match.lower()])
        certs.extend(certifications_from_profile({"resume_text": blob}))


def _recommendation_profile_terms() -> tuple[list[str], list[str], list[str]]:
    """Titles, skills, and certifications each user is actually scored against.

    These are searched before the general job-skill catalog, so a user with no
    50+ match still gets their own roles scraped.
    """
    from jobscraper.candidates import (
        certifications_from_profile,
        experience_titles_from_profile,
        skills_from_profile,
    )
    from jobscraper.recommend import search_phrase

    titles: list[str] = []
    skills: list[str] = []
    certs: list[str] = []
    if not mysql_configured():
        return titles, skills, certs
    user_ids: list[str] = []
    profiles: list[dict] = []
    try:
        conn = get_conn()
        if conn is None:
            return titles, skills, certs
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE 'users'")
            if cur.fetchone():
                cur.execute("SELECT id FROM users")
                user_ids = [str(row["id"]) for row in cur.fetchall() or [] if row.get("id") is not None]
            cur.execute(f"SHOW TABLES LIKE '{CANDIDATES_TABLE}'")
            if cur.fetchone():
                cur.execute(f"SELECT profile FROM {CANDIDATES_TABLE} LIMIT 500")
                for row in cur.fetchall() or []:
                    profile = _profile_from_cell(row.get("profile"))
                    if profile:
                        profiles.append(profile)
    except Exception:
        log.exception("recommendation profile terms failed")
        close_conn()
        return titles, skills, certs
    for user_id in user_ids:
        try:
            profile = load_app_user(user_id)
        except Exception:
            log.exception("recommendation terms skipped user %s", user_id)
            continue
        if profile:
            profiles.append(profile)
    for profile in profiles:
        role_titles = experience_titles_from_profile(profile)
        role_skills = skills_from_profile(profile)
        titles.extend(role_titles)
        skills.extend(role_skills)
        certs.extend(certifications_from_profile(profile))
        if role_titles and role_skills:
            focused = search_phrase(f"{role_titles[0]} {' '.join(role_skills[:3])}")
            if focused:
                titles.append(focused)
    return titles, skills, certs


def _merge_search_terms(*groups: list[str], limit: int) -> list[str]:
    chosen: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for term in group:
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            chosen.append(term)
            if len(chosen) >= limit:
                return chosen
    return chosen


def catalog_search_terms(limit: int = 40) -> list[str]:
    """Skills, certifications, and past job titles already stored in this database.

    Each user's own titles and skills are searched first, so the boards are asked
    for roles that can clear the recommendation score. The general catalog fills
    any slots that are left.
    """
    if not mysql_configured():
        return []
    try:
        cap = max(1, min(int(limit or 40), 120))
    except (TypeError, ValueError):
        cap = 40
    user_titles, user_skills, user_certs = _recommendation_profile_terms()
    priority = _balance_terms(user_certs, user_skills, cap, user_titles)
    skills: list[str] = []
    certs: list[str] = []
    titles: list[str] = []
    try:
        conn = get_conn()
        if conn is None:
            return priority
        with conn.cursor() as cur:
            _catalog_columns(cur, skills, certs)
            _catalog_job_skills(cur, skills)
            _catalog_candidate_profiles(cur, skills, certs, titles)
            _catalog_experience_titles(cur, titles)
            if len(_unique_terms(skills)) + len(_unique_terms(certs)) + len(_unique_terms(titles)) < 8:
                _catalog_posting_terms(cur, skills, certs)
    except Exception:
        log.exception("catalog search terms failed")
        close_conn()
        return priority
    filler = _balance_terms(certs, skills, cap, titles) if len(priority) < cap else []
    terms = _merge_search_terms(priority, filler, limit=cap)
    log.info(
        "Database search terms user_titles=%s user_skills=%s skills=%s certifications=%s titles=%s using=%s",
        len(set(user_titles)),
        len(set(user_skills)),
        len(set(skills)),
        len(set(certs)),
        len(set(titles)),
        len(terms),
    )
    return terms


def _scrape_progress_path() -> Path:
    return _ROOT / "data" / "scrape_progress.json"


def reset_scrape_progress() -> None:
    path = _scrape_progress_path()
    path.parent.mkdir(exist_ok=True)
    path.write_text('{"saved": 0, "new": 0, "duplicates": 0}', encoding="utf-8")


def note_scrape_progress(
    saved: int,
    *,
    new: int | None = None,
    duplicates: int | None = None,
) -> None:
    path = _scrape_progress_path()
    path.parent.mkdir(exist_ok=True)
    current = read_scrape_progress_stats()
    payload = {
        "saved": int(saved),
        "new": int(current["new"] if new is None else new),
        "duplicates": int(current["duplicates"] if duplicates is None else duplicates),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def read_scrape_progress() -> int:
    return int(read_scrape_progress_stats()["saved"])


def read_scrape_progress_stats() -> dict:
    empty = {"saved": 0, "new": 0, "duplicates": 0}
    try:
        payload = json.loads(_scrape_progress_path().read_text(encoding="utf-8"))
        return {
            "saved": max(0, int(payload.get("saved") or 0)),
            "new": max(0, int(payload.get("new") or 0)),
            "duplicates": max(0, int(payload.get("duplicates") or 0)),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return empty


def close_abandoned_scrape_runs(except_id: int | None = None) -> int:
    """Mark leftover running rows finished.

    finish_scrape_run only runs if this process is still alive at the end.
    An IIS recycle or a restart leaves the row on running forever.
    """
    if not mysql_configured():
        return 0
    try:
        conn = get_conn()
        if conn is None:
            return 0
        with conn.cursor() as cur:
            _ensure_run_columns(cur)
            sql = (
                f"UPDATE {RUNS_TABLE} SET finished_at = UTC_TIMESTAMP(), status = 'stopped', "
                "error = %s WHERE status = 'running' AND finished_at IS NULL"
            )
            params: list = [
                "Scrape stopped before it finished. The API restarted or the process was killed."
            ]
            if except_id:
                sql += " AND id <> %s"
                params.append(int(except_id))
            cur.execute(sql, tuple(params))
            return int(cur.rowcount or 0)
    except Exception:
        log.exception("Could not close abandoned scrape runs")
        close_conn()
        return 0


def start_scrape_run(keywords: str, state: str, platform: str) -> int | None:
    if not mysql_configured():
        return None
    try:
        close_abandoned_scrape_runs()
        conn = get_conn()
        if conn is None:
            return None
        with conn.cursor() as cur:
            _ensure_run_columns(cur)
            cur.execute(
                f"INSERT INTO {RUNS_TABLE} (started_at, keywords, state, platform, status) "
                "VALUES (UTC_TIMESTAMP(), %s, %s, %s, 'running')",
                (keywords[:512] if keywords else None, state or None, platform or None),
            )
            return int(cur.lastrowid)
    except Exception:
        log.exception("Could not record scrape start")
        close_conn()
        return None


def finish_scrape_run(
    run_id: int | None,
    *,
    job_count_value: int,
    new_count: int = 0,
    duplicate_count: int = 0,
    error: str | None = None,
) -> None:
    if not run_id or not mysql_configured():
        return
    status = "error" if error else "ok"
    job_count_value = max(0, int(job_count_value or 0))
    new_count = max(0, int(new_count or 0))
    duplicate_count = max(0, int(duplicate_count or 0))
    if new_count + duplicate_count == 0 and job_count_value:
        # Older callers only pass the total; keep columns consistent.
        new_count = job_count_value
    try:
        conn = get_conn()
        if conn is None:
            return
        with conn.cursor() as cur:
            _ensure_run_columns(cur)
            cur.execute(
                f"UPDATE {RUNS_TABLE} SET finished_at = UTC_TIMESTAMP(), job_count = %s, "
                "new_count = %s, duplicate_count = %s, status = %s, error = %s WHERE id = %s",
                (
                    job_count_value,
                    new_count,
                    duplicate_count,
                    status,
                    (error or "")[:4000] or None,
                    run_id,
                ),
            )
    except Exception:
        log.exception("Could not record scrape finish")
        close_conn()


def scrape_run_save_stats(run_id: int | None = None) -> dict | None:
    """Rows this scrape wrote, from first_seen_at / last_seen_at.

    saved = new inserts + rows that were already in the table.
    The progress file only counts listings the spider handled, so it can
    climb while both of those stay at 0.
    """
    if not mysql_configured():
        return None
    try:
        conn = get_conn()
        if conn is None:
            return None
        with conn.cursor() as cur:
            if run_id:
                cur.execute(
                    f"SELECT started_at FROM {RUNS_TABLE} WHERE id = %s",
                    (int(run_id),),
                )
            else:
                cur.execute(
                    f"SELECT started_at FROM {RUNS_TABLE} "
                    "WHERE status = 'running' AND finished_at IS NULL "
                    "AND started_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY "
                    "ORDER BY id DESC LIMIT 1"
                )
            run = cur.fetchone()
            started = run.get("started_at") if run else None
            if not started:
                return None
            cur.execute(
                f"SELECT "
                "COALESCE(SUM(CASE WHEN first_seen_at >= %s THEN 1 ELSE 0 END), 0) AS new_count, "
                "COALESCE(SUM(CASE WHEN last_seen_at >= %s "
                "AND (first_seen_at IS NULL OR first_seen_at < %s) THEN 1 ELSE 0 END), 0) AS duplicate_count "
                f"FROM {JOBS_TABLE} WHERE last_seen_at >= %s",
                (started, started, started, started),
            )
            row = cur.fetchone() or {}
        new_count = int(row.get("new_count") or 0)
        duplicate_count = int(row.get("duplicate_count") or 0)
        return {
            "saved": new_count + duplicate_count,
            "new": new_count,
            "duplicates": duplicate_count,
        }
    except Exception:
        log.exception("Could not count new vs duplicate jobs for this scrape")
        close_conn()
        return None


def last_scrape_meta() -> dict:
    empty = {
        "scraped_at": None,
        "age_seconds": None,
        "job_count": 0,
        "new_count": 0,
        "duplicate_count": 0,
        "status": None,
    }
    if not mysql_configured():
        return empty
    try:
        conn = get_conn()
        if conn is None:
            return empty
        with conn.cursor() as cur:
            _ensure_run_columns(cur)
            cur.execute(
                f"SELECT finished_at, job_count, new_count, duplicate_count, status FROM {RUNS_TABLE} "
                "WHERE status = 'ok' AND finished_at IS NOT NULL "
                "ORDER BY finished_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        if not row or not row.get("finished_at"):
            return {**empty, "job_count": job_count()}
        finished = row["finished_at"]
        if isinstance(finished, datetime):
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            scraped_at = finished.astimezone(timezone.utc).isoformat()
            age = int((datetime.now(timezone.utc) - finished.astimezone(timezone.utc)).total_seconds())
        else:
            scraped_at = str(finished)
            age = None
        return {
            "scraped_at": scraped_at,
            "age_seconds": age,
            "job_count": int(row.get("job_count") or 0),
            "new_count": int(row.get("new_count") or 0),
            "duplicate_count": int(row.get("duplicate_count") or 0),
            "status": row.get("status"),
        }
    except Exception:
        close_conn()
        return empty

def health() -> dict:
    """Liveness only. A locked jobs table must not hang this check into an IIS 502."""
    cfg = mysql_config()
    if not cfg:
        return {"configured": False, "ok": False, "jobs": 0}
    conn = None
    try:
        quick = dict(cfg)
        quick["connect_timeout"] = 3
        quick["read_timeout"] = 3
        quick["write_timeout"] = 3
        conn = _connect(quick)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
        return {
            "configured": True,
            "ok": True,
            "host": cfg["host"],
            "database": cfg["database"],
            "jobs_table": JOBS_TABLE,
        }
    except Exception as exc:
        return {"configured": True, "ok": False, "jobs": 0, "error": str(exc)[:300]}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def import_json_if_empty(path: Path) -> int:
    if not mysql_configured() or not path.exists() or job_count() > 0:
        return 0
    try:
        text = path.read_text(encoding="utf-8").replace("\x00", "").strip()
        if not text:
            return 0
        payload, _ = json.JSONDecoder().raw_decode(text)
    except (OSError, json.JSONDecodeError):
        return 0
    jobs = payload if isinstance(payload, list) else []
    stats = upsert_jobs(jobs, notify=False)
    if stats.saved:
        log.info(
            "Imported %s jobs from %s (new=%s duplicates=%s)",
            stats.saved,
            path,
            stats.new,
            stats.duplicates,
        )
    return stats.saved
