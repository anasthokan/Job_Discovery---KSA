# Resume / CV Parse API

Give this document to the frontend or backend developer who will call the API.

**Production base URL:** `http://74.208.184.175:517`

Local example: `http://127.0.0.1:9001`

Interactive try-out: [http://74.208.184.175:517/docs](http://74.208.184.175:517/docs)

Job listings + skill recommend API: `JOBS_API.md`.

ATS resume score API: `ATS_API.md` (`POST /api/ats/score-file`).

Job-description-to-CV API: `CV_API.md` (`POST /api/cv/from-job`).

AI resume generation API: `AI_RESUME_API.md` (`POST /api/resume/generate`).

AI cover letter API: `COVER_LETTER_API.md` (`POST /api/cover-letter/generate`).

PDF ↔ DOCX convert API: `CONVERT_API.md`.

Sample files in this repo: `samples/sample-resume.txt` and `samples/sample-resume.docx`.

## What it does

Upload a resume (PDF, DOCX, or TXT) or send raw text. The API extracts a structured JSON profile: name, email, phone, location, links, skills, experience, education, projects, certifications, and languages.

Files are parsed in memory. Nothing is saved on disk.

## Endpoints

| Method | Path | Use |
| --- | --- | --- |
| GET | `/api/health` | Liveness check (`resume_parse: true`) |
| GET | `/api/resume` | Contract / supported formats |
| POST | `/api/resume/parse` | Upload a file (`multipart/form-data`) |
| POST | `/api/resume/parse-text` | Send `{ "text": "..." }` JSON |
| POST | `/api/resume/scrape` | Same as `/api/resume/parse` |

Limits: max **8 MB**. Types: **pdf, docx, txt**. Old `.doc` is not supported; convert to PDF or DOCX first.

Query flag on parse endpoints: `include_raw=true` adds `raw_text`. Default is omitted so PII is not echoed back.

## File upload

```bash
curl -X POST "{BASE_URL}/api/resume/parse" ^
  -F "file=@C:\path\to\resume.pdf"
```

JavaScript:

```js
const form = new FormData();
form.append("file", fileInput.files[0]);

const res = await fetch(`${BASE_URL}/api/resume/parse`, {
  method: "POST",
  body: form,
});
const data = await res.json();
if (!res.ok) throw new Error(data.detail || "Resume parse failed");
```

Python:

```python
import requests

with open("resume.pdf", "rb") as handle:
    response = requests.post(
        "http://127.0.0.1:9001/api/resume/parse",
        files={"file": ("resume.pdf", handle, "application/pdf")},
    )
response.raise_for_status()
print(response.json())
```

## Text upload

```bash
curl -X POST "{BASE_URL}/api/resume/parse-text" ^
  -H "Content-Type: application/json" ^
  -d "{\"text\": \"Jane Doe\\nEmail: jane@example.com\\nSkills: Python, SQL\\n...\"}"
```

## Success response

HTTP `200`:

```json
{
  "ok": true,
  "source": {
    "filename": "resume.pdf",
    "content_type": "application/pdf",
    "pages": 2,
    "char_count": 4120
  },
  "profile": {
    "full_name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "415-555-0199",
    "phones": ["415-555-0199"],
    "location": "San Francisco, CA",
    "linkedin": "https://linkedin.com/in/janedoe",
    "github": "https://github.com/janedoe",
    "website": "https://janedoe.dev",
    "summary": "Backend engineer with 6 years of experience..."
  },
  "skills": ["Python", "FastAPI", "SQL", "AWS"],
  "experience": [
    {
      "title": "Senior Software Engineer",
      "company": "Acme Corp",
      "location": "San Francisco, CA",
      "start_date": "Jan 2022",
      "end_date": "Present",
      "is_current": true,
      "bullets": ["Built APIs used by 2M users"]
    }
  ],
  "education": [
    {
      "degree": "B.S.",
      "field": "Computer Science",
      "institution": "Stanford University",
      "start_year": "2014",
      "end_year": "2018",
      "gpa": null
    }
  ],
  "projects": [{ "name": "Job Finder", "description": "Scrapes public job boards" }],
  "certifications": ["AWS Solutions Architect"],
  "languages": ["English", "Spanish"],
  "raw_text": null
}
```

Missing fields are `null` or `[]`. Extraction is heuristic; scanned image-only PDFs will fail because there is no OCR in this version.

## Error responses

| Status | When |
| --- | --- |
| 400 | Empty file or text too short |
| 413 | File larger than 8 MB |
| 415 | Unsupported file type |
| 422 | Text could not be extracted (image-only PDF, corrupt file) |

`detail` is a string. Example: `{ "detail": "Unsupported file type '.doc'. Use PDF, DOCX, or TXT." }`

## CORS

CORS is open (`allow_origins=["*"]`), so a browser app on another origin can call this API directly.
