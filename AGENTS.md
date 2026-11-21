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
| `src/jobs.py` | Careers: find each company's public job board, sync matching open roles (Supabase only) |
| `netlify/functions/careers.ts` | `GET /api/careers`; admin `POST /api/careers`, `PUT` / `DELETE /api/careers/:company_key` |
| `netlify/functions/careers-run.ts`, `_github.ts` | Admin `GET`/`POST /api/careers-run`: latest run of `jobs.yml` and the Jobs page **Run now** button (uses `GITHUB_PAT`; the workflow file name is fixed in code) |
| `web/src/Careers.tsx` | The "Careers" panel on each card |
| `web/src/Jobs.tsx` | The Jobs page (`#/jobs`): all open roles, plus the admin "add a careers URL" form |
| `tests/` | Offline pytest suite for `src/jobs.py` (no network, no database) |
| `.github/workflows/jobs.yml` | Daily careers sync (public APIs, normal GitHub runner) |
| `web/src/` | React SPA (`App.tsx`, `api.ts`, `types.ts`, `blotter.css`) |
| `netlify/functions/` | `deals.ts`, `fetch.ts`, `health.ts`, shared `_lib.ts` |
| `config.yaml` | Sources, models, filters, paths |
| `docs/` | Setup guides plus legacy GitHub Pages output |

**Database schema:** the Supabase tables were created by hand (SQL editor) and the migration files are not kept in
the repo. Column names are in `src/supabase_store.py`; the careers tables are documented in
`docs/PLAN_CAREERS.md` section 3. The original `deals` schema is in git history
(`git show f6e7c52:supabase/migrations/001_initial.sql`).

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

Careers code has an offline test suite: `uv run pytest -q`. The rest of the pipeline has
no tests, so verify changes with `--probe`, `--dry-run`, and `--no-llm` before running
anything that writes or spends API quota. Careers dry run: `uv run python -m src.jobs
--dry-run --report /tmp/careers.csv` (writes nothing). For TS
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
10. **Careers: a failed fetch never closes jobs.** Only a successful fetch may close
    roles, and a board that had open roles and returns nothing is held for one run first.
11. **Careers: a URL with `source = manual` is never overwritten by discovery.**
12. **Careers: unverified guesses are admin-only.** `GET /api/careers` must never send
    them to visitors. Admin writes need `ADMIN_SECRET`, and URLs are validated
    (http/https only, no credentials). Keep `_careers.ts` and `jobs.py` URL logic in step.
13. Careers roles come from the public job-board APIs (Ashby, Greenhouse, Lever, Workable).
    The only other thing read is the one careers URL an admin saved for a company: it is
    fetched once per run (public hosts only, robots.txt respected) and used only to find a
    linked job board or schema.org `JobPosting` data. No LinkedIn or other scraping, no LLM calls.
14. **Jobs page companies:** an admin can add a company that is not in the blotter (a manual
    `company_careers` row with no matching deal). `company_key` must come from the same rule in
    `src/store.py` and `_careers.ts` (`tests/fixtures/company_keys.json` holds shared examples).
    A name whose key already exists in the blotter is rejected (409), never merged.
15. Page every Supabase list query (1000-row cap). `company_key` strips words like
    "Labs", so two companies can share a key (ORO Labs vs Oro); verify before trusting.

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

- `.github/workflows/jobs.yml` is the exception: it runs **daily on `ubuntu-latest`** because
  the job-board APIs are public. Scheduled workflows are disabled after 60 days without
  repository activity (public repos), so any commit resets that.
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
