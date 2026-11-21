import type { Config, Context } from "@netlify/functions";
import { corsPreflight, json, requireAdmin } from "./_lib";
import { dispatch, ghConfig, GhError, latestRun } from "./_github";

/**
 * Admin only.
 *   GET  /api/careers-run  -> { configured, run }   latest careers workflow run (status, link)
 *   POST /api/careers-run  -> starts the careers workflow now (refused while one is already running)
 */
export default async (req: Request, _context: Context) => {
  if (req.method === "OPTIONS") return corsPreflight();
  if (req.method !== "GET" && req.method !== "POST") return json({ error: "Method not allowed" }, 405);
  if (!requireAdmin(req)) return json({ error: "Unauthorized" }, 401);

  const cfg = ghConfig(process.env);
  if (!cfg) {
    if (req.method === "GET") return json({ configured: false, run: null });
    return json({ error: "GITHUB_PAT is not set on the server, so the workflow cannot be started from here." }, 501);
  }

  try {
    const run = await latestRun(cfg);
    if (req.method === "GET") return json({ configured: true, run });

    if (run?.active) return json({ error: "A careers run is already in progress.", run }, 409);
    await dispatch(cfg);
    return json({ ok: true, message: "Careers run started. New roles appear when it finishes (a few minutes)." }, 202);
  } catch (e) {
    if (e instanceof GhError) return json({ error: e.message }, e.status);
    console.error("careers-run error:", e instanceof Error ? e.message : String(e));
    return json({ error: "Could not reach GitHub." }, 502);
  }
};

export const config: Config = {
  path: "/api/careers-run",
};
