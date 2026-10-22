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
  raw_description LONGTEXT NOT NULL,
  job_type VARCHAR(32) NOT NULL DEFAULT 'fulltime',
  work_model VARCHAR(16) NOT NULL DEFAULT 'unknown',
  experience_level VARCHAR(32) NOT NULL DEFAULT 'unknown',
  years_experience SMALLINT NULL,
  visa_sponsorship VARCHAR(16) NOT NULL DEFAULT 'unknown',
  clearance_required VARCHAR(16) NOT NULL DEFAULT 'unknown',
  saudi_national_required VARCHAR(16) NOT NULL DEFAULT 'no',
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
  keywords VARCHAR(512) NULL,
  state VARCHAR(128) NULL,
  platform VARCHAR(128) NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'running',
  error TEXT NULL,
  PRIMARY KEY (id),
  KEY idx_jd_scrape_runs_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

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
    "description",
    "raw_description",
    "posted_at",
    "is_active",
    "job_type",
    "work_model",
    "experience_level",
    "years_experience",
    "visa_sponsorship",
    "clearance_required",
    "saudi_national_required",
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
        default = _sql_default(str(_row_value(column, "data_type") or ""), str(_row_value(column, "column_type") or ""))
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
  platform, days_ago, url, description, raw_description, posted_at, is_active,
  job_type, work_model, experience_level, years_experience, visa_sponsorship, clearance_required,
  saudi_national_required, listing_inferred, first_seen_at, last_seen_at, created_at, updated_at
) VALUES (
  %s, %s, %s, %s, %s, %s, %s, %s,
  %s, %s, %s, %s, %s, %s, 1,
  %s, %s, %s, %s, %s, %s,
  %s, 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
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
  url = new.url,
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
  posted_at = new.posted_at,
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
        ("listing_inferred", "TINYINT(1) NOT NULL DEFAULT 0"),
    ):
        cur.execute(f"SHOW COLUMNS FROM {JOBS_TABLE} LIKE '{name}'")
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE {JOBS_TABLE} ADD COLUMN {name} {ddl}")
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
    _default_omitted_columns(cur)


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
            _ensure_job_columns(cur)
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


def _startup_maintenance() -> None:
    """Backfill and cleanup after uvicorn is already listening on port 9001."""
    try:
        backfill_listing_fields()
        clear_copied_api_raw_descriptions()
    except Exception:
        log.exception("Startup maintenance failed")
    start_dice_description_backfill()


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


_DICE_BACKFILL_STARTED = False


def start_dice_description_backfill() -> None:
    """Fill title-only descriptions from each board's own posting page."""
    global _DICE_BACKFILL_STARTED
    if _DICE_BACKFILL_STARTED or not mysql_configured():
        return
    _DICE_BACKFILL_STARTED = True
    threading.Thread(
        target=_fill_short_dice_descriptions,
        name="posting-descriptions",
        daemon=True,
    ).start()


def _fill_short_dice_descriptions() -> None:
    import time
    import urllib.error

    from jobscraper.agent_reach import fetch_posting_paragraph

    conn = get_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, url, title, platform FROM {JOBS_TABLE} "
                f"WHERE platform IN ('Indeed', 'LinkedIn', 'Bayt', 'Glassdoor', 'Google Jobs') "
                f"AND CHAR_LENGTH(description) < 120 AND TRIM(url) <> ''"
            )
            rows = list(cur.fetchall() or [])
    except Exception:
        close_conn()
        log.exception("Could not list short job descriptions")
        return
    if not rows:
        return
    log.info("Filling %s short descriptions from posting pages", len(rows))
    filled = 0
    failures = 0
    for row in rows:
        time.sleep(0.25)
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        try:
            text = fetch_posting_paragraph(row.get("platform") or "", url)
        except urllib.error.HTTPError as exc:
            if exc.code not in {404, 410}:
                failures += 1
            if failures >= 20:
                log.warning("Stopped description fill after repeated request failures")
                break
            continue
        except Exception:
            failures += 1
            if failures >= 20:
                log.warning("Stopped description fill after repeated request failures")
                break
            continue
        failures = 0
        if not text:
            continue
        payload = json.dumps({"jobDescription": text}, ensure_ascii=False)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {JOBS_TABLE} SET description = %s, "
                    f"raw_description = IF("
                    f"JSON_VALID(raw_description) AND CHAR_LENGTH(raw_description) > 2 "
                    f"AND CAST(raw_description AS CHAR) NOT LIKE %s, "
                    f"JSON_MERGE_PATCH(raw_description, %s), "
                    f"IF(CHAR_LENGTH(raw_description) > 2 AND CAST(raw_description AS CHAR) NOT LIKE %s, "
                    f"raw_description, %s)) "
                    f"WHERE id = %s AND CHAR_LENGTH(description) < CHAR_LENGTH(%s)",
                    (text, '%"api_id"%', payload, '%"api_id"%', payload, row["id"], text),
                )
            filled += 1
        except Exception:
            close_conn()
            conn = get_conn()
            if conn is None:
                break
            log.exception("Job description update failed")
    log.info("Filled %s descriptions from posting pages", filled)


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
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "nat", "nan"}:
        return None
    if text.isdigit():
        try:
            stamp = int(text)
            if stamp > 10_000_000_000:
                stamp //= 1000
            return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return None
    cleaned = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return text[:10] + " 00:00:00"
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


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
    "description",
    "job_type",
    "work_model",
    "experience_level",
    "years_experience",
    "visa_sponsorship",
    "clearance_required",
    "saudi_national_required",
    "posted_at",
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
    skills = _as_skills_json(job.get("skills"))
    description = str(job.get("description") or job.get("search_text") or "").strip()
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
        "url": _clip(job.get("url"), 1000),
        "description": description,
        "raw_description": _raw_description_json(job),
        "posted_at": _posted_mysql(job.get("posted_at")),
        **_listing_fields(job, description),
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


def upsert_jobs(jobs: list[dict]) -> int:
    if not jobs or not mysql_configured():
        return 0
    rows = [row for row in (_row_from_item(job) for job in jobs) if row]
    if not rows:
        return 0
    unique = {}
    for row in rows:
        unique[row["source_id"]] = row
    by_url = {}
    for row in unique.values():
        by_url[row["url"] or f"id:{row['source_id']}"] = row
    rows = list(by_url.values())
    conn = get_conn()
    if conn is None:
        return 0
    try:
        with _LOCK:
            with conn.cursor() as cur:
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
                        row["job_type"] if row["job_type"] in {"fulltime", "parttime", "contract", "internship"} else "fulltime",
                        row["work_model"],
                        row["experience_level"],
                        row["years_experience"],
                        row["visa_sponsorship"],
                        row["clearance_required"],
                        row["saudi_national_required"],
                    )
                    for row in rows
                ]
                _write_row(cur, _upsert_sql(cur), payload, table=JOBS_TABLE, many=True)
        return len(rows)
    except Exception:
        close_conn()
        log.exception("MySQL upsert failed")
        raise


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


def _job_payload(row: dict, *, include_raw: bool) -> dict:
    posted = row.get("posted_at")
    if isinstance(posted, datetime):
        posted = posted.strftime("%Y-%m-%d %H:%M:%S")
    days = _days_ago_from_iso(posted, int(row.get("days_ago") or 0))
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
        "description": row.get("description") or "",
        "job_type": row.get("job_type") or "fulltime",
        "work_model": row.get("work_model") or "unknown",
        "experience_level": row.get("experience_level") or "unknown",
        "years_experience": _years_value(row.get("years_experience")),
        "visa_sponsorship": row.get("visa_sponsorship") or "unknown",
        "clearance_required": row.get("clearance_required") or "unknown",
        "saudi_national_required": "yes" if row.get("saudi_national_required") == "yes" else "no",
        "posted_at": posted,
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


def load_jobs() -> list[dict]:
    load_jobs.last_error = None
    if not mysql_configured():
        return []
    try:
        conn = get_conn()
        if conn is None:
            return []
        with conn.cursor() as cur:
            # raw_description stays off this query. It is a LONGTEXT, and loading it
            # for every row stalls MySQL. Description here is only a short preview;
            # the jobs API replaces it with the full column for the rows it returns.
            cur.execute(
                f"SELECT id AS api_id, "
                f"COALESCE(NULLIF(source_id, ''), public_id, CAST(id AS CHAR)) AS id, "
                f"title, company, skills, state, location, platform, "
                f"days_ago, url, LEFT(description, 800) AS description, "
                f"job_type, work_model, experience_level, "
                f"years_experience, visa_sponsorship, clearance_required, saudi_national_required, "
                f"posted_at "
                f"FROM {JOBS_TABLE} ORDER BY last_seen_at DESC"
            )
            rows = cur.fetchall() or []
        load_jobs.last_error = None
        return [_job_from_row(row) for row in rows]
    except Exception as exc:
        load_jobs.last_error = str(exc)[:300]
        log.exception("MySQL load_jobs failed")
        close_conn()
        return []


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
            skills = _user_skill_values(cur, user_id, profile_row.get("profile_id"))
            skills.extend(_linked_names(cur, user_id, profile_row.get("profile_id")))
            extra_text = _user_text(cur, user_id, profile_row.get("profile_id"))
            extra_text = "\n".join(
                part for part in (extra_text, _resume_text_from_stored_files(cur, user_id, profile_row.get("profile_id"))) if part
            )
    except Exception:
        log.exception("load_app_user failed for %s", candidate_id)
        close_conn()
        raise
    summary = " ".join(
        part
        for part in (
            profile_row.get("headline") or "",
            profile_row.get("professional_summary") or "",
            extra_text,
        )
        if part
    )
    return {
        "first_name": user.get("first_name") or "",
        "last_name": user.get("last_name") or "",
        "email": user.get("email") or "",
        "phone": profile_row.get("phone") or "",
        "city": profile_row.get("city") or "",
        "linkedin_url": profile_row.get("linkedin_url") or "",
        "skills": skills,
        "resume_text": summary,
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
        ("%skill%", "%skill%"),
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
            if isinstance(value, str) and value.strip().startswith("["):
                parsed = _profile_from_cell(value)
                if isinstance(parsed, list):
                    found.extend(str(item) for item in parsed)
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


def _linked_names(cur, user_id: int, profile_id) -> list[str]:
    """Resolve skill_id / target_role_id links to their display names."""
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
        if not any(token in fk for token in ("skill", "role")):
            continue
        table = str(link.get("table_name") or "")
        for catalog in catalogs:
            cat_table = str(catalog.get("table_name") or "")
            cat_name = str(catalog.get("name_column") or "")
            if cat_table == table or not _IDENT_RE.fullmatch(cat_table) or not _IDENT_RE.fullmatch(cat_name):
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
    "content",
    "body",
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


def _resume_text_from_stored_files(cur, user_id: int, profile_id) -> str:
    """Parse a resume file or blob saved on the user or profile row."""
    try:
        from jobscraper.resume_parser import parse_resume_bytes
    except Exception:
        return ""
    texts: list[str] = []
    try:
        links = _owner_links(
            cur,
            column_names=("file", "file_path", "path", "resume", "document", "upload", "attachment"),
        )
    except Exception:
        log.exception("resume path lookup failed")
        links = []
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
            text = _text_from_resume_value(row.get("value"), parse_resume_bytes)
            if text:
                texts.append(text)
    if texts:
        return "\n".join(texts)[:20000]
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
        return ""
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
        text = _text_from_resume_value(row.get("value"), parse_resume_bytes)
        if text:
            texts.append(text)
    return "\n".join(texts)[:20000]


def _text_from_resume_value(value, parse_resume_bytes) -> str:
    if isinstance(value, str):
        path = Path(value.strip())
        if path.suffix.lower() in {".pdf", ".docx", ".txt"} and path.is_file():
            try:
                parsed = parse_resume_bytes(path.read_bytes(), filename=path.name)
            except Exception:
                return ""
            return str(parsed.get("raw_text") or "")[:20000]
        return ""
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


def start_scrape_run(keywords: str, state: str, platform: str) -> int | None:
    if not mysql_configured():
        return None
    try:
        conn = get_conn()
        if conn is None:
            return None
        with conn.cursor() as cur:
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


def finish_scrape_run(run_id: int | None, *, job_count_value: int, error: str | None = None) -> None:
    if not run_id or not mysql_configured():
        return
    status = "error" if error else "ok"
    try:
        conn = get_conn()
        if conn is None:
            return
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {RUNS_TABLE} SET finished_at = UTC_TIMESTAMP(), job_count = %s, "
                "status = %s, error = %s WHERE id = %s",
                (job_count_value, status, (error or "")[:4000] or None, run_id),
            )
    except Exception:
        log.exception("Could not record scrape finish")
        close_conn()


def last_scrape_meta() -> dict:
    empty = {"scraped_at": None, "age_seconds": None, "job_count": 0, "status": None}
    if not mysql_configured():
        return empty
    try:
        conn = get_conn()
        if conn is None:
            return empty
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT finished_at, job_count, status FROM {RUNS_TABLE} "
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
    saved = upsert_jobs(jobs)
    if saved:
        log.info("Imported %s jobs from %s", saved, path)
    return saved
