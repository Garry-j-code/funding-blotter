// Start and watch the daily careers workflow (.github/workflows/jobs.yml) through the GitHub API.
// Pure functions with an injectable fetch, so they can be tested without a network.

export const CAREERS_WORKFLOW = "jobs.yml";      // fixed: never taken from a request

export interface GhConfig {
  token: string;
  owner: string;
  repo: string;
  ref: string;
  apiBase: string;
}

export interface RunInfo {
  id: number;
  status: string;                 // queued | in_progress | completed ...
  conclusion: string | null;      // success | failure | cancelled ... (null while running)
  created_at: string;
  html_url: string;
  event: string;
  active: boolean;
}

export class GhError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** Same env vars as POST /api/fetch (GITHUB_PAT, GITHUB_OWNER, GITHUB_REPO). Null when no token is set. */
export function ghConfig(env: Record<string, string | undefined>): GhConfig | null {
  const token = (env.GITHUB_PAT || "").trim();
  if (!token) return null;
  return {
    token,
    owner: (env.GITHUB_OWNER || "Garry-j-code").trim(),
    repo: (env.GITHUB_REPO || "funding-blotter").trim(),
    ref: (env.GITHUB_REF_BRANCH || "main").trim(),
    apiBase: (env.GITHUB_API_URL || "https://api.github.com").replace(/\/$/, ""),
  };
}

type FetchFn = typeof fetch;

function headers(cfg: GhConfig) {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${cfg.token}`,
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "funding-blotter",
  };
}

function explain(status: number, body: string): GhError {
  if (status === 401 || status === 403) {
    return new GhError(502, "GitHub refused the token. It needs Actions read and write access on this repository.");
  }
  if (status === 404) {
    return new GhError(502, `GitHub cannot find ${CAREERS_WORKFLOW} on the main branch (it must be merged into main first), or the token cannot see the repository.`);
  }
  return new GhError(502, `GitHub ${status}: ${body.slice(0, 160)}`);
}

export function toRun(r: any): RunInfo {
  const status = String(r.status || "");
  return {
    id: r.id,
    status,
    conclusion: r.conclusion ?? null,
    created_at: r.created_at,
    html_url: r.html_url,
    event: r.event,
    active: status !== "completed",
  };
}

/** The newest run of the careers workflow, or null if it has never run. */
export async function latestRun(cfg: GhConfig, fetchFn: FetchFn = fetch): Promise<RunInfo | null> {
  const url = `${cfg.apiBase}/repos/${cfg.owner}/${cfg.repo}/actions/workflows/${CAREERS_WORKFLOW}/runs?per_page=1`;
  const resp = await fetchFn(url, { headers: headers(cfg) });
  if (!resp.ok) throw explain(resp.status, await resp.text());
  const data = (await resp.json()) as { workflow_runs?: any[] };
  const run = data.workflow_runs?.[0];
  return run ? toRun(run) : null;
}

/** Ask GitHub to start the workflow on the configured branch. Resolves when GitHub accepted it. */
export async function dispatch(cfg: GhConfig, fetchFn: FetchFn = fetch): Promise<void> {
  const url = `${cfg.apiBase}/repos/${cfg.owner}/${cfg.repo}/actions/workflows/${CAREERS_WORKFLOW}/dispatches`;
  const resp = await fetchFn(url, {
    method: "POST",
    headers: { ...headers(cfg), "Content-Type": "application/json" },
    body: JSON.stringify({ ref: cfg.ref, inputs: { company_key: "", discover_only: false } }),
  });
  if (resp.status !== 204) throw explain(resp.status, await resp.text());
}
