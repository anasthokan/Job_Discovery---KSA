# AI Cover Letter API

Give this document to the frontend or backend developer who will call the API.

**Production base URL:** `http://74.208.184.175:517`

Local example: `http://127.0.0.1:9001`

Interactive try-out: [http://74.208.184.175:517/docs](http://74.208.184.175:517/docs)

Related resume generation API: `AI_RESUME_API.md`.

Related parse API: `RESUME_API.md`.

## What it does

`POST /api/cover-letter/generate` asks the local Ollama model to write a first-person cover letter from facts you send.

The letter addresses `target_title`, `company`, or a job description. It has three paragraphs, a greeting, and a signature. The greeting is `Dear {hiring_manager},` when you send that name, and `Dear Hiring Manager,` otherwise. The model does not choose the manager's name.

It does **not** invent employers or numbers that were not in the request. If the model adds one, that answer is dropped and the letter is built from the supplied facts instead. `letter_source` is `model` or `facts`.

You can send the facts directly, paste `resume_text`, send parsed `resume` JSON from `/api/resume/parse`, or pass `user_id` for a saved profile. When more than one is sent, explicit fields win. Empty fields do not erase a loaded profile.

A job title, company, job description, or `job_id` is required. A letter with no role to address is rejected.

## Endpoint

| Method | Path | Use |
| --- | --- | --- |
| GET | `/api/health` | Liveness check (`cover_letter: true`, plus `ollama.ok`) |
| GET | `/api/cover-letter` | Contract |
| POST | `/api/cover-letter/generate` | Generate the letter |

Query `format`:

| Value | Response |
| --- | --- |
| `pdf` (default) | PDF file download named `cover-letter.pdf` |
| `docx` | Word file download |
| `json` | JSON body described below |

File responses set `X-Cover-Letter-Mode`, `X-Cover-Letter-Model`, and `X-Cover-Letter-Source` (`model` or `facts`).

## PDF download

```bash
curl -X POST "{BASE_URL}/api/cover-letter/generate" ^
  -H "Content-Type: application/json" ^
  -o cover-letter.pdf ^
  -d "{\"target_title\": \"Senior Backend Engineer\", \"company\": \"Northwind\", \"hiring_manager\": \"Sara Ali\", \"full_name\": \"Jane Doe\", \"email\": \"jane@example.com\", \"location\": \"Riyadh\", \"skills\": [\"Python\", \"FastAPI\", \"PostgreSQL\"], \"experience\": [{\"title\": \"Software Engineer\", \"company\": \"Acme Corp\", \"bullets\": [\"Built APIs used by 2 million users\"]}]}"
```

## JSON

```bash
curl -X POST "{BASE_URL}/api/cover-letter/generate?format=json" ^
  -H "Content-Type: application/json" ^
  -d "{\"target_title\": \"Senior Backend Engineer\", \"company\": \"Northwind\", \"full_name\": \"Jane Doe\", \"email\": \"jane@example.com\", \"location\": \"Riyadh\", \"skills\": [\"Python\", \"FastAPI\"], \"experience\": [{\"title\": \"Software Engineer\", \"company\": \"Acme Corp\", \"bullets\": [\"Built APIs used by 2 million users\"]}]}"
```

JavaScript:

```js
const res = await fetch(`${BASE_URL}/api/cover-letter/generate?format=json`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    target_title: "Senior Backend Engineer",
    company: "Northwind",
    hiring_manager: "Sara Ali",
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
    job_description: "Optional posting text. Used to aim the letter.",
  }),
});
const data = await res.json();
if (!res.ok) throw new Error(data.detail || "Cover letter generation failed");
// data.letter, data.letter_text, data.letter_source
```

Optional inputs use the same names as `POST /api/resume/generate`:

| Field | When to send it |
| --- | --- |
| `resume_text` | Raw resume. Parsed first, then used as facts. |
| `resume` | JSON already returned by `/api/resume/parse`. |
| `user_id` | Saved app user. Same ids as `/api/resume/enhance`. |
| `job_id` | Scraped job from `/api/jobs`. Fills title, company, and description. |
| `job_title`, `company`, `job_location`, `job_description` | Override the loaded job. |
| `hiring_manager` | Name in the greeting. Leave empty for Dear Hiring Manager. |

## Success response

HTTP `200`:

```json
{
  "ok": true,
  "mode": "generated",
  "model": "qwen2.5:7b",
  "letter_source": "model",
  "target_title": "Senior Backend Engineer",
  "hiring_manager": "Sara Ali",
  "note": "The greeting uses the hiring manager you sent, or Dear Hiring Manager. Employers and numbers that were not in the request are left out. The wording is first person.",
  "job": {
    "title": "Senior Backend Engineer",
    "company": "Northwind",
    "location": null
  },
  "letter": {
    "greeting": "Dear Sara Ali,",
    "paragraphs": [
      "I am writing to apply for the Senior Backend Engineer role at Northwind.",
      "At Acme Corp I built APIs used by 2 million users with Python and FastAPI.",
      "I would welcome the chance to discuss the Senior Backend Engineer opening."
    ],
    "closing": "Sincerely,",
    "signature": "Jane Doe"
  },
  "letter_text": "Jane Doe\nRiyadh | jane@example.com\n\nDear Sara Ali,\n\n..."
}
```

`letter_source` is `facts` when the model answer was rejected and the three paragraphs were built from the request instead. The HTTP status is still 200. A rejected answer is one that adds a number, or says the person already works at the hiring company when that company is not in their work history.

## Error responses

| Status | When |
| --- | --- |
| 400 | No skills, work history, education, or summary to write from. No job title, company, description, or `job_id`. `format` is not `json`, `pdf`, or `docx`. Resume text is too short. |
| 404 | `user_id` or `job_id` was sent and is not on file. |
| 502 | Ollama answered, but the answer was not usable JSON. |
| 503 | Ollama is turned off, has no model, or cannot be reached. |

`detail` is a string. Example: `{ "detail": "Send a job title, company, job description, or job_id so the letter has a role to address." }`

Check `GET /api/health` and read `ollama.ok` before calling this endpoint. Generation waits on the model and can take up to about 90 seconds.

## CORS

CORS is open (`allow_origins=["*"]`), so a browser app on another origin can call this API directly.
