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
| Review UI | `api.py` (`/ui`, `/ui/opportunities/{id}/feedback`) | Codex | in progress as of this row — server-rendered HTML, no JS, form-based feedback with a 303 redirect. Don't build a competing UI; if extending it, extend this one |

**Not wired yet:** `drafting.build_application_package()` exists and is tested
but has no API endpoint (e.g. `POST /opportunities/{id}/package`) — not adding
one myself right now since `api.py` is mid-edit on the discovery/verification
path. Small, obvious follow-up once that settles: call it with
`store`'s stored opportunity + a fresh `match_opportunity()` call and return
the `ApplicationPackage`.

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
