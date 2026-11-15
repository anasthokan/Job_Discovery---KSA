# AI Resume Generation API

Give this document to the frontend or backend developer who will call the API.

**Production base URL:** `http://74.208.184.175:517`

Local example: `http://127.0.0.1:9001`

Interactive try-out: [http://74.208.184.175:517/docs](http://74.208.184.175:517/docs)

Related parse API: `RESUME_API.md`.

Job-description-to-CV API (no model, template or reorder only): `CV_API.md`.

Saved-user file rewrite: `POST /api/resume/enhance` in `RESUME_API.md`.

## What it does

`POST /api/resume/generate` asks the local Ollama model to write a resume from facts you send.

The model rewrites the professional summary and the experience bullets so they read toward `target_title` or a job description. It does **not** invent employers, job titles, dates, skills, degrees, certifications, or numbers that were not in the request.

You can send the facts directly, paste `resume_text`, send parsed `resume` JSON from `/api/resume/parse`, or pass `user_id` for a saved profile. When more than one is sent, explicit fields win. Empty fields do not erase a loaded profile.

`cv` uses the same field names as `/api/resume/parse`.

## Endpoint

| Method | Path | Use |
| --- | --- | --- |
| GET | `/api/health` | Liveness check (`resume_generate: true`, plus `ollama.ok`) |
| GET | `/api/resume` | Contract, including this endpoint |
| POST | `/api/resume/generate` | Generate the resume |

Query `format`:

| Value | Response |
| --- | --- |
| `json` (default) | JSON body described below |
| `pdf` | PDF file download |
| `docx` | Word file download |

File responses set `X-Resume-Generate-Mode: generated` and `X-Resume-Model` to the Ollama model name.

## JSON

```bash
curl -X POST "{BASE_URL}/api/resume/generate" ^
  -H "Content-Type: application/json" ^
  -d "{\"target_title\": \"Senior Backend Engineer\", \"full_name\": \"Jane Doe\", \"email\": \"jane@example.com\", \"location\": \"Riyadh\", \"skills\": [\"Python\", \"FastAPI\", \"PostgreSQL\"], \"experience\": [{\"title\": \"Software Engineer\", \"company\": \"Acme Corp\", \"start_date\": \"Jan 2022\", \"end_date\": \"Present\", \"bullets\": [\"Built APIs used by 2 million users\"]}]}"
```

JavaScript:

```js
const res = await fetch(`${BASE_URL}/api/resume/generate`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    target_title: "Senior Backend Engineer",
    full_name: "Jane Doe",
    email: "jane@example.com",
    phone: "+966500000000",
    location: "Riyadh",
    skills: ["Python", "FastAPI", "PostgreSQL"],
    experience: [
      {
        title: "Software Engineer",
        company: "Acme Corp",
        start_date: "Jan 2022",
        end_date: "Present",
        bullets: ["Built APIs used by 2 million users"],
      },
    ],
    education: [
      { degree: "B.S.", field: "Computer Science", institution: "KSU", end_year: "2018" },
    ],
    job_description: "Optional posting text. Used for emphasis and the match score.",
  }),
});
const data = await res.json();
if (!res.ok) throw new Error(data.detail || "Resume generation failed");
// data.cv, data.cv_text, data.summary_source, data.match
```

Optional inputs:

| Field | When to send it |
| --- | --- |
| `resume_text` | Raw resume. Parsed first, then rewritten. |
| `resume` | JSON already returned by `/api/resume/parse`. |
| `user_id` | Saved app user. Same ids as `/api/resume/enhance`. |
| `job_id` | Scraped job from `/api/jobs`. Fills title, company, and description. |
| `job_title`, `company`, `job_location`, `job_description` | Override the loaded job. |

`summary_source` is `model` when the summary text is the model's, and `facts` when that text was rejected and a short summary was built from the supplied facts instead. `rewritten` is true when the summary or at least one role's bullets changed.

`match.score` is 0-100 when the job lists skills. It counts overlap only. Missing skills stay in `match.missing_skills` and are not added to `cv.skills`.

## Success response

HTTP `200`:

```json
{
  "ok": true,
  "mode": "generated",
  "model": "qwen2.5:7b",
  "target_title": "Senior Backend Engineer",
  "summary_source": "model",
  "rewritten": true,
  "note": "Employers, dates, skills, education, and certifications come from the request. The model rewrote the summary and experience bullets.",
  "job": {
    "title": "Senior Backend Engineer",
    "company": null,
    "location": null,
    "required_skills": []
  },
  "match": {
    "score": null,
    "matched_skills": [],
    "missing_skills": [],
    "extra_skills": []
  },
  "cv": {
    "profile": {
      "full_name": "Jane Doe",
      "email": "jane@example.com",
      "summary": "Jane Doe is a Senior Backend Engineer..."
    },
    "skills": ["Python", "FastAPI", "PostgreSQL"],
    "experience": [
      {
        "title": "Software Engineer",
        "company": "Acme Corp",
        "start_date": "Jan 2022",
        "end_date": "Present",
        "is_current": true,
        "bullets": ["Built APIs used by 2 million users"]
      }
    ]
  },
  "cv_text": "Jane Doe\n..."
}
```

## Error responses

| Status | When |
| --- | --- |
| 400 | No skills, work history, education, or summary to write from. `format` is not `json`, `pdf`, or `docx`. Resume text is too short. |
| 404 | `user_id` or `job_id` was sent and is not on file. |
| 502 | Ollama answered, but the answer was not usable JSON. |
| 503 | Ollama is turned off, has no model, or cannot be reached. |

`detail` is a string. Example: `{ "detail": "Send skills, work history, education, or a summary. This API writes from those facts and does not invent a career." }`

Check `GET /api/health` and read `ollama.ok` before calling this endpoint. Generation waits on the model and can take up to two minutes.

## CORS

CORS is open (`allow_origins=["*"]`), so a browser app on another origin can call this API directly.
