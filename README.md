# Job Application Agent

A lightweight prototype for a pure job application agent.

## What it does

- Accepts a job description URL and extracts visible JD text.
- Lets a user upload a resume as `.txt`, `.docx`, or a text-based `.pdf`.
- Scores the resume against the JD with an ATS-style keyword and skill match report.
- Generates tailored resume summary text, skills, bullets, and a short cover note.
- Prepares a human-reviewed application checklist and application form link.

The prototype intentionally stops before final submission. It prepares the package and review steps so the user stays in control of any real application.

## Run

```bash
python3 app.py 8000
```

Open:

```text
http://127.0.0.1:8000
```

## Notes

- The URL fetcher depends on the target job board allowing direct page access.
- PDF extraction is best-effort without external dependencies. For the cleanest parsing, upload `.txt` or `.docx`.
- The ATS score is a transparent heuristic, not a guarantee from any employer ATS.
