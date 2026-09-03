import type { Config, Context } from "@netlify/functions";
import { corsPreflight, json, requireAdmin } from "./_lib";

type Workflow = {
  id: number;
  name: string;
  path: string;
  state: string;
};

async function resolveWorkflowId(
  owner: string,
  repo: string,
  pat: string,
  preferred: string,
): Promise<{ id: number; path: string } | { error: string }> {
  const headers = {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${pat}`,
    "X-GitHub-Api-Version": "2022-11-28",
  };

  // Prefer an exact file name / path (e.g. daily.yml), never a display name.
  const want = preferred.replace(/^\.github\/workflows\//, "").toLowerCase();

  const listResp = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows?per_page=100`,
    { headers },
  );
  if (!listResp.ok) {
    const text = await listResp.text();
    return { error: `GitHub ${listResp.status} listing workflows: ${text.slice(0, 160)}` };
  }

  const data = (await listResp.json()) as { workflows?: Workflow[] };
  const workflows = data.workflows || [];

  const match =
    workflows.find((w) => w.path.toLowerCase().endsWith(`/${want}`)) ||
    workflows.find((w) => w.path.toLowerCase().endsWith(want)) ||
    workflows.find((w) => w.name.toLowerCase() === preferred.toLowerCase());

  if (!match) {
    const paths = workflows.map((w) => w.path).join(", ") || "(none)";
    return {
      error: `Workflow "${preferred}" not found. Available: ${paths}`,
    };
  }

  // Confirm this workflow accepts workflow_dispatch by fetching its file on main
  // is not exposed via list API; attempt dispatch and rely on caller. Prefer
  // repo workflows under .github/workflows/ over GitHub-managed ones (pages, etc).
  if (!match.path.startsWith(".github/workflows/")) {
    return {
      error: `Refusing to dispatch "${match.path}" (not a repo workflow file). Set GITHUB_WORKFLOW=daily.yml`,
    };
  }

  return { id: match.id, path: match.path };
}

export default async (req: Request, _context: Context) => {
  if (req.method === "OPTIONS") return corsPreflight();
  if (req.method !== "POST") return json({ error: "Method not allowed" }, 405);
  if (!requireAdmin(req)) return json({ error: "Unauthorized" }, 401);

  const pat = process.env.GITHUB_PAT;
  const owner = process.env.GITHUB_OWNER || "Garry-j-code";
  const repo = process.env.GITHUB_REPO || "funding-blotter";
  // File name only — not the display name "daily blotter", not pages-build-deployment
  const workflowFile = process.env.GITHUB_WORKFLOW || "daily.yml";
  if (!pat) return json({ error: "GITHUB_PAT not configured" }, 500);

  const body = await req.json().catch(() => ({}));
  const runner = (body as { runner?: string }).runner || "any";

  const resolved = await resolveWorkflowId(owner, repo, pat, workflowFile);
  if ("error" in resolved) return json({ error: resolved.error }, 500);

  const resp = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${resolved.id}/dispatches`,
    {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${pat}`,
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({
        ref: "main",
        inputs: { reason: "blotter-netlify", runner },
      }),
    },
  );

  if (resp.status === 204) {
    return json({
      ok: true,
      message: "Fetch workflow started",
      workflow: resolved.path,
      actions_url: `https://github.com/${owner}/${repo}/actions/workflows/daily.yml`,
    });
  }

  const text = await resp.text();
  let hint = "";
  if (resp.status === 422 && text.includes("workflow_dispatch")) {
    hint =
      " — Netlify may be targeting the wrong workflow. In Netlify env, set GITHUB_WORKFLOW=daily.yml (or delete that var) and redeploy.";
  }
  return json(
    { error: `GitHub ${resp.status}: ${text.slice(0, 200)}${hint}` },
    resp.status,
  );
};

export const config: Config = {
  path: "/api/fetch",
};
