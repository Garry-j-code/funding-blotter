# Deploy to Netlify + Supabase

## 1. Supabase

1. Create a project at [supabase.com](https://supabase.com).
2. Open **SQL Editor** and run [`supabase/migrations/001_initial.sql`](../supabase/migrations/001_initial.sql).
3. Copy **Project URL** and **service_role** key (Settings → API).
4. Migrate existing SQLite data (optional, one time):

```bash
cp .env.example .env
# fill SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

uv run python scripts/migrate_sqlite_to_supabase.py
uv run python scripts/migrate_sqlite_to_supabase.py --dry-run  # preview counts
```

## 2. GitHub Actions secrets

Repo → Settings → Secrets → Actions:

| Secret | Purpose |
|--------|---------|
| `GROQ_API_KEY` | Extraction + enrichment |
| `TAVILY_API_KEY` | Web search |
| `SUPABASE_URL` | Pipeline writes deals |
| `SUPABASE_SERVICE_ROLE_KEY` | Pipeline writes deals |

Run **Actions → daily blotter → Run workflow** (Mac or Windows runner online).

## 3. Netlify

1. **Add new site** → Import from Git → this repo.
2. Build settings (or use [`netlify.toml`](../netlify.toml)):
   - Build command: `npm --prefix web ci && npm --prefix web run build`
   - Publish directory: `web/dist`
   - Functions directory: `netlify/functions`
3. **Environment variables** (Site settings → Environment variables):

| Variable | Purpose |
|----------|---------|
| `SUPABASE_URL` | API reads/writes |
| `SUPABASE_SERVICE_ROLE_KEY` | API (never expose to browser) |
| `ADMIN_SECRET` | Protects Remove + Fetch API |
| `GITHUB_PAT` | Optional: server-side workflow dispatch |
| `GITHUB_OWNER` | Default `Garry-j-code` |
| `GITHUB_REPO` | Default `funding-blotter` |
| `GITHUB_WORKFLOW` | Default `daily.yml` |

4. Deploy. Open the site → **Set admin secret** (same value as `ADMIN_SECRET`).

## 4. Local dev

```bash
# Terminal 1 — API + functions
npm install
npx netlify dev

# Terminal 2 — Vite (or use netlify dev which proxies)
cd web && npm install && npm run dev
```

Vite proxies `/api` to `localhost:8888` when using `netlify dev`.

## 5. Disable GitHub Pages

Repo → Settings → Pages → set source to **None** (Netlify is the live site).

Redirect old Pages URL to Netlify in Netlify domain settings if needed.
