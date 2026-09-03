# Self-hosted runner setup (Mac + Windows)

FinSMEs blocks GitHub cloud runners. The **daily blotter** workflow scrapes on
your Mac or Windows PC (home IP) and writes to **Supabase**.

Runs are **manual only** — from the Netlify blotter **Fetch today's deals**
button (via `/api/fetch`) or **Actions → Run workflow**.

---

## 1. Register runners

https://github.com/Garry-j-code/funding-blotter/settings/actions/runners/new

| Machine | Labels |
|---------|--------|
| Mac | `self-hosted`, `mac` |
| Windows | `self-hosted`, `windows` |

See [DEPLOY_NETLIFY.md](DEPLOY_NETLIFY.md) for install commands.

**Windows:** install [Git for Windows](https://git-scm.com/download/win) and [uv](https://docs.astral.sh/uv/).

### Windows: `bash: command not found`

The workflow uses `bash`. Install Git for Windows, then verify in **PowerShell**:

```powershell
& "C:\Program Files\Git\bin\bash.exe" --version
```

If that works but Actions still fails:

1. Add `C:\Program Files\Git\bin` to the **system** PATH (not only user PATH):
   Settings → System → About → Advanced system settings → Environment Variables → System variables → Path → New
2. Restart the runner:
   ```powershell
   cd C:\actions-runner
   .\svc.cmd stop
   .\svc.cmd start
   ```
   Or if you use `.\run.cmd`, close that window and start it again.

---

## 2. GitHub Actions secrets

| Secret | Purpose |
|--------|---------|
| `GROQ_API_KEY` | Extraction + enrichment |
| `TAVILY_API_KEY` | Web search |
| `SUPABASE_URL` | Write deals to cloud DB |
| `SUPABASE_SERVICE_ROLE_KEY` | Write deals to cloud DB |

---

## 3. Run a fetch

1. **Actions** → **daily blotter** → **Run workflow**
2. Pick runner: `any`, `mac`, or `windows`
3. Refresh the Netlify blotter site when the job finishes green

Or use **Fetch today's deals** on the Netlify site (requires `ADMIN_SECRET` +
`GITHUB_PAT` in Netlify env — see [DEPLOY_NETLIFY.md](DEPLOY_NETLIFY.md)).

---

## 4. Mac vs Windows

- Same home network → same public IP; either works for FinSMEs.
- `any` → whichever runner is online.
- Only one workflow at a time (`concurrency: blotter`).
