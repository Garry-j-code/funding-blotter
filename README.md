# Funding Blotter

A daily funding-round aggregator focused on **fintech, AI fintech, and financial
services**. It scrapes US venture coverage (primarily FinSMEs), extracts
structured deal fields, uses a web-search tool + LLM to keep only finance-relevant
companies, deduplicates rounds, scores NYC matches, and publishes a static HTML
page via GitHub Pages.

**Live page:** [garry-j-code.github.io/funding-blotter](https://garry-j-code.github.io/funding-blotter/)

**Purpose.** Surfacing recently funded NYC AI / fintech companies for job
targeting. Companies that closed a Series A–C in the last few months are often
building ML and data teams before a rigid hiring funnel exists — a good window
for cold outreach.

**Why FinSMEs.** Large funding trackers often drop small or undisclosed rounds.
FinSMEs covers nearly every US round, including seeds and undisclosed amounts.
Its RSS/API endpoints are WAF-blocked; this project scrapes the rendered
[`/category/usa`](https://www.finsmes.com/category/usa) listing and article pages
instead.

---

## Table of contents

1. [Quick start](#quick-start)
2. [Architecture](#architecture)
3. [File reference](#file-reference)
4. [Data contracts](#data-contracts)
5. [Extraction](#extraction)
6. [Sector enrichment](#sector-enrichment)
7. [Dedupe and scoring](#dedupe-and-scoring)
8. [Rendering](#rendering)
9. [Config reference](#config-reference)
10. [CLI reference](#cli-reference)
11. [Deployment](#deployment)
12. [Known limitations](#known-limitations)
13. [Gotchas](#gotchas)

---

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
cp .env.example .env
# Edit .env:
#   GROQ_API_KEY=...     https://console.groq.com  (free tier)
#   TAVILY_API_KEY=...   https://app.tavily.com    (web search)

uv sync

uv run python -m src.main --probe finsmes   # verify the FinSMEs scraper
uv run python -m src.main --dry-run         # fetch + extract + enrich, write nothing
uv run python -m src.main                   # full run
open docs/index.html
```

Dependencies (managed via `pyproject.toml` + `uv.lock`): `feedparser`,
`requests`, `beautifulsoup4`, `PyYAML`, `python-dotenv`.

---

## Architecture

```
config.yaml
    │
    ▼
┌─────────────┐  RawItem[]  ┌─────────────┐  Deal[]  ┌─────────────┐
│ sources.py  │ ──────────► │ extract.py  │ ───────► │ enrich.py   │
│             │             │             │          │             │
│ FinSMEs     │             │ regex ─┐    │          │ web_search  │
│ HTML scrape │             │        ├──► │          │ tool + LLM  │
│ (+ optional │             │ Groq ──┘    │          │ sector keep │
│  HTML srcs) │             └─────────────┘          └──────┬──────┘
└─────────────┘                                             │ Deal[] (filtered)
                                                            ▼
                                                     ┌─────────────┐
                                                     │  store.py   │
                                                     │  SQLite     │
                                                     │  dedupe     │
                                                     │  scoring    │
                                                     └──────┬──────┘
                                                            │
                                                            ▼
                                                     ┌─────────────┐
                                                     │  render.py  │
                                                     │ docs/       │
                                                     │  index.html │
                                                     │ data/       │
                                                     │  deals.csv  │
                                                     └─────────────┘
```

`src/main.py` orchestrates. Each stage takes plain dicts and is independently
callable.

**Run sequence:**

1. `sources.fetch_all` → `RawItem` dicts. Per-source failures are logged and
   skipped, never fatal.
2. If **zero** items from every source, exit 1 **without writing**. A broken
   scraper should leave a stale page, not an empty one.
3. `extract.extract_all` → funding `Deal` dicts (`is_funding == True`).
4. `enrich.enrich_all` → web-search + classify; drop unrelated companies.
   Also purges previously stored unrelated rows.
5. `store.upsert` → insert / merge duplicates / score.
6. `render.write_html` + `write_csv` → `docs/index.html` and `data/deals.csv`.

---

## File reference

| Path | Role |
|---|---|
| `src/sources.py` | Fetch adapters (`finsmes`, `rss`, `html`) |
| `src/extract.py` | Regex fast path + Groq field extraction |
| `src/enrich.py` | `web_search` tool (Tavily) + sector classifier |
| `src/store.py` | SQLite, dedupe, priority scoring |
| `src/render.py` | Self-contained blotter HTML + CSV |
| `src/main.py` | CLI + orchestration |
| `config.yaml` | Sources, models, filters, paths |
| `data/deals.db` | Committed on purpose (pipeline memory across Actions) |
| `.github/workflows/daily.yml` | Scheduled daily build + commit |
| `.env` | Local secrets only (git-ignored) |

---

## Data contracts

### `RawItem` — output of `sources.py`

```python
{
    "source":        str,   # e.g. "finsmes"
    "title":         str,
    "text":          str,   # body, HTML stripped
    "url":           str,
    "published_at":  str,   # "YYYY-MM-DD" — coverage date, not deal close date
    "template_hint": str,   # "finsmes" enables the regex fast path
}
```

### `Deal` — after extract (+ enrich fields)

```python
{
    "company":       str,
    "amount_raw":    str,           # "$7M", "undisclosed", …
    "amount_usd":    float | None,  # None = undisclosed, NEVER zero
    "stage":         str,
    "location":      str,
    "description":   str,
    "investors":     str,
    "is_funding":    bool,
    "extracted_by":  str,           # "regex" or model id
    "sector_label":  str,           # ai_fintech | fintech | financial_services | enabler | unrelated
    "sector_reason": str,
}
```

---

## Extraction

Two paths (Groq free tier is TPM-limited ~6k tokens/min):

1. **Regex fast path** — FinSMEs articles follow a fixed template
   (`Company, a Location-based …, raised $X in Stage funding/financing`).
   Matched per sentence so headlines don’t poison company names.
2. **Groq** — everything the regex misses, in small batches with JSON mode.
   The parser accepts both `{"deals":[…]}` arrays and id-keyed objects.

---

## Sector enrichment

After extraction, each **new** company goes through an agentic step:

1. The LLM is given a `web_search` tool.
2. The tool is executed via **Tavily** (web search API).
3. The model returns one label. Only configured `keep_labels` survive.

| Label | Meaning |
|---|---|
| `ai_fintech` | AI is core **and** primary use case is finance |
| `fintech` | Financial technology product |
| `financial_services` | Bank, broker, asset manager, insurer, etc. |
| `enabler` | Primarily sells to / enables financial institutions |
| `unrelated` | Dropped (biotech, consumer, space, generic enterprise AI, …) |

Classifications are cached in the `company_sector` table so daily runs don’t
re-search companies already seen. Use `--no-enrich` to skip this filter.

---

## Dedupe and scoring

**Dedupe.** Normalize company name → `company_key`. Two rows match if they share
`company_key` + `stage` within `dedupe_window_days` (default 21). Duplicates
gap-fill missing amount / investors / location; the **first** coverage date wins.

**Scoring** (soft ranking on top of the hard sector filter):

```
score    = sector_hits * 2 + location_hits * 3 - deprioritize_hits * 6
priority = 1 if (sector AND location match, no deprioritize hit) else 0
```

Word-boundary matching only. Investors are excluded from the match blob (VC
names like “Nyca” contain “nyc”).

---

## Rendering

`docs/index.html` is self-contained (plus Google Fonts). Deal data is embedded
as JSON; filter / search / day grouping run client-side (`file://` works).

Each row shows location, sector label, investors, and a **via {source}** link to
the original article. Flagged (priority) rows get a red left gutter rail.

---

## Config reference

See `config.yaml`. Highlights:

| Section | Purpose |
|---|---|
| `sources.finsmes` | `kind: finsmes`, category URL, `article_limit` / `article_delay` |
| `sources.aifunding_me` | Optional HTML source (currently unverified / often empty) |
| `extraction` | Groq model, batch size, regex fast path |
| `enrichment` | Tavily, `keep_labels`, search pacing |
| `filters` | NYC / sector keywords for **priority** flagging |
| `output` / `store` | Paths and windows |

Paths resolve relative to the **repo root**, not the working directory.

---

## CLI reference

```bash
uv run python -m src.main                 # full run
uv run python -m src.main --dry-run       # print kept deals, write nothing
uv run python -m src.main --no-llm        # regex only (no Groq)
uv run python -m src.main --no-enrich     # skip sector filter
uv run python -m src.main --probe finsmes # dump raw items from one source
uv run python -m src.main --render-only   # rebuild page from existing DB
uv run python -m src.main -v              # DEBUG logging
```

Exit codes: `0` success, `1` no items fetched (or empty `--probe`).

---

## Deployment

Published at
[garry-j-code.github.io/funding-blotter](https://garry-j-code.github.io/funding-blotter/).

### How it works

1. **GitHub Actions** (`.github/workflows/daily.yml`) runs daily at **03:30 UTC**
   ≈ **11:30 PM New York (EDT)**, or on demand via **Actions → daily blotter →
   Run workflow**.
2. The runner executes `uv run python -m src.main` with repo secrets.
3. If `docs/index.html`, `data/deals.db`, or `data/deals.csv` changed, results
   are committed back to `main`.
4. **GitHub Pages** deploys from branch `main` / folder `/docs`.

### Secrets (repo Settings → Secrets and variables → Actions)

| Secret | Purpose |
|---|---|
| `GROQ_API_KEY` | Extraction + sector classification |
| `TAVILY_API_KEY` | `web_search` tool backend |

Direct link pattern: `https://github.com/<user>/<repo>/settings/secrets/actions`

### Pages setup

Settings → Pages → **Deploy from a branch** → `main` / `/docs`.

---

## Known limitations

- **`published_at` is coverage date**, not necessarily the day the round closed.
  Sources can publish a day late.
- **FinSMEs feeds are blocked**; the HTML category scraper is the workaround and
  can break if the site layout changes.
- **aifunding.me** is enabled but unverified; often returns zero rows.
- **FX rates** in `parse_amount` are approximate (sorting only).
- **Entity resolution** is string normalization only — rebrands can duplicate.
- **Enrichment is strict but not perfect**; borderline “enterprise AI that also
  sells to banks” cases can be mislabeled. Cache entries live in `company_sector`.
- Groq / Tavily free tiers have rate limits; the daily run is paced accordingly.

---

## Gotchas

1. **`amount_usd is None` means undisclosed, never `0`.**
2. Don’t unanchor `FINSMES_RE` or match over `title + text` as one blob.
3. Keyword scoring must stay word-boundary; don’t score on investor names.
4. `_sentences()` must not split on names like `Transient.AI`.
5. The LLM must echo item `id`s — don’t map responses by array order alone.
6. **`docs/` is the Pages publish directory.**
7. **`data/deals.db` is committed on purpose** — do not gitignore it.
8. Config paths resolve against the repo root (`ROOT` in `main.py`).
9. Zero items from all sources aborts the run intentionally.
10. Never commit `.env`. Use GitHub Actions secrets in CI.
