CREATE DATABASE IF NOT EXISTS ksajob
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE ksajob;

CREATE TABLE IF NOT EXISTS jobs (
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS companies (
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS job_discovery_candidates (
  candidate_id VARCHAR(64) NOT NULL,
  profile JSON NOT NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (candidate_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS job_discovery_scrape_runs (
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
  KEY idx_scrape_runs_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
