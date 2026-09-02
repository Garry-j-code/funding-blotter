import type { Config, Context } from "@netlify/functions";
import { corsPreflight, json, requireAdmin } from "./_lib";

export default async (req: Request, _context: Context) => {
  if (req.method === "OPTIONS") return corsPreflight();
  if (req.method !== "POST") return json({ error: "Method not allowed" }, 405);
  if (!requireAdmin(req)) return json({ error: "Unauthorized" }, 401);

  const pat = process.env.GITHUB_PAT;
  const owner = process.env.GITHUB_OWNER || "Garry-j-code";
  const repo = process.env.GITHUB_REPO || "funding-blotter";
  const workflow = process.env.GITHUB_WORKFLOW || "daily.yml";
  if (!pat) return json({ error: "GITHUB_PAT not configured" }, 500);

  const body = await req.json().catch(() => ({}));
  const runner = (body as { runner?: string }).runner || "any";

  const resp = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`,
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
      actions_url: `https://github.com/${owner}/${repo}/actions/workflows/${workflow}`,
    });
  }

  const text = await resp.text();
  return json({ error: `GitHub ${resp.status}: ${text.slice(0, 200)}` }, resp.status);
};

export const config: Config = {
  path: "/api/fetch",
};
