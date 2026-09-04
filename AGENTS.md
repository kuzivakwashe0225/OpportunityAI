# Working here: Claude Code + Codex

Two coding agents share this working tree at the same time, with no live channel
between us — **git history is the only communication we have.** Before you start
any work in a session:

1. `git log --oneline` and `git status` — see what the other agent did since you
   last looked.
2. If a file you're about to change has commits from the other agent you haven't
   read, `git show <hash>` first. Don't edit a file blind.
3. Commit in small, focused steps with a message that explains *why*, not just
   *what* — the commit message is how the other agent finds out what you did and
   what you were thinking. Attribute yourself in the message (see the two commits
   already in history for the pattern).
4. Run the full test suite before every commit. Never commit red on purpose except
   as an immediately-following "add failing test" step in the same work session.

The product spec, architecture, and phased plan live in `SOLUTION_DEFINITION.md`.
Read that before making any scope decision — this file is only about *how we two
agents divide and hand off work*, not what the product is.

## Live deployment

Running at **https://opportunityai.meshcloud.co.zw/ui** (also reachable
directly at `http://161.97.176.218:8000/ui`, unencrypted - fine for now, see
review notes) as of 4 September — `docker compose` on the owner's own server
(`~/apps/OpportunityAI`, GitHub remote `kuzivakwashe0225/OpportunityAI`),
alongside several other unrelated live projects on that box (do not assume
it's a dedicated machine: caidev, valerie, agritrack, preciseagric,
telechaplaincy, ZRRSportal, lionturfportal, panica, pmi.ozzene.com all share
it). Ports 80/443 are fronted by a host-level **Caddy** instance
(`/etc/caddy/Caddyfile`, requires root to edit — the deploy SSH user has
`sudo` but not passwordless) serving all of those; this app was added there
too (a plain `reverse_proxy 127.0.0.1:8000` block, Caddy gets the TLS cert
automatically) rather than trying to bind 80/443 itself. **Before ever
editing that file again**: validate with
`caddy validate --config <candidate> --adapter caddyfile` first, apply with
`systemctl reload` (never `restart` — this fronts other people's live
sites), and spot-check a couple of the *other* domains still respond
afterward. Postgres and MinIO are not published to the host except MinIO's
own ports (9000/9001, needed for presigned URLs from outside the Docker
network) - Postgres stays internal-only.

To ship a change to production: push to `master`, then on the server
`cd ~/apps/OpportunityAI && git pull && docker compose up -d --build`.
Registration is capped at one account (see the auth review note below) - it
may already be claimed by the owner; don't consume it testing.

Also worth knowing: **Ollama is already running on that server** (port 11434,
found while checking for port conflicts) - relevant whenever the deferred
document-extraction work (SOLUTION_DEFINITION.md §14) actually starts.

## Current status

MVP vertical: scholarships only, read-only, no browser automation, no submission
(`SOLUTION_DEFINITION.md` §10, "MVP: the scholarship inbox"), now pivoted to
profile-driven discovery instead of a curated source list — see the spec's latest
commit if that sentence is a surprise. As of the last commit in this file's
history: models (including certificates/work_history for query generation),
matching engine, opportunity dedup, a digest formatter, a full workflow API
(`PUT /profile`, `POST /opportunities`, `GET /matches`,
`POST /opportunities/{id}/feedback`, `GET /digest`) with atomic file-backed
persistence built directly into `OpportunityStore`, a `SourceRegistry` for known
fixed portals, and a first pass at profile→query generation in `discovery.py`,
covered by 26 passing tests. Run `.venv/Scripts/python.exe -m pytest -q` to
confirm current state — don't trust this paragraph once it's a few commits old,
trust the test run.

(The earlier "known gap" here — in-memory-only `OpportunityStore` — is closed;
Codex solved it directly with atomic temp-file+replace writes in `store.py`,
which also covers opportunities and feedback, not just the profile. A separate
`storage.py`/`ProfileStore` had been built in parallel to fix the same gap;
removed as redundant once the two efforts landed on the same problem — this is
the kind of overlap this file exists to prevent, and it still happened once, so
check `git log` before assuming an old status line is current.)

**Resolved:** search API is **Tavily**. `search.py` (commit `7ab06cd`) has a
tested client — `search(query, api_key=..., max_results=...) -> list[SearchResult]`
— verified against Tavily's actual documented contract, not guessed.

**Current gap:** the Tavily-backed discovery path, safe public fetcher, verified
extraction, and persisted aggregate plus per-source discovery-run audit now
exist. Structured fields are extracted only from explicitly verified page/PDF
text; Tavily snippets remain evidence-only. The next observability increment is
richer provenance (content type, parser version, page/PDF references). The owner
still needs to set `TAVILY_API_KEY` before live discovery can run; tests mock the
client.

## Lane ownership

Claim a lane here before starting substantial work in it, so we don't build the
same thing twice. Move rows as work actually happens; this table is only useful if
it's kept current.

| Lane | Files | Owner | Status |
|---|---|---|---|
| Models / contracts | `models.py` | shared | additive only — extend, don't restructure, without a note here first |
| Matching engine | `matching.py` | Codex | active |
| Ingestion / dedup | `ingestion.py` | Codex | merge logic done; real fetching not started |
| Workflow API + persistence | `api.py`, `store.py` | Codex | done for MVP scope, atomic persistence included |
| Digest / review workspace | `digest.py` | Claude | done for MVP scope |
| Source registry (fixed portals) | `sources.py` | Codex | registry primitive done, not populated yet |
| Search client | `search.py` | Claude | done — Tavily, tested with mocked HTTP |
| Discovery (profile → search) | `discovery.py`, `search.py` | Codex/Claude | Tavily-backed profile discovery done; bounded and URL-deduplicated |
| Extraction (search result → Opportunity draft) | `extraction.py` | Codex | conservative parser done; verified-content gate protects hard requirements |
| Drafting / package builder | `drafting.py` | Claude | done — checklist + cover-note draft, no LLM call, nothing unfounded in the output |
| Test isolation | `tests/conftest.py` | Claude | done — module store singleton was defaulting to real `.data/store.json` in tests, fixed |
| Review UI | `src/opportunity_agent/web/index.html`, `api.py` (`GET /ui`, `GET /profile`) | Claude | **redesigned** (commit `870eaaa`) — direct request for a real interactive frontend, not incremental polish on the server-rendered version. Now a single-page app: vanilla JS, no build step, no new dependency, talks to the existing JSON API via `fetch()`. The two server-rendered-only routes (`POST /ui/opportunities/{id}/feedback`, `GET /ui/opportunities/{id}/package`) are gone — the SPA calls the plain JSON endpoints directly. `GET /profile` is new (previously write-only). If you were mid-work on the server-rendered version: it's superseded, not broken by accident — see the commit message. Extend `web/index.html`, don't resurrect the old inline-HTML approach in `api.py`. |

The drafting package is now available through both a JSON API endpoint and a
server-rendered review page. Submission remains intentionally absent.

### Phase 2 (new, `SOLUTION_DEFINITION.md` §14): accounts, documents, containers

Direct request, confirmed multi-tenant (separate logins, not a personal gate).
Built as new, independently-tested infrastructure alongside the working MVP,
**not wired into it yet** — `/discover`, `/matches`, `/digest`, `/ui` stay
single-tenant until that migration is deliberately scoped as its own step.
Don't assume auth applies to those endpoints just because it exists now.

| Lane | Files | Owner | Status |
|---|---|---|---|
| Accounts / auth | `db.py`, `auth.py`, `api.py`, `web/index.html` | Claude | **done and live-verified** — `/register` (capped at one account, see AGENTS.md gap notes below), `/login`, `/logout`, `/me`; every existing single-tenant endpoint now requires a session cookie except `/health` and `/ui`. Frontend has a login/register gate. Verified in a real browser via Playwright: register → app appears → session survives reload → logout → login → wrong password rejected. 127/127 tests pass |
| Data model (Account/Profile/Document/Notification) | `db.py`, `models_db.py` | Claude | done — SQLAlchemy, one Profile per type per Account enforced at the DB level, cascade deletes, tested against in-memory SQLite. Postgres in Docker via `DATABASE_URL`, not yet initialized there (no `init_db()` call wired into `api.py`'s startup - the API doesn't touch this DB at all yet) |
| Document vault (MinIO) | `documents.py`, `api.py` | Claude | **done and wired** - `POST/GET/DELETE /profiles/{id}/documents`, account-ownership-checked. Added `download_document()` (verified against the real SDK, like the other functions here). Live-verified end to end on the deployment server: real upload to real MinIO, real download, real delete - not just mocked tests |
| Profile CRUD (first real multi-tenancy) | `models_db.py`, `api.py` | Claude | **done** - `POST/GET/PUT /profiles`, account-scoped and isolated (tested with a genuinely separate second Account + session token, not just assumed). One profile per type per account, matching the DB constraint. This is the actual multi-tenant foundation the deferred pipeline-migration row below still needs |
| Document extraction (Ollama) | `document_text.py`, `extraction_llm.py`, `api.py` | Claude | **done, unblocked, and live-tested with a real CV-shaped document** on the deployment server (register → upload → extract → verify merged fields → clean up so the account slot stays free). Model is `qwen2.5:0.5b` (397MB) - the installed 27B model OOM-killed Ollama on the deployment server (11GB RAM, no GPU, shared with other live projects) on first live test; this one is verified safe. Quality is genuinely modest: work history extracted correctly and verbatim across two separate test CVs, but certificates and field of study were missed both times, and study level picked the completed degree over the in-progress one once. Merge into `profile.fields` is conservative - list fields (work_history, certificates) get new unique items appended, scalar fields (study_level, field) only fill in if currently empty; an owner-set value is never overwritten, tested explicitly |
| Worker service (scheduled discovery) | `worker.py` | Claude | done, tested. Known DRY debt: duplicates `api.py`'s `/discover` loop rather than sharing one function - not fixed given `api.py`'s continuous concurrent activity all session; extract a shared function when someone's next in both files anyway |
| Docker Compose (api/worker/db/minio) | `docker-compose.yml`, `Dockerfile`, `.dockerignore` | Claude | done and **actually verified end-to-end** - built the real image, started all four containers, hit `/health`, `/ui`, `/profile` and MinIO's health/console endpoints through the running containers. Caught and fixed a real bug this way: `web/index.html` wasn't included in a non-editable `pip install .` (local dev used `-e .` all session, which masked it completely) - see `[tool.setuptools.package-data]` in `pyproject.toml`. No MinIO healthcheck in compose - not verified what tooling that image has available |
| Notifications engine (in-app first) | not started | open | `Notification` table exists now - this is buildable |
| Multi-view UI (onboarding → dashboard → review queue → browse → profile) | `web/index.html` | Claude | **done and live** - app shell with hash routing, stepped onboarding, profile switcher, Agent Inbox-style review queue, "apply anyway" override on rejected items, notifications. 35/35 Playwright checks incl. switching, reload persistence, mobile layout. Extend this, don't start a sixth version |
| Autonomous per-profile pipeline | `pipeline.py`, `models_db.py`, `api.py`, `worker.py` | Claude | **done** - the unattended loop (discover → match → auto-shortlist → auto-draft → notify) running per profile for every account on the worker's schedule. Live-verified: real run found 10, added 4, auto-drafted 2, queued a notification |
| Delete endpoints for Profile/Account | not started | open, small | found while cleaning up e2e test data - had to delete rows directly via `psql` (children before parent - no DB-level `ON DELETE CASCADE`, only the ORM-level `cascade=` in `models_db.py`) since no `DELETE /profiles/{id}` or `DELETE /accounts/{id}` exists. Small, worth adding |
| Retire the legacy single-tenant path | `store.py`, `api.py`'s `/discover`,`/matches`,`/digest`,`/opportunities/*` | open | superseded by the per-profile pipeline above, which was built alongside it rather than rewriting it so the live deployment never broke. Nothing in the UI calls the old endpoints any more. Deleting them (and `store.py`, and `worker.py`'s legacy cycle) is now a straightforward cleanup - check nothing else references them first |
| Learning from feedback | not started | open | signal is already being collected (`usefulness`, decision history, escalations) and nothing consumes it. Be honest about scale before building: tens of opportunities a week is not training data. The achievable version is inspectable heuristics - down-weight sources always dismissed, up-weight terms in what gets approved - as a layer *above* the hard eligibility rules, never replacing them. See SOLUTION_DEFINITION.md §16 |

## Review protocol

We review each other by reading commits, not by talking. When you find something
worth flagging in the other's code:

- **Small, clear bug:** write a regression test proving it, confirm it fails, fix
  it, commit with a message explaining the failure scenario. (See commit
  `09318b5` — country eligibility was substring-matching, so "Niger" matched a
  profile country of "Nigeria".)
- **Design judgment call, not a clear bug:** leave it under "Open review notes"
  below instead of unilaterally changing someone else's deliberate, tested
  behavior. Whoever owns that lane resolves it in a follow-up commit.

### Open review notes

- **Ollama's port (11434) is open to the public internet on the deployment
  server**, found while testing extraction from this machine (a plain
  `curl http://161.97.176.218:11434/api/tags` from outside works, no auth).
  Not something either of us configured - it's how Ollama was already set up
  on that box before this project touched it - but now that `api`/`worker`
  actually depend on it, it's worth knowing: anyone can currently run
  inference or pull/manage models on that server for free, which is both a
  cost/abuse vector and exactly the kind of resource contention that already
  OOM-killed Ollama once during testing (see the extraction_llm.py commit).
  Firewalling it to only the Docker host's internal bridge (or the specific
  container IPs) rather than `0.0.0.0` would close this without changing how
  `api`/`worker` reach it via `host.docker.internal`. Not fixed here - it's
  server configuration outside this repo, the owner's call. — Claude

- **Session cookie isn't marked `Secure`**: now that HTTPS exists
  (`https://opportunityai.meshcloud.co.zw`), `_set_session_cookie()` in
  `api.py` should set `secure=True` so the browser never sends it over the
  still-open plain-HTTP path (`http://161.97.176.218:8000`). Not fixed here -
  deciding whether to keep the plain-HTTP path open at all (redirect to
  HTTPS instead?) is a small product call, not just a one-line flag flip. —
  Claude

- **Auth's single-account cap is an interim safety measure, not the design**:
  `/register` returns 403 once any account exists. This is deliberate given
  the current data model - `store` is still one global `OpportunityStore`
  (§14's pipeline migration hasn't happened), so a second real account would
  see and edit the first account's profile/opportunities. Capping
  registration at one account was the honest choice given that reality,
  not a shortcut to remove later without also finishing the pipeline
  migration first. If that migration lands, revisit this cap - it's the
  first thing that should come off, not an incidental restriction to keep
  around. — Claude

- **Full Playwright-driven interaction test of the new SPA** (through commit
  `d62c61f` — real Chromium, real server subprocess, real `/discover` call,
  not TestClient): every button, form field, tag-input, filter tab,
  toggle, and feedback action clicked and asserted on — profile CRUD
  round-trip, tag add/remove/backspace, discovery progress state, filters,
  package panel open/close, evidence expand/collapse, all four feedback types,
  runs/digest panels, responsive layout, console/JS error monitoring. 43/43
  passing after fixing two real bugs the test surfaced: (1) the name input's
  native `required` attribute was pre-empting the page's own styled validation
  message — removed, added `novalidate`; (2) a race condition where the
  discovery-progress ticker could overwrite the "Found X, added Y" success
  message with a stale "Fetching…" string if it fired during the
  `await loadMatches()` window right after success — fixed by clearing it
  immediately on resolution, not only in `finally`. Also known: client-side
  evidence truncation (the SPA) bounds page size/load time but does not fix
  readability — a truncated raw-HTML preview is still raw HTML, just short.
  The real fix is still the server-side one below. — Claude

- **Full UI/UX + workflow pass, done with a real server on a real port** (not
  TestClient): empty-profile `/ui`, set-profile, zero-opportunities `/ui`,
  live `/discover`, loaded `/ui`, package view, dismiss/shortlist/useful
  feedback, digest, and a full process restart to check persistence. Findings
  beyond the raw-HTML-evidence one below (still confirmed present: 1.7MB `/ui`
  for 7 opportunities, 7 `DOCTYPE` leaks this run):
  - **`POST /discover` took 34 seconds** (18 results, fetched one page at a
    time, sequentially). If a UI control ever calls this synchronously, most
    browsers/reverse proxies time out around 30s by default, and there's no
    progress feedback either way — worth async/background execution or at
    least a fetch concurrency limit before this is exposed as a button.
  - **`/ui` has no way to create/edit a profile or trigger discovery** — the
    empty state literally says "Add one through the profile API," with no
    link or form. Right now the only way to *use* the review page at all is
    to leave the browser and call the JSON API first. Reasonable for this
    stage (JSON API works, confirmed via full live walkthrough), but it means
    `/ui` isn't yet a complete workflow on its own.
  - **Visual inconsistency**: `/ui` has an inline `<style>` block (serif
    font, warm palette); `/ui/opportunities/{id}/package` has none at all —
    plain unstyled HTML. Jarring when navigating between them.
  - **Errors on `/ui/...` routes return raw JSON** (`{"detail": "..."}`),
    breaking the page's look — a stale link mid-session dumps you into an
    unstyled JSON blob instead of a styled error page.
  - **What does work well, confirmed live**: dismiss/shortlist/useful/
    not_useful all persisted correctly and independently (shortlisted +
    useful on the same item didn't clobber each other); dismissed items
    correctly disappear from `/ui` and `/digest` while staying in `/matches`;
    a full server restart against the same store file preserved every
    opportunity and decision exactly. The underlying workflow is solid — the
    gaps above are UI completeness/polish, not correctness. — Claude

- **Highest-severity finding, from testing the real `/ui` against a live
  server** (profile → `/discover` → opened `/ui` in an HTTP client, not just
  TestClient): the page came back **3.0 MB for 10 opportunities**, because
  `evidence` stores the entire raw HTTP response body from
  `fetch_public_page()` — full `<!DOCTYPE html>`, `<head>`, inline `<script>`,
  meta tags, everything — and `/ui` renders `evidence[0]` (HTML-escaped)
  straight into a `<p>`. A human opening the review page sees a wall of
  escaped markup, not readable evidence text; 9 of 10 opportunities in this
  run had a literal `DOCTYPE` string visible in the page. This is worse than
  the earlier findings because it's not a wrong answer, it's the review
  workspace being functionally unusable for its actual job (§5.7: "Draft
  review showing every generated answer with its supporting... source
  documents" — presupposes the source text is readable).
  Second-order effect, same root cause: `extraction.py`'s regex extractors
  (`_extract_countries/_levels/_fields/_age/_documents`) run against this same
  raw HTML, not clean visible text — fragile by construction (matches inside
  `<script>`/meta content are indistinguishable from real page text to a
  regex), and probably compounds the `_extract_age` false-positive above.
  Third-order: every stored opportunity now persists hundreds of KB into
  `.data/store.json`, so store size scales badly with opportunity count.
  Recommended fix (not applied — this is a real feature addition to
  `connector.py`/`extraction.py`, not a one-liner, and both are Codex's active
  files): extract clean readable text from the HTML before it ever becomes
  `evidence` or reaches the regex extractors — either a proper readability-style
  library (`trafilatura`, already flagged as a candidate in
  `SOLUTION_DEFINITION.md` §6 research) or, if avoiding a new dependency
  matters more right now, a minimal stdlib `html.parser` strip. Either way,
  regex extraction should run on the same cleaned text that gets stored as
  evidence, not raw HTML. — Claude

- **Found running a live user-journey test** (real profile, real Tavily
  results, real fetched pages — not mocks): `_extract_age()` in
  `extraction.py` produced `required_age_max=9` for "10 Renewable Energy
  Resources Scholarships available," wrongly marking it `ineligible` for a
  27-year-old profile. Cause: `r"(?:...|under|below|younger than)\s+(\d{2})"`
  matches *any* "under/below/younger than <2 digits>" anywhere in the fetched
  page, with no requirement that it's actually talking about age — "under 10
  minutes," "ranked under 10," anything with that shape matches. Not fixing
  this myself: the obvious tightening (require an "age"/"years" keyword near
  the match) would break `test_result_parser_extracts_common_scholarship_requirements`,
  which deliberately asserts age extraction from bare "at most 35" with zero
  age-context words — so the fix means both tightening the regex *and*
  deciding whether that existing fixture should gain realistic context
  ("at most 35 years of age") to match, which is a call for whoever owns
  `extraction.py`, not something to change unilaterally mid-review. The other
  extractors (`_extract_countries/_levels/_fields/_documents`) don't have this
  problem — they match named vocabulary, not generic numeric patterns, so
  false positives there require an actual keyword collision rather than any
  nearby two-digit number. — Claude

- **Found by actually running the live pipeline, not by inspection** (with the
  now-configured real `TAVILY_API_KEY`): `PUT /profile` → `POST /discover` →
  `GET /digest` end to end, real Zimbabwe/masters/CS profile, real Tavily
  results. All **15/15** discovered opportunities came back `eligible, score
  100` in the digest under "Ready to apply" — because `/discover` calls
  `result_to_opportunity(result)` with the default `verified=False`, so *zero*
  eligibility fields ever get extracted, and `matching.py` reads "no fields
  populated" as "no requirements exist" rather than "unknown." The conservative
  gate in `extraction.py` (commit `6fa1279`) is doing exactly what it says —
  keeping snippets from producing false hard requirements — but nothing
  downstream knows the difference between "verified, genuinely unrestricted"
  and "never actually checked," so the digest currently reports every single
  discovery-stage result as a perfect match. That's worse than showing nothing:
  it's confident-looking noise, and directly contradicts the MVP acceptance
  criteria ("an unknown requirement is never presented as eligible").
  `connector.py`'s `fetch_public_page()` (in progress as of this note) plus
  wiring `/discover` to call it and pass `verified=True` into extraction is the
  real fix and looks like exactly where this is already headed. Until that
  lands: consider whether `matching.py` should treat an `Opportunity` with
  zero populated eligibility fields as `needs_review` rather than `eligible` as
  a stopgap, so the digest doesn't ship misleading "eligible" results in the
  meantime. Leaving both options here rather than picking one — this is a
  design call, and `/discover`'s wiring is already Codex's active thread. —
  Claude

- `matching.py`: a missing required document currently produces a **hard
  `ineligible`** (see `test_missing_document_is_a_hard_failure`, named
  intentionally). Worth a second look: `SOLUTION_DEFINITION.md` §5.3 says a
  missing profile fact should be marked "missing" and asked about, not treated as
  disqualifying — a document you just haven't uploaded yet reads differently from
  a citizenship mismatch you can never fix. Possibly `unknown_requirements`
  (→ `needs_review`) is the better status for *documents*, keeping hard failure
  for facts that are genuinely immutable (country, age, deadline). Leaving this
  for whoever touches `matching.py` next rather than changing tested behavior
  unilaterally. — Claude
