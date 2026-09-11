# SearXNG configuration notes

`settings.yml` here is upstream's own default file (fetched from
`searxng/searxng`'s `master` branch, not written from scratch), with two
kinds of change layered on top. Keeping it a real diff of the upstream file
- rather than a short hand-written override - means a future SearXNG upgrade
can be diffed against a fresh copy of the same upstream file to see exactly
what this project changed and why.

## 1. JSON output enabled

`search.formats` gained `json` alongside the default `html`. Required because
`opportunity_agent.search.search_via_searxng()` is the only caller - never a
browser - and `format=json` is disabled by default on every SearXNG instance
for exactly that reason (most public instances also disable it, which is why
none could be used to verify the response shape during development - see
`search.py`'s docstring). No ports are published for this container in
`docker-compose.yml`, so `json` never reaches the public internet regardless.

## 2. Rate-limit hygiene: fewer engines queried per search

Every query SearXNG runs fans out to *every enabled engine* in parallel. Self
hosting removes Tavily's monthly cap, but the traffic doesn't disappear - it
moves to this server's own IP making requests against Google, Bing, Brave and
the rest on our behalf, and heavy use can get that IP temporarily
rate-limited by any of them.

Two upstream defaults already help: `google` and `bing` - the two engines
most aggressive about blocking automated traffic - ship `disabled: true` out
of the box. On top of that, this file disables nine more engines that were
enabled by default but add request volume with no benefit to what this app
actually searches for (opportunity listings - text, not images/video/news):

| Engine | Why disabled |
|---|---|
| `wikidata` | Structured-data lookups, not opportunity listings |
| `wolframalpha_api` | Computational queries; needs an API key we don't have anyway |
| `dogpile` | Itself a metasearch engine - querying it re-exposes us to Google/Yahoo indirectly, defeating the point of trimming the list |
| `startpage news`, `startpage images` | Wrong category entirely |
| `brave.images`, `brave.videos`, `brave.news` | Wrong category entirely |

`yandex api` ships `inactive: true` upstream already and was left as-is.

This is a dial, not a fixed decision - if results feel thin for a particular
profile type, re-enabling an engine here (remove its `disabled: true` line)
is a one-line change, redeployed the same way as any other config change
(`docker compose up -d searxng`, no rebuild needed since it's a mounted
volume, not baked into an image).

## The rest of the mitigation lives in application code, not here

Trimming engines only reduces fan-out *per query*. The larger fix - not
re-asking a question already answered an hour ago - is
`SearchQueryCache` (`models_db.py`) and the caching/pacing/backoff logic in
`discovery.py`'s `discover()` and `pipeline.py`'s cache wiring. Three env
vars tune it without touching code:

- `SEARCH_CACHE_TTL_MINUTES` (default 360 / 6h) - how long a query's results
  are trusted before being asked again.
- `SEARCH_PAUSE_SECONDS` (default 1.0) - the gap between queries that do
  reach the network within one discovery cycle, so a cycle's requests arrive
  spread out rather than in one burst.
- A 429 or 403 from any single query stops the rest of *that* cycle's queries
  outright (not configurable - retrying into a rate limit is never correct).
