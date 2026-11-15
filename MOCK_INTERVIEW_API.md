# AI Mock Interview API

Give this document to the frontend or backend developer who will call the API.

**Production base URL:** `http://74.208.184.175:517`

Local example: `http://127.0.0.1:9001`

Interactive try-out: [http://74.208.184.175:517/docs](http://74.208.184.175:517/docs)

Related resume generation API: `AI_RESUME_API.md`.

Related cover letter API: `COVER_LETTER_API.md`.

## What it does

`POST /api/mock-interview/start` asks the local Ollama model to open a practice interview for a role.

`POST /api/mock-interview/answer` scores the candidate's reply and asks the next question. After the last answer it returns a closing report.

The interview is for `target_title`, `company`, a job description, or `job_id`. A role is required. Candidate facts are optional. When you send them, questions and the suggested answer use those employers, skills, and projects. The suggested answer does not add employers or numbers that were not in the facts or in the reply.

You can send the facts directly, paste `resume_text`, send parsed `resume` JSON from `/api/resume/parse`, or pass `user_id` for a saved profile. When more than one is sent, explicit fields win. Empty fields do not erase a loaded profile.

The server does not store the interview. Keep the `session` object from each response and send it back with the next answer. Send it unchanged.

Each answer is scored on its own. The closing report averages those scores. `question_source` is `model` or `template`. `feedback.source` is `model` or `rubric`. A template or rubric result is still HTTP 200. It means the model text could not be used, so the interview continued from the role and the reply.

## Endpoints

| Method | Path | Use |
| --- | --- | --- |
| GET | `/api/health` | Liveness check (`mock_interview: true`, plus `ollama.ok`) |
| GET | `/api/mock-interview` | Contract |
| POST | `/api/mock-interview/start` | First question and a session |
| POST | `/api/mock-interview/answer` | Score one answer and continue |

`question_count` is 3 to 8. The default is 5.

`focus`:

| Value | Questions lean toward |
| --- | --- |
| `mixed` (default) | Introduction, experience, skill, a workplace example, and a close |
| `behavioral` | Workplace examples |
| `technical` | Skills and how the work was done |

Check `GET /api/health` and read `ollama.ok` before calling these endpoints. Each call returns within about 75 seconds, so a proxy in front of the API does not drop the open connection. Keep the Postman request timeout at `0`.

## Start

```bash
curl -X POST "{BASE_URL}/api/mock-interview/start" ^
  -H "Content-Type: application/json" ^
  -d "{\"target_title\": \"Senior Backend Engineer\", \"company\": \"Northwind\", \"full_name\": \"Jane Doe\", \"skills\": [\"Python\", \"FastAPI\"], \"experience\": [{\"title\": \"Software Engineer\", \"company\": \"Acme Corp\", \"bullets\": [\"Built APIs used by 2 million users\"]}], \"question_count\": 5, \"focus\": \"mixed\"}"
```

JavaScript:

```js
const res = await fetch(`${BASE_URL}/api/mock-interview/start`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    target_title: "Senior Backend Engineer",
    company: "Northwind",
    full_name: "Jane Doe",
    skills: ["Python", "FastAPI", "PostgreSQL"],
    experience: [
      {
        title: "Software Engineer",
        company: "Acme Corp",
        bullets: ["Built APIs used by 2 million users"],
      },
    ],
    question_count: 5,
    focus: "mixed",
    job_description: "Optional posting text. Used to aim the questions.",
  }),
});
const data = await res.json();
if (!res.ok) throw new Error(data.detail || "Interview could not start");
// Keep data.session. Show data.question.prompt.
```

Optional inputs use the same names as `POST /api/resume/generate`:

| Field | When to send it |
| --- | --- |
| `resume_text` | Raw resume. Parsed first, then used as facts. |
| `resume` | JSON already returned by `/api/resume/parse`. |
| `user_id` | Saved app user. Same ids as `/api/resume/enhance`. |
| `job_id` | Scraped job from `/api/jobs`. Fills title, company, and description. |
| `job_title`, `company`, `job_location`, `job_description` | Override the loaded job. |
| `question_count` | 3 to 8. Default 5. |
| `focus` | `mixed`, `behavioral`, or `technical`. |

## Answer

```js
const res = await fetch(`${BASE_URL}/api/mock-interview/answer`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    session, // the session from the previous response, unchanged
    answer: "At Acme Corp I built FastAPI services used by 2 million users.",
  }),
});
const data = await res.json();
if (!res.ok) throw new Error(data.detail || "Answer could not be scored");
session = data.session;
if (data.status === "complete") {
  // data.report.overall_score, data.report.turns
} else {
  // data.feedback, then data.question.prompt
}
```

The answer needs at least one sentence. Longer replies are cut at 4000 characters.

## Success response

HTTP `200` while the interview is in progress:

```json
{
  "ok": true,
  "mode": "interview",
  "model": "qwen2.5:7b",
  "interview_id": "a1b2c3d4e5f6",
  "status": "in_progress",
  "focus": "mixed",
  "question_source": "model",
  "progress": { "answered": 1, "total": 5 },
  "job": { "title": "Senior Backend Engineer", "company": "Northwind", "location": null },
  "answered": {
    "number": 1,
    "category": "introduction",
    "prompt": "Tell me about yourself and why you want this role.",
    "answer": "At Acme Corp I built FastAPI services used by 2 million users."
  },
  "feedback": {
    "score": 8,
    "summary": "You named Acme Corp and the API work you did there.",
    "strengths": ["You named a real employer and a concrete result."],
    "improvements": ["Add what changed after the APIs launched."],
    "better_answer": "At Acme Corp I built FastAPI services used by 2 million users and kept them reliable.",
    "source": "model"
  },
  "question": {
    "number": 2,
    "total": 5,
    "category": "experience",
    "prompt": "Walk me through your work as a Software Engineer at Acme Corp."
  },
  "report": null,
  "session": {}
}
```

`question_source` is present on the start response only. The start response has `answered` and `feedback` set to `null`, and `progress.answered` is `0`.

`feedback.score` is 1 to 10. `better_answer` is `null` when that suggestion added an employer or a number that was not in the facts or the reply. `source` is `rubric` when the model score could not be used. The score then comes from the length of the reply and whether it mentions a skill or employer you sent.

After the last answer, `status` is `complete`, `question` is `null`, and `report` is set:

```json
{
  "status": "complete",
  "report": {
    "overall_score": 8,
    "summary": "You completed a 5-question mixed interview for the Senior Backend Engineer role at Northwind. Your average score is 8 out of 10.",
    "strengths": ["You named a real employer and a concrete result."],
    "improvements": ["Add what changed after the APIs launched."],
    "turns": [
      {
        "number": 1,
        "category": "introduction",
        "question": "Tell me about yourself and why you want this role.",
        "answer": "At Acme Corp I built FastAPI services used by 2 million users.",
        "score": 8,
        "summary": "You named Acme Corp and the API work you did there."
      }
    ]
  }
}
```

## Error responses

| Status | When |
| --- | --- |
| 400 | No job title, company, description, or `job_id`. `focus` is not `mixed`, `behavioral`, or `technical`. The answer is shorter than a sentence. The session is missing or the interview is already complete. |
| 404 | `user_id` or `job_id` was sent and is not on file. |
| 422 | `question_count` is outside 3 to 8, or `session` or `answer` was left out. |
| 503 | Ollama is turned off, has no model, or cannot be reached. The session is unchanged, so the same answer can be sent again. |

`detail` is a string. Example: `{ "detail": "Send a job title, company, job description, or job_id so the interview has a role." }`
