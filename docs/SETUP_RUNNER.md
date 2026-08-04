# Self-hosted Mac runner setup

FinSMEs’ Cloudflare blocks GitHub’s cloud runners. The daily workflow must run
on **your Mac** (home IP), via a self-hosted Actions runner.

## 1. Add a runner on GitHub

1. Open: https://github.com/Garry-j-code/funding-blotter/settings/actions/runners/new
2. Choose **macOS**
3. Follow the commands GitHub shows (they include a one-time token)

Roughly:

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
# download + extract the runner package from the page above, then:
./config.sh --url https://github.com/Garry-j-code/funding-blotter --token <TOKEN_FROM_PAGE>
```

When asked for labels / name, defaults are fine. `runs-on: self-hosted` matches
any self-hosted runner.

## 2. Install as a service (so it survives reboot)

```bash
cd ~/actions-runner
./svc.sh install
./svc.sh start
./svc.sh status
```

Keep the Mac **awake** (or allow wake for network) around **11:30 PM NY** for
the scheduled run. For on-demand fetches, the Mac just needs to be on when you
hit **Fetch today's deals**.

## 3. Prerequisites on the Mac

- `git` (Xcode CLT or Homebrew)
- Network access to finsmes.com, api.groq.com, api.tavily.com, github.com
- The workflow installs `uv` via `astral-sh/setup-uv` on each run

Repo secrets (already set): `GROQ_API_KEY`, `TAVILY_API_KEY`.

## 4. Verify

1. Confirm the runner shows **Idle** (green) under  
   Settings → Actions → Runners
2. On the blotter page: **Set token** (once) → **Fetch today's deals**  
   Or: Actions → daily blotter → **Run workflow**
3. Watch the run; when it finishes green, refresh the Pages site

## 5. PAT for the blotter button (optional)

The page button calls GitHub’s API from your browser. Create a token:

- Fine-grained: resource = this repo, permission **Actions: Read and write**
- Or classic: `workflow` scope

Paste it via **Set token** on the blotter. It is stored in `localStorage` only
in that browser — never committed.

Without a token, the button opens the Actions page so you can click **Run workflow** manually.
