import type { Config, Context } from "@netlify/functions";
import { corsPreflight, json, supabaseAdmin } from "./_lib";

export default async (req: Request, _context: Context) => {
  if (req.method === "OPTIONS") return corsPreflight();
  try {
    const sb = supabaseAdmin();
    const { count, error } = await sb.from("deals").select("*", { count: "exact", head: true });
    if (error) return json({ ok: false, error: error.message }, 500);
    return json({ ok: true, deals: count ?? 0 });
  } catch (e) {
    return json({ ok: false, error: String(e) }, 500);
  }
};

export const config: Config = {
  path: "/api/health",
};
