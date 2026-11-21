# AGENTS.md

Guidance for AI coding agents working in this repo. For full detail see
[README.md](README.md); this file is the short, operational version.

## What this project is

**Funding Blotter** — a daily funding-round aggregator for fintech / AI fintech /
financial services. It scrapes FinSMEs, extracts deal fields (regex + Groq LLM),
filters by sector using an LLM with a Tavily `web_search` tool, dedupes and scores
NYC matches, and serves the result.

Two halves:

- **Python pipeline** (`src/`) — fetch → extract → enrich → store → render.
- **Web app** — Vite/React SPA (`web/`) + Netlify Functions (`netlify/functions/`)
  reading from Supabase.

## Layout

| Path | Role |
|---|---|
| `src/main.py` | CLI + orchestration (`ROOT` = repo root; config paths resolve against it) |
| `src/sources.py` | Fetch adapters (`finsmes`, `rss`, `html`) |
| `src/extract.py` | Regex fast path + Groq extraction |
| `src/enrich.py` | Tavily `web_search` tool + sector classifier |
| `src/backend.py` | `Backend` protocol; `open_backend("sqlite" \| "supabase")` |
| `src/store.py` | SQLite backend: dedupe, scoring |
| `src/supabase_store.py` | Supabase (Postgres) backend — production |
| `src/render.py` | Legacy static `docs/index.html` + `data/deals.csv` |
| `src/backfill.py` | Recover a date range of missed days |
| `web/src/` | React SPA (`App.tsx`, `api.ts`, `types.ts`, `blotter.css`) |
| `netlify/functions/` | `deals.ts`, `fetch.ts`, `health.ts`, shared `_lib.ts` |
| `supabase/migrations/` | Postgres schema |
| `config.yaml` | Sources, models, filters, paths |
| `docs/` | Setup guides plus legacy GitHub Pages output |

## Commands

Python (needs [uv](https://docs.astral.sh/uv/), Python 3.11+; CI pins 3.13):

```bash
uv sync
uv run python -m src.main --probe finsmes                        # check the scraper
uv run python -m src.main --dry-run --backend sqlite             # no writes
uv run python -m src.main --backend sqlite --render-only         # rebuild local page
uv run python -m src.main --backend supabase                     # production run
uv run python -m src.backfill --from YYYY-MM-DD --to YYYY-MM-DD  # recover missed days
```

Other useful flags: `--no-llm` (regex only), `--no-enrich` (skip sector filter),
`-v` (debug logs).

Frontend / functions:

```bash
npm ci && npm --prefix web ci
npm --prefix web run dev          # Vite dev server
npm --prefix web run build        # output: web/dist
```

There is **no test suite**. Verify changes with `--probe`, `--dry-run`, and
`--no-llm` before running anything that writes or spends API quota. For TS
changes, run `npx tsc -p tsconfig.functions.json --noEmit` and build `web/`.

## Browser / UI testing

- **Never run a browser by yourself.** Do not launch any browser automation (no
  Claude-in-Chrome / `mcp__claude-in-chrome__*` tools, no Playwright runs) unless
  the user has explicitly granted it in chat. Ask first and wait for a clear yes. An
  earlier offer that went unanswered is not permission.
- **Don't use Chrome automation at all in this project.** UI testing here is done
  with **Playwright**. Playwright is not set up in the repo yet; ask before
  installing it or adding test files.
- Until the user grants a browser run, verify UI changes with typechecks
  (`npx tsc --noEmit -p web`), `curl` against the local API, and checking that the
  Vite dev server compiles, then say what was not verified visually.

## Rules that must not be broken

These are load-bearing invariants (see README "Gotchas"):

1. `amount_usd is None` means **undisclosed** — never use `0`.
2. Don't unanchor `FINSMES_RE`, and don't regex over `title + text` as one blob.
3. Keyword scoring is **word-boundary only**, and investor names are excluded from
   the match blob (e.g. "Nyca" contains "nyc").
4. `_sentences()` must not split on names like `Transient.AI`.
5. The LLM must echo item `id`s — never map responses by array position.
6. Zero items from all sources **aborts the run without writing** (exit 1). A stale
   page is better than an empty one. Don't "fix" this.
7. Per-source fetch failures are logged and skipped, never fatal.
8. Both backends must stay in sync: any change to the `Backend` protocol in
   `src/backend.py` needs matching changes in `store.py` **and**
   `supabase_store.py`.
9. Preserve the amount filter behavior: undisclosed-amount deals stay visible
   under the Over/Under $5M blotter filters.

## Secrets and safety

- Never commit `.env`; never print or log key values. Use `.env.example` for new
  variables and GitHub Actions / Netlify secrets for deployed ones.
- `SUPABASE_SERVICE_ROLE_KEY` is server-side only — never expose it to the SPA
  (`web/`) or ship it in client code.
- `DELETE /api/deals` and `POST /api/fetch` are guarded by `ADMIN_SECRET`; keep
  them guarded.
- Real runs spend Groq and Tavily free-tier quota and write to the production
  Supabase DB. Prefer `--dry-run` / `--backend sqlite` unless asked otherwise.
- Don't commit `logs.txt` (untracked scratch output).

## CI / deploy

- `.github/workflows/daily.yml` is **manual-trigger only** (`workflow_dispatch`)
  and runs on **self-hosted** Mac/Windows runners — FinSMEs returns 403 to
  GitHub's cloud IPs. Don't switch it to `ubuntu-latest`.
- The workflow runs bash steps; on Windows it relies on Git Bash being on PATH.
  Be careful with YAML indentation around PowerShell here-strings (this has broken
  the file before).
- Netlify builds with `npm ci && npm --prefix web ci && npm --prefix web run build`
  and publishes `web/dist`; the SPA fallback redirect must not catch `/api/*`.
- Setup docs: `docs/DEPLOY_NETLIFY.md`, `docs/SETUP_RUNNER.md`.

## Known doc drift

README.md still describes the older GitHub Pages + SQLite setup in places. In
particular, it says `data/deals.db` is committed on purpose, but `.gitignore` now
excludes it because production uses Supabase. Trust the code, `.gitignore`, and
`daily.yml` over the README where they disagree, and update the README if you touch
those areas.

## Style

- Python: type hints, `from __future__ import annotations`, stdlib `logging`, small
  stage functions that take and return plain dicts.
- Keep changes minimal and match surrounding code; don't add dependencies without a
  reason (update `pyproject.toml` and `uv.lock` together; `requirements.txt` is a
  legacy subset).
- Commit messages: short imperative sentence, as in the existing history.
