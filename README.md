# Funding Blotter

A daily funding-round aggregator. It pulls venture rounds from FinSMEs,
fintech.global, and aifunding.me, extracts structured fields from the prose,
deduplicates the same round across sources, scores each deal against
sector/location filters, and publishes a single static HTML page via GitHub
Pages.

**Purpose.** Surfacing recently funded NYC AI and fintech companies for job
targeting. Companies that closed a Series A through C in the last three to six
months are building out ML and data infrastructure teams but haven't set up a
rigid hiring funnel yet, so this is the window where cold outreach to a founder
or head of engineering still gets read.

**Why these three sources.** The big AI funding trackers key off deal size and
drop anything below their floor or without a stated number. Two concrete misses
that motivated this project: Obin AI's $7M seed (below the floor) and
Transient.AI's Series A (amount undisclosed, so nothing to sort by). Both were
covered by FinSMEs on the day. FinSMEs is sector-agnostic and covers nearly
every US round including small seeds and undisclosed amounts, which is exactly
the blind spot that matters here.

---

## Table of contents

1. [Quick start](#quick-start)
2. [Architecture](#architecture)
3. [File reference](#file-reference)
4. [Data contracts](#data-contracts)
5. [Extraction](#extraction)
6. [Dedupe and scoring](#dedupe-and-scoring)
7. [Rendering](#rendering)
8. [Config reference](#config-reference)
9. [CLI reference](#cli-reference)
10. [Deployment](#deployment)
11. [Testing](#testing)
12. [Known limitations](#known-limitations)
13. [Extension recipes](#extension-recipes)
14. [Gotchas](#gotchas)

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export GROQ_API_KEY=gsk_...          # console.groq.com, free tier, no card

python -m src.main --probe aifunding_me   # VERIFY THIS SOURCE FIRST
python -m src.main --dry-run              # parse and print, write nothing
python -m src.main                        # full run
open docs/index.html
```

Python 3.11+. Only four dependencies, all pure-Python: `feedparser`,
`requests`, `beautifulsoup4`, `PyYAML`.

---

## Architecture

```
config.yaml
    │
    ▼
┌─────────────┐   RawItem[]   ┌─────────────┐   Deal[]   ┌───────────┐
│ sources.py  │ ────────────► │ extract.py  │ ─────────► │ store.py  │
│             │               │             │            │           │
│ RSS + HTML  │               │ regex ─┐    │            │ SQLite    │
│ + JSON      │               │        ├──► │            │ dedupe    │
│ adapters    │               │ Groq ──┘    │            │ scoring   │
└─────────────┘               └─────────────┘            └─────┬─────┘
                                                               │ Deal[]
                                                               ▼
                                                        ┌─────────────┐
                                                        │  render.py  │
                                                        │             │
                                                        │ docs/       │
                                                        │  index.html │
                                                        │ data/       │
                                                        │  deals.csv  │
                                                        └─────────────┘
```

`main.py` orchestrates. Each stage is independently testable and takes plain
dicts, so you can call any of them from a REPL without the others.

**Run sequence:**

1. `sources.fetch_all(cfg["sources"])` returns `RawItem` dicts. Adapter failures
   are logged and skipped, never fatal.
2. If zero items came back from every source, `main` aborts with exit 1 **without
   writing anything**. This is deliberate: a scraper breakage should show up as a
   stale page, not an empty one.
3. `extract.extract_all(raw, cfg["extraction"])` returns `Deal` dicts, filtered to
   `is_funding == True`.
4. `store.upsert(...)` inserts new rows, merges duplicates, and computes scores.
5. `store.recent(conn, window_days)` reads back the display window.
6. `render.write_html` and `render.write_csv` produce the outputs.

---

## File reference

### `src/sources.py`

Fetch adapters. Every adapter returns the same `RawItem` shape so downstream
code never branches on source.

- `fetch_rss(name, cfg)` — `feedparser` over one or more feed URLs. Handles both
  `summary` and `content[0].value` bodies, strips HTML via BeautifulSoup,
  normalizes dates to ISO from `published_parsed`/`updated_parsed`, dedupes by
  link across multiple feeds of the same source. Used by FinSMEs (which exposes
  `/feed`, `/usa/feed`, and even tag-level feeds like `/tag/<slug>/feed`) and
  fintech.global.
- `fetch_html(name, cfg)` — three-tier fallback for sites without a feed:
  1. Probe configured JSON endpoints (`json_probe` in config).
  2. Look for `<script type="application/json">` blobs, which Next.js and
     similar frameworks use to embed page data. This beats scraping rendered
     markup every time.
  3. Fall back to CSS-selector row scraping.
- `_items_from_json(...)` — recursively walks an arbitrary JSON structure
  looking for deal-shaped dicts (something with both a name-ish key and a
  money-ish key), then maps varied key names onto the common shape. This is
  what makes tier 1 and 2 work without knowing the API schema in advance.
- `fetch_all(sources_cfg, only=None)` — dispatches by `kind`, catches per-adapter
  exceptions. `only` powers `--probe`.

Constants: `UA` (real user agent string), `TIMEOUT` (25s).

### `src/extract.py`

Prose to structured fields. See [Extraction](#extraction) for detail.

Key symbols: `FINSMES_RE`, `LED_BY_RE`, `INV_TAIL_RE`, `AMOUNT_RE`, `MULT`, `FX`,
`STAGE_CANON`, `NON_FUNDING`, `BAD_NAME_RE`, `parse_amount`, `canon_stage`,
`clean_investors`, `_sentences`, `regex_extract`, `SYSTEM_PROMPT`,
`GroqExtractor`, `extract_all`.

### `src/store.py`

SQLite persistence, cross-source dedupe, priority scoring.

Key symbols: `SCHEMA`, `SUFFIXES`, `company_key`, `connect`, `_hits`,
`score_deal`, `upsert`, `recent`.

### `src/render.py`

Single self-contained HTML page plus CSV. Data is embedded as a JSON literal in
a `<script>` tag; all filtering and search runs client-side, so the page needs
no server and works from `file://`.

Key symbols: `CSS`, `JS`, `TEMPLATE`, `write_html`, `write_csv`.

### `src/main.py`

Argparse CLI, logging setup, orchestration. See [CLI reference](#cli-reference).

### `config.yaml`

All tuning. No code changes needed for normal adjustments. See
[Config reference](#config-reference).

### `.github/workflows/daily.yml`

Scheduled build. See [Deployment](#deployment).

---

## Data contracts

### `RawItem` — output of `sources.py`

```python
{
    "source":        str,   # "finsmes" | "fintech_global" | "aifunding_me"
    "title":         str,   # headline, HTML stripped
    "text":          str,   # body/summary, HTML stripped, whitespace collapsed
    "url":           str,
    "published_at":  str,   # "YYYY-MM-DD", falls back to today
    "template_hint": str,   # "finsmes" enables the regex fast path
}
```

### `Deal` — output of `extract.py`

A `RawItem` merged with these keys:

```python
{
    "company":      str,           # no Inc/Ltd/Corp suffixes
    "amount_raw":   str,           # "$7M", "£8.1M", "an undisclosed amount"
    "amount_usd":   float | None,  # None means undisclosed, NOT zero
    "stage":        str,           # canonicalized, see STAGE_CANON
    "location":     str,           # "NYC", "New York, NY", "Abingdon, UK"
    "description":  str,           # <= 280 chars
    "investors":    str,           # comma-separated leads only
    "is_funding":   bool,          # False filters the row out entirely
    "extracted_by": str,           # "regex" or the model id, for debugging
}
```

`amount_usd is None` is load-bearing throughout. Never coerce it to `0` — an
undisclosed Series A is often the most interesting row on the page, and zeroing
it would sort it to the bottom and render it as `$0`.

### Database schema

```sql
CREATE TABLE deals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_key   TEXT NOT NULL,   -- normalized, used for dedupe
    company       TEXT NOT NULL,   -- display name
    amount_usd    REAL,            -- NULL = undisclosed
    amount_raw    TEXT,
    stage         TEXT,
    location      TEXT,
    description   TEXT,
    investors     TEXT,
    source        TEXT,            -- first source to report it
    url           TEXT,
    published_at  TEXT,            -- "YYYY-MM-DD"
    first_seen    TEXT,            -- when this pipeline first saw it
    priority      INTEGER,         -- 1 = sector AND location match
    score         INTEGER,         -- can be negative
    extracted_by  TEXT
);
CREATE INDEX idx_key ON deals(company_key, published_at);
CREATE INDEX idx_pub ON deals(published_at);
```

The DB is committed to git. It's small (a few hundred KB per year) and
committing it is what gives the GitHub Actions run its memory between builds,
since the runner filesystem is ephemeral.

---

## Extraction

Two paths. The split exists because of a hard constraint: Groq's free tier is
capped at roughly **6,000 tokens per minute** and 30 requests per minute, with a
daily ceiling that resets at midnight UTC. TPM is the binding limit, not RPM.

### Path 1: regex fast path

FinSMEs writes every entry to a fixed template:

```
<Company>, a[n] <location>-based <description>, raised <amount> in <Stage> funding.
The round was led by <Investors>, with participation from <Others>.
```

Real examples this was built against:

```
Obin AI, a NYC-based enterprise AI company building an agentic workforce for
financial institutions, raised $7M in Seed funding. The round was led by Motive
Partners with participation from angel investors Dr. Fei-Fei Li and Lukasz Kaiser.

Transient.AI, a NYC-based AI-native investment management platform provider
purpose-built for institutional trading environments, raised an undisclosed
amount in Series A funding. The round was led by NEXT Investors.
```

`FINSMES_RE` captures `company`, `location`, `region`, `description`, `amount`,
`stage`. FinSMEs is the highest-volume source, so handling it locally is where
the token savings live.

**Three non-obvious things in this code:**

1. **Sentence-level matching.** `_sentences()` splits on
   `(?<=[.!?])\s+(?=[A-Z])` and the regex is applied per sentence with
   `.match()`, not `.search()` over the whole blob. Reason: the *headline*
   ("Obin AI Raises $7M in Seed Funding") also matches the template, and an
   unanchored search over `title + text` yields
   `company = "Obin AI Raises $7M in Seed Fun..."`. The split requires
   whitespace and a capital after the period, so names like `Transient.AI`
   survive intact.
2. **`BAD_NAME_RE` guard.** Rejects a match whose company name contains
   `raised`/`funding`/`Series`/`$`, which is the signature of having caught the
   headline instead of the body sentence.
3. **`INV_TAIL_RE`.** Investor lists run on forever. Cutting at
   "with participation" / "alongside" / "joined by" keeps only the leads.
   Without this you get `"Motive Partners with participation from angel
   investors and advisors Dr. Fei-Fei Li..."` in the investors column.

`NON_FUNDING` rejects acquisitions, hires, product launches, and VC firms
closing their own funds. Verified against the real Medium/Superfeedr
acquisition item, which the template would otherwise partially match.

### Path 2: Groq

Everything the regex misses. OpenAI-compatible endpoint at
`https://api.groq.com/openai/v1/chat/completions`.

- **Model:** `llama-3.1-8b-instant` by default. This is templated field
  extraction, not reasoning, so 8B is plenty and it has the most free-tier
  headroom. Swap to `openai/gpt-oss-120b` or `llama-3.3-70b-versatile` in config
  if you see bad parses.
- **Batching:** 8 items per request, each truncated to 700 chars, 6 seconds
  between calls. Tuned to stay under TPM.
- **JSON mode:** `response_format: {"type": "json_object"}`. The prompt also
  forbids markdown fences, and the parser strips them anyway because models
  emit them regardless.
- **Item IDs:** each input carries an `id` and the model must echo it back.
  Never rely on array ordering to map responses to inputs.
- **429 handling:** reads the `retry-after` header and sleeps that long, capped
  at 90s. Exponential backoff for 5xx and network errors. Groq also returns
  `x-ratelimit-remaining-requests` and `x-ratelimit-remaining-tokens` headers if
  you want to add proactive throttling.
- **Unsupported OpenAI fields:** Groq does not support `logprobs`,
  `logit_bias`, `top_logprobs`, or `messages[].name`. Don't add them.

Set `extraction.regex_fastpath: false` to route everything through the model.
Expect several minutes per run on the free tier.

### Currency handling

`parse_amount` converts to a USD-ish float so the amount column sorts across
currencies. `FX = {"$": 1.0, "€": 1.08, "£": 1.27}` is hardcoded and approximate
— fine for sorting and bucketing, wrong for anything analytical. If you ever
need real numbers, pull the rate from an FX API keyed on `published_at`.

---

## Dedupe and scoring

### Dedupe

All three sources cover the same rounds, so without dedupe every deal appears
two or three times.

`company_key()` normalizes the name: lowercase, strip smart quotes, strip TLD
suffixes (`.ai`, `.io`, `.com`), strip corporate and generic suffixes via
`SUFFIXES` (`inc`, `llc`, `ltd`, `labs`, `technologies`, `ai`, `io`, ...), then
remove all non-alphanumerics. `"Obin AI"` and `"Obin, Inc."` both collapse to
`obin`.

Two rows match if they share `company_key` **and** `stage` **and** their
`published_at` values are within `dedupe_window_days` (default 21) of each
other. Stage is part of the key so a company raising a Seed and then a Series A
within the window still produces two rows.

**Gap filling.** When a duplicate arrives, it isn't discarded. If the stored row
is missing an amount, investors, or location and the new one has it, those
fields are filled in. This is the point: FinSMEs might report an undisclosed
amount and fintech.global might name a figure two days later.

### Scoring

`score_deal()` matches config keywords against `company + description +
location`, lowercased.

```
score    = sector_hits * 2 + location_hits * 3 - deprioritize_hits * 6
priority = 1 if (sector_hits and location_hits and not deprioritize_hits) else 0
```

**Investors are deliberately excluded from the match blob.** VC firm names are
full of city and sector words and would flag every round they touch — "Nyca
Partners" contains "nyc".

**Matching is word-boundary, not substring** (`_hits` uses
`\b{escaped_keyword}\b`). Substring matching produced exactly that Nyca false
positive, and would also fire "risk" inside "brisk".

Deprioritized rounds are **stored and rendered, just dimmed**. Nothing silently
disappears. Biotech is the bulk of FinSMEs' volume and almost never relevant
here, but the failure mode of a hard filter is invisible, and an invisible
failure mode in a tool you check daily is worse than scrolling past a few rows.

---

## Rendering

`docs/index.html` is fully self-contained apart from two Google Fonts. Data is
embedded as a JSON array; filtering, search, and grouping run client-side.

### Design

The page is a **trade blotter**, not a dashboard. The visual concept is
double-entry bookkeeping on ledger paper: green ink for money, red ink for the
items flagged for attention.

Design tokens (CSS custom properties on `:root`):

| Token | Value | Use |
|---|---|---|
| `--ledger` | `#ECEEE8` | page ground, cool pale ledger paper |
| `--card` | `#F6F7F3` | row hover, input fields |
| `--ink` | `#171A16` | primary text |
| `--rule` | `#CBCFC5` | hairlines, unflagged gutter rail |
| `--muted` | `#6B7267` | secondary text, metadata |
| `--credit` | `#0C5A3A` | **dollar amounts only** |
| `--flag` | `#A8380F` | **flagged gutter rail and stage label only** |

Type: `Newsreader` (serif) for company names and descriptions, `IBM Plex Mono`
for all figures, labels, and metadata. Amounts use
`font-variant-numeric: tabular-nums` so the column aligns vertically.

**Signature element:** the 3px left gutter rail on every row. Hairline by
default, `--flag` when `priority == 1`. It's the one place boldness is spent;
everything else stays quiet.

Layout is a 4-column CSS grid (`3px 1fr 108px 92px`) collapsing to 2 columns
under 720px. Rows animate in with a staggered fade capped at 260ms, wrapped in
`@media (prefers-reduced-motion: no-preference)`. Focus rings are visible on
all interactive elements.

### Client-side behavior

Filter chips: `all`, `priority` (flagged only), `early` (Seed and Series A),
`disclosed` (amount known). Free-text search across company, description,
investors, location, and stage, debounced 120ms. Rows group by `published_at`,
newest first, with a per-day count and flagged count.

`fmt()` renders amounts as `$7M`, `$10.3M`, `$2.4B`, `$750K`, and `n/d` for
undisclosed. `trim()` strips trailing zeros so you get `$2.4B` rather than
`$2.40B`.

All user-controlled strings pass through `esc()` before interpolation.

---

## Config reference

### `sources.<name>`

| Key | Type | Meaning |
|---|---|---|
| `enabled` | bool | Skip this source entirely when false |
| `kind` | `rss` \| `html` | Which adapter to dispatch to |
| `urls` | list | Feed or page URLs, fetched in order |
| `json_probe` | list | (html only) JSON endpoints to try before scraping |
| `selectors.row` | CSS | (html only) selector for one deal row |
| `selectors.link` | CSS | (html only) anchor within a row |
| `template_hint` | `finsmes` \| `generic` | `finsmes` enables the regex fast path |

### `extraction`

| Key | Default | Meaning |
|---|---|---|
| `provider` | `groq` | Informational; only Groq is implemented |
| `base_url` | `https://api.groq.com/openai/v1` | OpenAI-compatible base |
| `model` | `llama-3.1-8b-instant` | Model id |
| `batch_size` | `8` | Items per request |
| `chars_per_item` | `700` | Truncation before sending |
| `seconds_between_calls` | `6` | Pacing for TPM limits |
| `max_retries` | `4` | Per request |
| `regex_fastpath` | `true` | Parse FinSMEs locally, skip the LLM |

### `filters`

| Key | Meaning |
|---|---|
| `priority_sectors` | Sector keywords. Currently 32, tuned to capital markets and AI infrastructure |
| `priority_locations` | Location keywords. Currently NYC-focused |
| `deprioritize` | Dims and sinks rather than deletes. Biotech, pharma, real estate, etc. |

### `output` / `store`

| Key | Default | Meaning |
|---|---|---|
| `output.html_path` | `docs/index.html` | Must stay under `docs/` for GitHub Pages |
| `output.csv_path` | `data/deals.csv` | |
| `output.window_days` | `30` | How much history the page shows |
| `store.db_path` | `data/deals.db` | |
| `store.dedupe_window_days` | `21` | Cross-source merge window |

All paths are resolved relative to the repo root, not the working directory.

---

## CLI reference

| Command | Effect |
|---|---|
| `python -m src.main` | Full run: fetch, extract, store, render |
| `python -m src.main --dry-run` | Fetch and extract, print a table, write nothing |
| `python -m src.main --no-llm` | Regex only, zero API calls, zero cost |
| `python -m src.main --probe <source>` | Print raw items from one source and exit |
| `python -m src.main --render-only` | Rebuild the page from the existing DB, no network |
| `python -m src.main --config path` | Alternate config file |
| `python -m src.main -v` | DEBUG logging |

Exit codes: `0` success, `1` no items fetched (or `--probe` found nothing).

`--probe` is the debugging entry point. When a source breaks, run it first — it
shows you exactly what came back before any parsing.

---

## Deployment

GitHub Actions on a cron, publishing to GitHub Pages.

**Setup:**

1. Push to a GitHub repo.
2. Settings → Secrets and variables → Actions → new secret `GROQ_API_KEY`.
3. Settings → Pages → Source: *Deploy from a branch* → `main` / `/docs`.
4. Actions tab → run the workflow manually once to confirm before trusting the
   schedule.

Page lands at `https://<user>.github.io/<repo>/`. The repo must be public for
that URL to work without auth.

**Workflow notes:**

- Cron is `30 1 * * *` UTC = 21:30 New York during EDT, 20:30 during EST.
  **GitHub cron is always UTC and has no timezone option.** Scheduled runs are
  also frequently delayed several minutes under load, and GitHub disables
  schedules on repos with no activity for 60 days.
- `permissions: contents: write` is required to commit results back.
- `workflow_dispatch` is included so you can trigger runs by hand.
- The commit step checks `git diff --staged --quiet` and skips the commit
  entirely when nothing changed, so you don't get empty commits on quiet days.
- `concurrency: group: blotter` with `cancel-in-progress: false` prevents a
  manual run and a scheduled run from racing on the same DB.

---

## Testing

There is no test suite yet. Development was done against a fixture file of real
captured source copy, which is the right pattern to keep: the failure modes here
are all parsing failures, and parsing failures only reproduce against real text.

Cases verified during the initial build:

| Case | Expected | Status |
|---|---|---|
| Obin AI $7M seed | `$7M / Seed / NYC / Motive Partners`, flagged | pass |
| Transient.AI undisclosed Series A | `n/d`, flagged, not dropped | pass |
| Same round from two sources, 1 day apart | merged to one row | pass |
| Medium/Superfeedr acquisition | rejected, `is_funding` false | pass |
| Luffy AI £8.1M | converted to `$10.3M` | pass |
| Trace Biosciences (biotech) | stored, `score < 0`, dimmed | pass |
| Provable Markets, securities finance, NYC | flagged | pass |
| Amount formatting `2.4e9` | `$2.4B` not `$2.40B` | pass |
| All sources return 403 | abort, page left intact | pass |

**Suggested first test-suite move:** save a handful of real RSS entries as
fixtures under `tests/fixtures/`, and assert on `regex_extract` and
`company_key` outputs. Those two functions carry almost all the parsing risk and
neither needs network or an API key.

---

## Known limitations

- **aifunding.me is unverified.** It has no confirmed feed. The adapter's JSON
  probes and CSS selectors in `config.yaml` are educated guesses. Run
  `--probe aifunding_me` and fix the selectors against what it actually returns.
  The two RSS sources are low-risk by comparison.
- **FX rates are hardcoded and approximate.** Fine for sorting, wrong for
  analysis.
- **No entity resolution beyond string normalization.** A company that rebrands,
  or is written as "Acme" in one source and "Acme Financial Technologies" in
  another, will produce two rows. `SUFFIXES` catches the common cases.
- **`amount_usd` is derived from the reported figure only.** Undisclosed stays
  undisclosed; nothing is estimated.
- **Single-threaded and deliberately slow.** One request per feed per day, real
  user agent, no concurrency. Prefer RSS over HTML scraping wherever a feed
  exists.
- **The Groq free tier has a daily request ceiling that resets midnight UTC.**
  A daily run of this size is comfortably inside it, but a debugging session
  with `regex_fastpath: false` can burn through it.

---

## Extension recipes

### Add a source

1. Add a block under `sources:` in `config.yaml` with `kind: rss` or
   `kind: html`.
2. If it needs different fetch logic, write `fetch_<kind>(name, cfg)` in
   `sources.py` returning `RawItem` dicts and register it in the `FETCHERS`
   dict.
3. `--probe <name>` to verify.

No other file needs to change. Extraction, dedupe, scoring, and rendering are
all source-agnostic.

### Add SEC Form D as a source

The highest-value addition. Form D filings appear on EDGAR before press
coverage, typically two to six weeks earlier, and they carry the **actual
amount for rounds the press reports as undisclosed** — which would directly
close the Transient.AI gap.

EDGAR full-text search has a JSON API at
`https://efts.sec.gov/LATEST/search-index?q=...&forms=D`. Requires a real
`User-Agent` with contact info per SEC policy. Form D is XML with structured
`totalAmountSold` and `totalOfferingAmount` fields, so this would be a pure
parse with no LLM involvement, and it would merge into existing rows through
the normal gap-filling path in `store.upsert`.

### Add investor-side tracking

Watching the funds is higher-signal than watching the companies for this use
case. Motive Partners, Nyca, QED, Bain Capital Ventures, FinTech Collective, and
NEXT Investors are the relevant check-writers here. Their portfolio pages update
on announcement. An adapter per fund page, or a `priority_investors` list in
config that adds to the score, would both work.

### Add an email digest

`render.py` already produces the deal list. Add a `notify.py` that takes
`store.recent(conn, 1)`, filters to `priority == 1`, and posts to a webhook or
sends via SMTP. Wire it as a final step in `main.py` and add the credential as
another Actions secret.

### Add a jobs cross-reference

The actual goal of the project is finding companies that are hiring. Flagged
companies could be checked against their careers pages or a jobs API, with the
result rendered as an extra column. This is the natural next feature.

---

## Gotchas

Things that will bite whoever edits this next.

1. **`amount_usd is None` means undisclosed, never zero.** Coercing it breaks
   sorting, breaks the `disclosed` filter, and renders `$0` on the page.
2. **Don't unanchor `FINSMES_RE` or apply it to `title + text` as one blob.**
   The headline matches the same template and wins, producing garbage company
   names. Match per sentence with `.match()`.
3. **Keyword matching must stay word-boundary.** Substring matching flags
   "Nyca Partners" as New York.
4. **Don't add investors to the scoring blob.** Same reason.
5. **`_sentences()` must not split on `Transient.AI`.** The lookahead requiring
   whitespace plus a capital is what prevents that. Don't simplify it to
   splitting on `.`.
6. **The LLM must echo back the item `id`.** Never map responses to inputs by
   array position.
7. **`docs/` is the Pages publish directory.** Moving `index.html` elsewhere
   silently breaks deployment.
8. **The SQLite DB is committed to git on purpose.** It's the pipeline's memory
   across ephemeral Actions runners. Don't add it to `.gitignore`.
9. **Config paths resolve against the repo root** (`ROOT` in `main.py`), not
   `os.getcwd()`.
10. **A source returning zero items aborts the run.** That's intentional. If you
    "fix" it to continue, a broken scraper silently publishes an empty page.
