# OpportunityAI

The scholarship-discovery flow at `/ui` is the first working slice of OpportunityAI. It discovers opportunities from a real search API using your own profile (no hand-picked source list), checks them against an evidence-backed eligibility model, and prepares a digest. It does not log in, upload, pay, send, or submit anything on that flow yet — see `SOLUTION_DEFINITION.md` for the full problem definition, architecture, and phased plan, and `AGENTS.md` for who's built what and what's still open. (The Python package itself stays `opportunity_agent` — an internal name, not user-facing, not worth the churn of renaming everywhere it's imported.)

## Setup

```powershell
.venv\Scripts\python.exe -m pip install -e ".[test]"
```

Copy `.env.example` to `.env` and fill in `TAVILY_API_KEY` (sign up at [tavily.com](https://tavily.com) — needed for `/discover` to actually search anything; the rest of the app runs fine without it). `OPPORTUNITY_AGENT_STORE_PATH` is optional and only needed if you want the app's data somewhere other than `.data/store.json`.

## Running it

```powershell
.venv\Scripts\python.exe -m uvicorn opportunity_agent.api:app --reload
```

Then open **http://127.0.0.1:8000/ui** — a server-rendered review page listing discovered opportunities with their eligibility status, evidence, and shortlist/dismiss actions. No separate frontend build or JS toolchain; it's plain HTML forms.

To actually get something to review, you need a profile and a discovery run first — either through the JSON API below, or by hand-crafting requests against it:

```powershell
$profile = @{
    name         = "Your Name"
    country      = "Zimbabwe"
    study_level  = "masters"
    field        = "Computer Science"
    documents    = @("transcript", "cv")
    interests    = @("renewable energy")
    goals        = @("build climate technology for Southern Africa")
    certificates = @("BSc Computer Science, University of Zimbabwe (2021)")
} | ConvertTo-Json

Invoke-RestMethod -Method Put -Uri http://127.0.0.1:8000/profile -ContentType "application/json" -Body $profile
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/discover
```

The richer the profile — certificates, work history, goals, not just tags — the better the search queries discovery generates from it. A thin profile means thin discovery, regardless of how good the search API is.

## API surface

| Endpoint | What it does |
|---|---|
| `GET /health` | Liveness check |
| `GET /ui` | The review page (human-facing, server-rendered) |
| `PUT /profile` | Create/replace the single stored profile |
| `POST /opportunities` | Add an opportunity manually (mainly for testing) |
| `POST /discover` | Run profile-driven search, fetch and verify each result, store new opportunities |
| `GET /matches` | Every stored opportunity with its computed eligibility status, score, and reasons |
| `GET /opportunities/{id}/package` | A draft application package (checklist + cover note) for one opportunity |
| `POST /opportunities/{id}/feedback` | Record `shortlisted` / `dismissed` / `useful` / `not_useful` |
| `GET /digest` | The formatted text digest (excludes dismissed items) |
| `GET /runs` | Audit history of discovery runs — queries used, found/added counts, per-source failures |

## Development

```powershell
.venv\Scripts\python.exe -m pytest
```

Tests are isolated from real app data — `tests/conftest.py` redirects the store to a throwaway path per test, so running the suite never touches your actual `.data/store.json`.

Current coverage: matching (hard eligibility, evidence/verification gating, deadline expiry, interest scoring), opportunity deduplication, discovery (profile → search queries → deduplicated results), the Tavily search client, page fetching and content extraction, the digest formatter, the application-package drafter, the full workflow API, and the review UI.
