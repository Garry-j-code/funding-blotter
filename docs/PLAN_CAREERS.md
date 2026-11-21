# Plan: open roles on every company card

Status: **plan only, nothing built yet.** Written 2026-11-21.

## 1. Goal

For every company already in the blotter, find its job board, pull the open roles
that match **Forward Deployed Engineer, AI Engineer, Product Engineer, AI Product
Engineer**, and show them on the company's card. The careers URL is visible to
everyone and editable by the admin. A daily automation keeps it fresh.

### Decisions already made (from our conversation)
- Match the four titles **plus close variants** (see section 5.3).
- Unverified board guesses are visible **to the admin only**, tagged "unverified".
- A separate Jobs page is **out of scope for now** ("after this task").
- Data comes from the **public job-board APIs** of Ashby, Greenhouse, Lever and
  Workable. No scraping, no AI model, no Groq/Tavily tokens.

### Out of scope (v1)
- Custom career pages (no public API). The URL is stored and shown, roles are not fetched.
- LinkedIn and other aggregators.
- A cross-company Jobs page.
- A "Refresh now" button in the UI (v2, needs `GITHUB_PAT` on Netlify).

## 2. Architecture

```
 daily GitHub Action (jobs.yml, ubuntu-latest, cron)
        │  uv run python -m src.jobs
        ▼
 src/jobs.py ──► public ATS APIs (Ashby / Greenhouse / Lever / Workable)
        │
        ▼
 Supabase: company_careers, jobs          (new tables, RLS on)
        ▲
        │ service-role key
 Netlify Functions: GET /api/careers, PUT /api/careers/:company_key (admin)
        ▲
        │
 React SPA: "Careers" option on each DealRow
```

## 3. Data model (new migration `supabase/migrations/002_careers.sql`)

```sql
CREATE TABLE IF NOT EXISTS company_careers (
    company_key         TEXT PRIMARY KEY,         -- same key as deals.company_key
    company             TEXT,                     -- display name at discovery time
    careers_url         TEXT,                     -- what users see; admin editable
    ats                 TEXT,                     -- ashby|greenhouse|lever|workable|custom|none
    ats_slug            TEXT,                     -- board token for the ATS API
    source              TEXT NOT NULL DEFAULT 'auto',   -- auto | manual (manual is never overwritten)
    verified            BOOLEAN NOT NULL DEFAULT FALSE,
    status              TEXT NOT NULL DEFAULT 'pending',-- pending|ok|no_board|unverified|error|unsupported
    needs_refresh       BOOLEAN NOT NULL DEFAULT FALSE, -- set when admin edits the URL
    consecutive_errors  INT NOT NULL DEFAULT 0,
    last_checked        TIMESTAMPTZ,
    last_ok             TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS jobs (
    id            BIGSERIAL PRIMARY KEY,
    company_key   TEXT NOT NULL REFERENCES company_careers(company_key) ON DELETE CASCADE,
    external_id   TEXT NOT NULL,                  -- the ATS's own posting id
    title         TEXT NOT NULL,
    role_type     TEXT NOT NULL,                  -- primary match (see 5.3)
    tags          TEXT[] NOT NULL DEFAULT '{}',   -- every role type it matched
    department    TEXT,
    location      TEXT,
    employment_type TEXT,
    url           TEXT NOT NULL,
    posted_at     TIMESTAMPTZ,
    first_seen    DATE NOT NULL,
    last_seen     DATE NOT NULL,
    closed_at     DATE,
    is_open       BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (company_key, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_company_open ON jobs (company_key) WHERE is_open;

ALTER TABLE company_careers ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
```

**You run this once in the Supabase SQL editor** (I cannot create tables from here).
Only new tables are added; nothing existing changes.

## 4. Where the work lives

| Piece | Files |
|---|---|
| Migration | `supabase/migrations/002_careers.sql` |
| Backend | `src/jobs.py` (CLI + logic), `src/supabase_store.py` (new methods), `config.yaml` (`jobs:` section) |
| API | `netlify/functions/careers.ts`, `_lib.ts` (PUT in CORS, URL validator) |
| UI | `web/src/Careers.tsx` (new), `App.tsx`, `api.ts`, `types.ts`, `blotter.css` |
| Automation | `.github/workflows/jobs.yml` |
| Tests | `tests/test_jobs.py`, `tests/fixtures/*.json`, `pytest` as a dev dependency |
| Docs | `AGENTS.md`, `README.md` (short pointer), `docs/RUN_LOCALLY.md` |

## 5. Backend design (`src/jobs.py`)

CLI: `uv run python -m src.jobs [--company-key KEY] [--discover-only] [--dry-run] [--limit N] [--backend supabase]`

### 5.1 Which companies
Every distinct `company_key` in `deals` (kept companies only, ~450 today). New
companies from the daily deal pipeline are picked up automatically on the next run.

### 5.2 Finding the board (discovery)
1. Candidate board names from the company name: strip "(fka ...)", Inc/LLC/Labs/
   Technologies, try hyphenated and joined forms, and a version without a trailing "AI".
   At most 6 candidates.
2. For each candidate try Ashby, Greenhouse, Lever, Workable (public GET endpoints).
3. **Verify** the board belongs to the company: the board/company name equals
   the company, or the company name appears in the postings' text. Names under 5
   characters (e.g. "De", "Aqua") need an exact board-name match, otherwise they
   stay `unverified` and are admin-only.
4. Save `careers_url` (the human page, e.g. `https://jobs.ashbyhq.com/hanover-park`),
   `ats`, `ats_slug`, `verified`, `status`.
5. Companies with no board get `status = no_board` and are **retried every 7 days**
   (capped per run), so new boards are picked up.
6. **Manual URLs win.** If `source = manual`, discovery never overwrites it.
   A pasted URL on a supported system (`jobs.ashbyhq.com/x`, `boards.greenhouse.io/x`,
   `job-boards.greenhouse.io/x`, `jobs.lever.co/x`, `apply.workable.com/x`) is
   parsed into `ats` + `ats_slug`. Any other URL is stored with `status = unsupported`.

### 5.3 Matching roles (configurable in `config.yaml`)
| role_type | Matches (case-insensitive) |
|---|---|
| `forward_deployed_engineer` | "Forward Deployed Engineer/Engineering/AI Engineer", "FDE" |
| `ai_product_engineer` | titles with "Product Engineer" **and** a standalone "AI" |
| `product_engineer` | "Product Engineer" incl. Founding/Senior/Staff |
| `ai_engineer` | "AI Engineer", "Applied AI", "AI/ML Engineer", "Machine Learning Engineer", "ML Engineer", "LLM Engineer", "GenAI Engineer", "Software Engineer, AI", "Software Engineer (AI-Native)" |

Rules: a title needs engineer/engineering/developer in it (except FDE). Titles with
manager, director, head of, recruiter, sales, account executive, marketing are excluded.
Primary `role_type` precedence is FDE > AI Product > Product > AI Engineer; `tags`
keeps every match. All seniority levels and interns are included. Location is stored
as text only; filtering by NYC or remote is a later UI feature.

### 5.4 Keeping jobs in sync (per company, per run)
- Key is `(company_key, external_id)`.
- Matching jobs found: insert (`first_seen = today`) or update (`last_seen = today`).
  A previously closed job that reappears is reopened.
- Open jobs missing from a **successful** fetch: `is_open = false`, `closed_at = today`.
- **A failed fetch never closes jobs.**
- **Zero-result guard:** if a board that had 5+ open matching jobs suddenly returns
  an empty list, wait for a second run before closing them (protects against a bad
  API response).
- "New" badge = `first_seen` within the last 7 days (computed at read time).

### 5.5 Reliability and politeness
- 4 concurrent workers, ~0.1s pause per request, retry 429/5xx up to 3 times with backoff.
- A generic `User-Agent`.
- Errors are logged per company, `consecutive_errors` is tracked, and the run exits
  non-zero if more than 20% of companies fail, so the Action turns red.
- Every Supabase list query is **paginated** (the 1,000-row cap bit us before).
- Dry-run mode prints what would change and writes nothing.

## 6. API (Netlify Functions, `careers.ts`)

- `GET /api/careers` returns `{ company_key: { careers_url, ats, status, verified, jobs: [...] } }`.
  - **Public:** only verified entries that have a URL. Unverified guesses are never sent.
  - **Admin** (valid `Authorization: Bearer ADMIN_SECRET`): also unverified entries and status details.
- `PUT /api/careers/:company_key` (**admin only**): body `{ careers_url }` or `{ accept: true }`
  (accept an unverified guess).
  - Validation: `http`/`https` only, max 500 chars, no embedded credentials, no `javascript:` etc.
  - Saves with `source = manual`, `verified = true`, `needs_refresh = true`.
  - The next daily run (or a manual workflow run) refreshes that company first.
- CORS: add `PUT` to the allowed methods in `_lib.ts`.
- v2: `POST /api/careers/refresh` dispatching `jobs.yml` (same pattern as `fetch.ts`).

## 7. UI (cards)

- Each `DealRow` gets a **Careers** option, shown as `Careers · 2` when matching roles exist.
- Clicking toggles an inline panel (`aria-expanded`, keyboard accessible):
  - The careers URL as a link with a copy button, plus "checked today / 3 days ago".
  - The open roles: title, location, a role-type chip, a "new" badge, posted date, link.
  - Empty states: "No careers page found yet", "Careers page saved (roles are not fetched
    automatically for this site)", "No matching open roles right now".
- **Admin only:** an input prefilled with the URL, a Save button, an "Unverified guess,
  Accept / Edit" row, and the status.
- Data loads with **one extra request** (`/api/careers`) at page load, merged by
  `company_key`. Saving updates the local state immediately.
- React escapes text, links use `rel="noopener noreferrer"`, and only `http(s)` URLs are rendered as links.
- Mobile layout follows the existing card styles.

## 8. Automation (`.github/workflows/jobs.yml`)

- Triggers: `schedule` (daily, default **11:00 UTC**) and `workflow_dispatch`
  (optional inputs: `company_key`, `discover_only`).
- `runs-on: ubuntu-latest`. The job APIs are public, so no self-hosted runner is needed.
- Steps: checkout, setup-uv, `uv sync --frozen --python 3.13`, check the Supabase
  secrets (already set), `uv run python -m src.jobs`.
- `concurrency: group: careers`, `permissions: contents: read`.
- Caveats: GitHub schedules can start late, and if the repo is public, scheduled
  workflows are disabled after 60 days without repository activity (any commit resets it).

## 9. Security
- Admin-only writes via `ADMIN_SECRET` (same mechanism as Remove).
- Strict URL validation on the server.
- Public API never exposes unverified guesses.
- The Supabase service-role key stays server-side (Actions secrets and Netlify env only).
- New tables have RLS enabled, like the others.

## 10. Testing and verification

No test suite exists today, so this adds a small one (`pytest` as a dev dependency).

**Offline unit tests** (no network, no database), using saved real API responses as fixtures:
- Title matching: positives, negatives, precedence, the excluded words.
- Board-name candidates, URL parsing for all supported systems, URL validation.
- Verification, including the short-name rule.
- The sync logic with a fake store: new, still open, closed, reopened, fetch-error
  (jobs untouched), zero-result guard, manual URL never overwritten.

**Live checks, in order**
1. `--dry-run` across all companies: review a report (company, board, verified, matches). Nothing is written.
2. Real run, then count rows and spot-check about 10 companies against their live boards.
3. Netlify functions: typecheck, then local `curl` for GET and PUT (admin and non-admin).
4. UI: typecheck and build. A visual check only after you explicitly grant a browser
   run (Playwright), per `AGENTS.md`.

## 11. Delivery phases

| # | Phase | Output | Your checkpoint |
|---|---|---|---|
| 0 | Prep | Commit the existing uncommitted work first (recommended) and branch `feature/careers` | OK to commit? |
| 1 | Schema | `002_careers.sql` | **You run it in the Supabase SQL editor** |
| 2 | Backend + tests | `src/jobs.py`, store methods, config, tests passing | Review the matching list |
| 3 | First data | Dry-run report, then the real run over ~450 companies | Approve the dry-run before any write |
| 4 | API | `careers.ts`, admin PUT | Local curl results |
| 5 | UI | Careers panel on cards | Look at it, or grant a Playwright run |
| 6 | Automation | `jobs.yml`, one manual workflow run to prove it | Check the run in Actions |
| 7 | Docs | `AGENTS.md`, `README.md`, `RUN_LOCALLY.md` | Final review and commit |

## 12. Risks and mitigations
| Risk | Mitigation |
|---|---|
| A board that belongs to a different company with a similar name | Verification step; unverified stays admin-only; admin can fix the URL |
| Companies sharing a `company_key` after name normalisation (like ORO Labs / Oro) | Display name stored; verification uses the name; admin override |
| Title false positives (e.g. "AI" in unrelated titles) | Require engineer/developer, exclusion words, tests, config-editable patterns |
| API outage or bad response wiping jobs | Failed fetch never closes jobs; zero-result guard |
| Roughly 40% of companies have no supported board | Clear empty state; admin can paste a URL; extra job systems can be added later |
| GitHub cron delay or 60-day auto-disable | Documented; any commit resets the clock; manual `workflow_dispatch` always works |
| Terms of use | Only public job-board APIs are used, with a polite request rate |

## 13. Small open items (defaults noted)
1. Commit existing work before starting: **default yes**, in logical commits.
2. Daily time: **11:00 UTC** (about 6-7am New York).
3. Interns and all seniorities included: **default yes**.
4. "Refresh now" button: **v2**.
