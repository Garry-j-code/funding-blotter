# Plan: open roles on every company card

Status: **plan only, nothing built yet.** Written 2026-11-21.

## 1. Goal

For every company already in the blotter, find its job board, pull the open roles
that match **Forward Deployed Engineer, AI Engineer, Product Engineer, AI Product
Engineer**, and show them on the company's card. The careers URL is visible to
everyone and editable by the admin. A daily automation keeps it fresh.

### Decisions already made (from our conversation)
- Match the four titles **plus close variants** (see section 5.3). After the first dry run this was made strict: ML Engineer, AI Security/Operations Engineer, generic "Software Engineer, AI" titles and "Forward Deployed Banker/Auditor" are no longer matched.
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

## 3. Data model (already applied in Supabase)

The SQL below was run once in the Supabase SQL editor. The migration files were then removed
from the repo to keep it clean, so this is the record of the careers schema.

```sql
-- Careers: each company's public job board and the matching open roles.
-- Run once in the Supabase SQL editor. Only adds new tables; nothing existing changes.

CREATE TABLE IF NOT EXISTS company_careers (
    company_key         TEXT PRIMARY KEY,                  -- same key as deals.company_key
    company             TEXT,                              -- display name at discovery time
    careers_url         TEXT,                              -- what users see; admin editable
    ats                 TEXT,                              -- ashby | greenhouse | lever | workable | custom | none
    ats_slug            TEXT,                              -- board token for the job system's public API
    source              TEXT NOT NULL DEFAULT 'auto',      -- auto | manual (manual is never overwritten)
    verified            BOOLEAN NOT NULL DEFAULT FALSE,    -- false = a guess; only the admin sees it
    status              TEXT NOT NULL DEFAULT 'pending',   -- pending | ok | no_board | unverified | error | unsupported
    needs_refresh       BOOLEAN NOT NULL DEFAULT FALSE,    -- set when the admin edits the URL
    empty_streak        INT NOT NULL DEFAULT 0,            -- consecutive runs where a board with open roles came back empty
    consecutive_errors  INT NOT NULL DEFAULT 0,
    last_checked        TIMESTAMPTZ,
    last_ok             TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS jobs (
    id               BIGSERIAL PRIMARY KEY,
    company_key      TEXT NOT NULL REFERENCES company_careers (company_key) ON DELETE CASCADE,
    external_id      TEXT NOT NULL,                        -- the job system's own posting id
    title            TEXT NOT NULL,
    role_type        TEXT NOT NULL,                        -- primary match: forward_deployed_engineer | ai_product_engineer | product_engineer | ai_engineer
    tags             TEXT[] NOT NULL DEFAULT '{}',         -- every role type the title matched
    department       TEXT,
    location         TEXT,
    employment_type  TEXT,
    url              TEXT NOT NULL,
    posted_at        TIMESTAMPTZ,
    first_seen       DATE NOT NULL,
    last_seen        DATE NOT NULL,
    closed_at        DATE,
    is_open          BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (company_key, external_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_company_open ON jobs (company_key) WHERE is_open;

-- RLS: deny public access; the service-role key (pipeline + Netlify Functions) bypasses RLS.
ALTER TABLE company_careers ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
```

## 4. Where the work lives

| Piece | Files |
|---|---|
| Migration | applied by hand in Supabase (SQL in section 3) |
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
| `ai_engineer` | "AI Engineer", "Applied AI Engineer", "AI/ML Engineer", "AI Software Engineer", "LLM Engineer", "GenAI Engineer" (not ML Engineer, not "Software Engineer, AI") |

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
| 1 | Schema | SQL in section 3 | done: you ran it in the Supabase SQL editor |
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

## 10. Addendum: custom careers pages (added after the first real run)

A URL the admin saves that is not an Ashby/Greenhouse/Lever/Workable board (`ats = custom`) is no longer
just displayed. Each run (daily while it yields roles or errors, otherwise every `retry_no_board_days`):

1. The page is fetched once (public host only, robots.txt respected, redirects re-checked).
2. If it links or embeds a supported job board, that board is used through its API like any other board;
   the admin's URL stays as the card's link (`ats`/`ats_slug` are filled in, `source` stays `manual`).
3. Otherwise any schema.org `JobPosting` JSON-LD on the page is used (`ats = custom`, `status = ok`,
   job ids prefixed `ld:`).
4. Otherwise `status = unsupported`: the link is shown, no roles. Roles from an earlier source are retired
   through the normal "hold one run, then close" rule. A page that cannot be fetched never closes roles.

Pages that render their jobs with JavaScript or list them as plain text stay `unsupported`; the admin can still
point the URL at the company's real job board when they find one. The API sends `roles_tracked` so the panel
can say whether roles are being fetched for the company.

## 11. Addendum: the Jobs page

`#/jobs` (header: Blotter | Jobs) lists every open role that has been fetched, newest first, with role and
company filters, a search box and a NEW filter. Everyone can see it; visitors only get roles from verified boards.

Admins also get an "Add a careers URL" form: company name + URL.
- A name whose `company_key` already exists in the blotter is rejected with "open its card and add the URL in its
  Careers panel" (HTTP 409). A name already added here is also rejected (edit its URL in the list instead).
- Otherwise a **standalone** company is saved: a `company_careers` row with `source = manual` and no deal. The daily run
  processes these like blotter companies (`standalone_companies()` in `src/jobs.py`), so the URL is read on the next run.
- The admin list under the form shows each standalone company's status, lets the admin edit its URL, and remove it
  (its roles are deleted with it). Blotter companies cannot be removed this way.
- API: `POST /api/careers`, `PUT` and `DELETE /api/careers/:company_key` (all admin only). `GET` now includes `company`
  and, for the admin, `standalone` and `needs_refresh`.

Ordering: roles are grouped by company, and companies are ordered by the date they were added, newest first. For a
blotter company that is the newest `published_at` among its deals; for a company added on the Jobs page it is the day
it was added (`company_careers.created_at`). The API sends it as `added_at`. `created_at` needs this one-time SQL
(run in the Supabase SQL editor); without it, companies added on the Jobs page have no date and sort last:

```sql
ALTER TABLE company_careers ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
```

## 12. Addendum: Run now

Admins get a **Run now** button on the Jobs page. It calls `POST /api/careers-run`, which asks GitHub to start
`jobs.yml` on `main` (workflow_dispatch) using the same `GITHUB_PAT` as the Fetch button. It is refused while a
run is already queued or in progress, and the workflow file name is fixed in code. The page then polls
`GET /api/careers-run` (latest run, status, link) every 8 seconds and reloads the roles when the run finishes.
It only works once `jobs.yml` is on `main`. Without `GITHUB_PAT` on the server the button is hidden and the page says so.

