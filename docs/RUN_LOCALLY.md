# Run the app locally

How to run the Funding Blotter web app (React UI + Netlify Functions) on your
machine. For production deployment see [DEPLOY_NETLIFY.md](DEPLOY_NETLIFY.md).

> **Heads up:** the local app reads and writes the **same Supabase database as
> production**. Using **Remove** locally deletes real rows.

## Prerequisites

- Node.js 22 (matches `netlify.toml`) and npm
- A `.env` file in the repo root (copy from `.env.example`) with at least:

  | Variable | Needed for |
  |---|---|
  | `SUPABASE_URL` | API functions |
  | `SUPABASE_SERVICE_ROLE_KEY` | API functions (server-side only) |
  | `ADMIN_SECRET` | Remove / Fetch buttons (any long random string) |

  `GROQ_API_KEY` and `TAVILY_API_KEY` are only needed for the Python pipeline.

## 1. Install dependencies (once)

From the repo root:

```bash
npm install                  # Netlify Functions dependencies
npm --prefix web install     # React app dependencies
```

## 2. Start the app

From the repo root, run one command:

```bash
npx -y netlify-cli dev --port 8888 --framework "#custom" \
  --command "npm --prefix web run dev -- --port 5174 --strictPort" \
  --target-port 5174
```

This starts the API functions and the Vite dev server together and loads `.env`
automatically. Wait for the "Server now ready" line, then open:

**http://localhost:8888**

Stop everything with `Ctrl+C`.

### Why not plain `npx netlify dev`?

- The npm package is named `netlify-cli`, so `npx netlify dev` fails with
  "could not determine executable to run".
- Netlify's framework auto-detection mistakes the `docs/` folder for a Hugo site and
  fails with `Failed running command: hugo server -w`. The `--framework "#custom"`
  flags above skip that detection.

## 3. Check it works

```bash
curl localhost:8888/api/health
# {"ok":true,"deals":186}   (count will differ)

curl "localhost:8888/api/deals?window_days=30"   # JSON array of deals
```

In the UI, use the **Range** dropdown (Last 30 days / 90 days / Last year / All
time) to load older deals. `window_days=0` on the API means "no date cutoff".

## Admin features (Fetch / Remove)

Click **Unlock admin** in the UI and paste the `ADMIN_SECRET` value from your
`.env`. It is stored in that browser only (localStorage). Without it the app is
read-only.

**Fetch today's deals** triggers the GitHub Actions workflow and needs
`GITHUB_PAT` set in `.env`. `GITHUB_OWNER`, `GITHUB_REPO` and `GITHUB_WORKFLOW`
are optional and default to this project's values.

## Running the Python pipeline locally (optional)

The pipeline is separate from the web app. It scrapes FinSMEs and writes deals.

```bash
uv sync

uv run python -m src.main --probe finsmes                  # check the scraper
uv run python -m src.main --dry-run --backend sqlite       # prints deals, writes nothing
uv run python -m src.main --backend sqlite                 # writes to local data/deals.db
uv run python -m src.main --backend supabase               # writes to PRODUCTION Supabase
```

Prefer `--backend sqlite` for experiments. A local SQLite run does **not** change
what the web app shows, because the web app reads Supabase. FinSMEs blocks some
networks, such as GitHub's cloud IPs; run it from your own machine.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Missing Supabase env vars` / API returns 500 | Check `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in the root `.env`, then restart. |
| Remove / Fetch return `401 Unauthorized` | `.env` must use `ADMIN_SECRET` (not `ADMIN_SECRET_KEY`), and the value you pasted into **Unlock admin** must match. Restart after editing `.env`. |
| `Port 8888 / 5174 already in use` | Another instance is still running. Stop it with `Ctrl+C`, or find it with `lsof -i :8888`. |
| `hugo server -w` error | You ran `netlify dev` without `--framework "#custom"`. Use the command in step 2. |
| UI shows "Failed to load deals" | Open the browser Network tab for the `/api/deals` response, or check the terminal running `netlify-cli`. |
| UI is empty or old deals are missing | Pick a longer **Range**. The default is 30 days. |
