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

**Real gap, blocking, next up:** `discovery.py`'s `build_search_urls()` builds
literal `google.com/search?q=...` URLs. Nothing should fetch these — see
`SOLUTION_DEFINITION.md` §6 for why (Google's terms, plus it's not parseable
without a headless browser) and for the recommendation (Tavily, Brave as
alternative). This needs an actual decision + API key from the human before it's
buildable — flagged to them; not something either of us resolves alone. Until
then, `build_search_queries()` (the query strings themselves, not the URLs) is
solid and worth building the extraction pipeline's interface around.

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
| Discovery (profile → search) | `discovery.py` | Codex | query generation done; blocked on search API choice, see gap above |
| Extraction (search result → Opportunity draft) | not started | open | needs the search API decision first — its output shape depends on which provider |
| Drafting / package builder | not started | open | after extraction exists |

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
