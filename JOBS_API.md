# Jobs Listings + Skill Recommend API

Give this document to the frontend or backend developer who will call the API.

**Production base URL:** `https://agentsksa.atozeesolutions.com`

Local example: `http://127.0.0.1:9001`

Interactive try-out: [https://agentsksa.atozeesolutions.com/docs](https://agentsksa.atozeesolutions.com/docs)

Related APIs: `RESUME_API.md`, `ATS_API.md`, `CV_API.md`, `CONVERT_API.md`

## What it does

1. **Listings** — return US job listings (title, company, skills, description, job_type, location, board, apply URL)
2. **Scrape** — refresh from public job boards and **upsert into MySQL** (JSON file is a backup)
3. **Recommend** — rank those listings against candidate skills (or a resume)
4. **Schedule** — scrape runs **twice a day** (default 08:00 and 20:00 Asia/Riyadh) across the full role list, aiming for about 5000 jobs

Listings come from MySQL when `.env` has `MYSQL_*` set, otherwise from the last `data/jobs.json` scrape. If `count` is `0`, scrape first (or call recommend with `refresh= true`).

US jobs only. Remote + named US states. Foreign listings are dropped.

## Endpoints

| Method | Path | Use |
| --- | --- | --- |
| GET | `/api/health` | Liveness (`jobs_list` and `jobs_recommend`) |
| GET | `/api/filters` | States, boards, and day options for dropdowns |
| GET | `/api/jobs` | Cached listings, filterable |
| POST | `/api/scrape` | Scrape boards, save to MySQL, return this run |
| GET | `/api/scrape/status` | MySQL + last scrape + next schedule |
| GET | `/api/recommend` | Contract |
| GET | `/api/recommend/{candidate_id}` | Rank listings for the profile saved at registration |
| PUT | `/api/candidates/{candidate_id}` | Save the registration profile under that API id |
| POST | `/api/candidates/{candidate_id}/resume` | Attach the master resume and extract skills |
| POST | `/api/recommend` | Rank listings from a candidate id, skill list, or resume |
| POST | `/api/recommend/file` | Upload a resume file, then rank listings |

Scrape can take **several minutes** on a full run. Recommend without `refresh` is fast (cache only).

## List cached jobs

```bash
curl "{BASE_URL}/api/jobs?keywords=python,fastapi&state=All&platform=All&days=7&limit=20&page=1"
```

JavaScript:

```js
const params = new URLSearchParams({
  keywords: "python,fastapi",
  state: "All",          // All | Remote | California | ...
  platform: "All",       // All | LinkedIn | Remotive | ...
  days: "30",
  city: "",
  limit: "20",
  page: "1",
});
const res = await fetch(`${BASE_URL}/api/jobs?${params}`);
const data = await res.json();
if (!res.ok) throw new Error(data.detail || "Jobs list failed");
// data.jobs, data.pagination.page, data.pagination.total_pages, data.pagination.has_next
```

Query flags:

| Query | Default | Notes |
| --- | --- | --- |
| `keywords` | `""` | Comma-separated. **All** tokens must appear in title, company, location, or skills |
| `state` | `All` | `All`, `Remote`, or a US state name |
| `platform` | `All` | Exact board name from `/api/filters` |
| `days` | `30` | `1`, `3`, `7`, or `30` |
| `city` | `""` | Optional city substring |
| `work_model` | `""` | Comma-separated: `remote`, `hybrid`, `onsite` |
| `job_type` | `""` | Comma-separated: `fulltime`, `contract`, `parttime`, `internship` |
| `experience_level` | `""` | Comma-separated: `Intern/New Grad`, `Entry Level`, `Mid Level`, `Senior Level`, `Lead/Staff`, `Director/Executive` |
| `visa_sponsorship` | `""` | `yes` or `no`. Iqama or work-visa sponsorship inferred from the posting. `unknown` means the posting does not say |
| `clearance_required` | `""` | `yes` or `no` |
| `us_citizen_required` | `""` | `yes` or `no`. `yes` = posting demands US citizenship |
| `years` | omit | Candidate years. Drops jobs that require more. Jobs with no stated years stay |
| `e_verified` | `""` | `true`, `false`, or `unknown` |
| `limit` | omit | Page size, max **200**. Omit to return every match, unless `page` is set |
| `page` | omit | 1-based page. Overrides `offset`. With no `limit`, page size is **20** |
| `offset` | `0` | Skip this many matches. Ignored when `page` is set |

Next page: raise `page` by 1, or add `limit` to `offset`. `pagination.has_next` is false on the last page.

## Refresh listings (scrape)

```bash
curl -X POST "{BASE_URL}/api/scrape?keywords=&state=All&platform=All&days=7"
```

The response is immediate (`202`, `"started": true`). The scrape keeps running after that, so the IIS proxy does not return 502 while boards are still loading. Poll until `running` is false:

```bash
curl "{BASE_URL}/api/scrape/status"
```

`last_scrape.db_total` is the number of rows in `ksajob.jobs`. `409` if another scrape is already running.

This run is upserted into MySQL database `ksajob`, table `jobs`, when `.env` has `MYSQL_DATABASE=ksajob`. Duplicate URL/id rows are updated (`last_seen_at`), not inserted twice.

Omit `keywords` (or send empty) to scrape **all fields**. If you pass `python,react`, a job matching **either** token is kept.

A background scheduler also calls the same scrape at **08:00 and 20:00** `Asia/Riyadh` (override with `SCRAPE_HOURS` / `SCRAPE_TZ` in `.env`). It aims for **5000** jobs (`SCRAPE_TARGET`) and, when `SCRAPE_KEYWORDS` is empty, searches the full role list at up to 100 listings per board. That run takes one to two hours. Comma-separated scrape keywords match **any** token (OR).

Use this when the cache is empty or stale. Then call `/api/jobs` or `/api/recommend`.

A second feed, Dev Global Jobs, runs every minute (`GLOBALJOBS_INTERVAL_MINUTES`) for `country=sa`. Each listing is loaded by id so the full description is stored. The job URL stays on devglobaljobs.com. Their limit is 120 requests per minute per IP. This feed uses at most 100. Show a visible "Powered by Dev Global Jobs" link wherever those rows are displayed.

## Visa and Saudization

Each listing includes `visa_sponsorship` (`yes`, `no`, or `unknown`) and `saudi_national_required` (`yes` or `no`). These are read from the posting. There is no E-Verify or H-1B field.

## Recommend by candidate API id

Registration creates the candidate in ksajob. Pass that same id here. Do not send the password or EEO answers (gender, race, veteran, disability). Those are ignored and are not used to rank jobs.

Save the profile once, when the user finishes registration:

```bash
curl -X PUT "{BASE_URL}/api/candidates/123" ^
  -H "Content-Type: application/json" ^
  -d "{\"first_name\":\"Zain\",\"last_name\":\"Mohammed\",\"email\":\"name@company.com\",\"phone_code\":\"+1\",\"phone\":\"5552345678\",\"city\":\"San Francisco\",\"state\":\"CA\",\"zip_code\":\"94105\",\"country\":\"United States\",\"linkedin_url\":\"https://linkedin.com/in/username\",\"github_url\":\"https://github.com/username\",\"work_history\":[{\"company\":\"Google\",\"job_title\":\"Software Engineer\",\"start_date\":\"2022-01\",\"end_date\":\"Present\",\"description\":\"Python, FastAPI, AWS\"}],\"education\":[{\"school\":\"Stanford University\",\"degree\":\"B.S.\",\"field_of_study\":\"Computer Science\",\"graduation_year\":\"2021\"}],\"authorized_to_work\":\"yes\",\"need_visa_sponsorship\":\"no\",\"willing_to_relocate\":\"no\",\"open_to_remote\":\"yes\",\"desired_salary\":\"130000\"}"
```

`state` can be `CA` or `California`. Skills are read from `skills`, `resume_text`, work history, and education.

Master resume from step 4:

```bash
curl -X POST "{BASE_URL}/api/candidates/123/resume" -F "file=@C:\path\to\resume.pdf"
```

Then recommend with only the id:

```bash
curl "{BASE_URL}/api/recommend/123?limit=20"
```

```js
const res = await fetch(`${BASE_URL}/api/recommend/${candidateId}?limit=20`);
const data = await res.json();
if (!res.ok) throw new Error(data.detail || "Recommend failed");
// data.candidate_id, data.skills, data.filters, data.jobs
// data.jobs[0].job_id is ezyjob.jobs.id — use this in the app
// data.jobs[0].id is the board source id
```

`POST /api/recommend` with `{ "candidate_id": "123" }` does the same thing. Extra `skills` on that body replace the saved skill list for this call only.

Preferences from registration:

| Field | Effect |
| --- | --- |
| `willing_to_relocate` `no` | Keep the candidate's state, plus remote jobs when `open_to_remote` is not `no` |
| `willing_to_relocate` `yes` | Any US state |
| `open_to_remote` `no` | Drop remote listings |
| `need_visa_sponsorship` `yes`, or `authorized_to_work` `no` | Drop listings that say no sponsorship or require US citizenship |
| Work history dates | Drop listings that ask for more years than the candidate has |

`404` if that id was never saved. `400` if the profile has no skills yet.

## Recommend from skills

```bash
curl -X POST "{BASE_URL}/api/recommend" ^
  -H "Content-Type: application/json" ^
  -d "{\"skills\": [\"Python\", \"FastAPI\", \"AWS\", \"Docker\"], \"limit\": 20}"
```

JavaScript:

```js
const res = await fetch(`${BASE_URL}/api/recommend`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    skills: ["Python", "FastAPI", "PostgreSQL", "AWS", "Docker"],
    state: "All",
    platform: "All",
    days: 30,
    limit: 20,
    refresh: false, // true = scrape boards with these skills first (slow)
  }),
});
const data = await res.json();
if (!res.ok) throw new Error(data.detail || "Recommend failed");
// data.jobs[0].match.score is 0-100
```

If the resume was already parsed with `/api/resume/parse`, send that JSON instead of a skill list:

```js
body: JSON.stringify({
  resume: {
    skills: parsed.skills,
  },
  limit: 20,
})
```

Or send raw resume text; skills are extracted the same way as `/api/resume/parse-text`:

```json
{ "resume_text": "Jane Doe\nSkills: Python, FastAPI, AWS, Docker\n..." }
```

`skills` wins if it is non-empty. Otherwise `resume.skills`, then `resume_text`.

## Recommend from a resume file

```bash
curl -X POST "{BASE_URL}/api/recommend/file" ^
  -F "file=@C:\path\to\resume.pdf" ^
  -F "limit=20"
```

JavaScript:

```js
const form = new FormData();
form.append("file", fileInput.files[0]);
form.append("limit", "20");
form.append("refresh", "false");

const res = await fetch(`${BASE_URL}/api/recommend/file`, {
  method: "POST",
  body: form,
});
const data = await res.json();
if (!res.ok) throw new Error(data.detail || "Recommend failed");
```

You can also send `skills` as form text (`Python, FastAPI, AWS`) without a file.

## Success response (listings)

HTTP `200`:

```json
{
  "jobs": [
    {
      "id": "remotive-1680495",
      "job_id": 1842,
      "title": "Senior Backend Engineer",
      "company": "Northwind Labs",
      "skills": ["Python", "FastAPI", "AWS"],
      "state": "Remote",
      "location": "United States",
      "platform": "Remotive",
      "daysAgo": "2 days ago",
      "url": "https://...",
      "description": "Senior backend role. 5+ years of experience. Visa sponsorship available.",
      "job_type": "fulltime",
      "work_model": "remote",
      "experience_level": "Senior Level",
      "years_experience": 5,
      "h1b_sponsorship": "yes",
      "clearance_required": "unknown",
      "us_citizen_required": "no",
      "posted_at": "2026-09-15",
      "e_verified": "unknown",
      "e_verify_name": null,
      "raw_description": {
        "salary": "$140,000 - $180,000",
        "categories": ["Engineering"],
        "applicationLink": "https://boards.example/apply/1680495"
      }
    }
  ],
  "count": 1,
  "total": 1,
  "limit": 20,
  "offset": 0,
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total": 1,
    "total_pages": 1,
    "has_next": false,
    "has_previous": false
  },
  "scraped_at": "2026-09-17T05:00:00+00:00",
  "age_seconds": 120
}
```

`count` is the jobs in this page. `total` is every match for the filters. `pagination.page` starts at 1. Request `page=2` (or `offset=20` with the same `limit`) for the next page.

`job_id` is the numeric `jobs.id` primary key. Use that in the app. `id` stays the board source id (`remotive-1680495`). `/api/cv/from-job` accepts that source id or this numeric id.

`raw_description` is always an object. It holds the scraped fields that are not already saved in our columns (`title`, `company`, `description`, `url`, and the other listing fields). Board fields such as `summary`, `companyName`, `salary`, and `categories` stay here. `{}` means the board sent nothing beyond those columns. `/api/jobs` loads it for the current page only, up to 200 jobs.

Missing fields may be `null` or `[]`. `url` is the apply / posting link. `daysAgo` is a label such as `just now`, `2 min ago`, `1 hour ago`, `7 days ago`, or `1 month ago`. Show that string as-is. The `days` query still filters by whole days.

`work_model`, `job_type`, `experience_level`, `years_experience`, `visa_sponsorship`, `clearance_required`, and `saudi_national_required` are saved on each row. They are read from the posting. `years_experience` is `null` when no minimum is stated.

`us_citizen_required` is always `yes` or `no`, never `unknown`. It is `yes` when the posting says so ("must be a U.S. citizen", "US citizenship required", "U.S. citizens only") or asks for a Secret / Top Secret / TS/SCI clearance, which only citizens can hold. Everything else is `no`: citizen **or** green card / permanent resident, "U.S. person" (ITAR), visa sponsorship offered, H1B/OPT welcome, or no mention of citizenship at all.

## Success response (recommend)

HTTP `200`:

```json
{
  "ok": true,
  "count": 1,
  "skills": ["Python", "FastAPI", "AWS", "Docker"],
  "jobs": [
    {
      "id": "remotive-1680495",
      "job_id": 1842,
      "title": "Senior Backend Engineer",
      "company": "Northwind Labs",
      "skills": ["Python", "FastAPI", "AWS"],
      "state": "Remote",
      "location": "United States",
      "platform": "Remotive",
      "daysAgo": "2 days ago",
      "url": "https://...",
      "match": {
        "score": 75,
        "matched_skills": ["Python", "FastAPI", "AWS"],
        "missing_skills": ["Docker"]
      }
    }
  ],
  "hint": null,
  "scraped_at": "2026-09-17T05:00:00+00:00",
  "age_seconds": 120
}
```

Jobs are sorted by `match.score` (high first), then newest. `limit` max is **100**, default **25**.

`hint` is a string when the cache is empty or nothing overlapped the skills. Otherwise `null`.

Use `job.id` with `POST /api/cv/from-job` as `job_id` to tailor a CV to that listing.

## Error responses

JSON `{ "detail": "..." }`.

| Status | When |
| --- | --- |
| 400 | No skills, and resume / text had none. Or the saved candidate has no skills |
| 404 | `candidate_id` was never saved with PUT `/api/candidates/{candidate_id}` |
| 409 | A scrape is already running (`refresh=true` or `/api/scrape`) |
| 413 | Resume file larger than 8 MB |
| 415 | Unsupported resume file type |
| 422 | Resume file had no extractable text |
| 504 | Scrape timed out |

Example: `{ "detail": "Send at least one skill, or a resume / resume_text that contains a skills section." }`

## CORS

CORS is open (`allow_origins=["*"]`), so a browser app on another origin can call this API directly.
