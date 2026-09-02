import type { Config, Context } from "@netlify/functions";
import {
  corsPreflight,
  json,
  requireAdmin,
  slimDeal,
  supabaseAdmin,
} from "./_lib";

export default async (req: Request, _context: Context) => {
  if (req.method === "OPTIONS") return corsPreflight();

  const url = new URL(req.url);
  const pathMatch = url.pathname.match(/\/deals\/([^/]+)\/?$/);
  const companyKey =
    (pathMatch ? decodeURIComponent(pathMatch[1]) : null) ||
    url.searchParams.get("company_key");

  if (req.method === "GET" && !companyKey) {
    const windowDays = parseInt(url.searchParams.get("window_days") || "30", 10);
    const publishedAt = url.searchParams.get("published_at") || "";
    const q = (url.searchParams.get("q") || "").toLowerCase();

    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - windowDays);
    const cutoffIso = cutoff.toISOString().slice(0, 10);

    const sb = supabaseAdmin();
    const { data: blocked } = await sb.from("blocked_companies").select("company_key");
    const blockedSet = new Set((blocked || []).map((r) => r.company_key));

    let query = sb
      .from("deals")
      .select("*")
      .gte("published_at", cutoffIso)
      .order("published_at", { ascending: false })
      .order("priority", { ascending: false })
      .order("score", { ascending: false });

    if (publishedAt) {
      query = query.eq("published_at", publishedAt);
    }

    const { data, error } = await query;
    if (error) return json({ error: error.message }, 500);

    let rows = (data || []).filter((d) => !blockedSet.has(d.company_key));
    if (q) {
      rows = rows.filter((d) =>
        [d.company, d.description, d.investors, d.location, d.stage]
          .join(" ")
          .toLowerCase()
          .includes(q),
      );
    }

    return json(rows.map(slimDeal));
  }

  if (req.method === "DELETE" && companyKey) {
    if (!requireAdmin(req)) {
      return json({ error: "Unauthorized" }, 401);
    }
    const body = await req.json().catch(() => ({}));
    const company = (body as { company?: string }).company || companyKey;
    const today = new Date().toISOString().slice(0, 10);
    const sb = supabaseAdmin();

    await sb.from("blocked_companies").upsert(
      {
        company_key: companyKey,
        company,
        blocked_at: today,
        reason: "manual-remove",
      },
      { onConflict: "company_key" },
    );

    const { error: delErr } = await sb.from("deals").delete().eq("company_key", companyKey);
    if (delErr) return json({ error: delErr.message }, 500);

    await sb.from("company_sector").delete().eq("company_key", companyKey);

    return json({ company_key: companyKey, company, ok: true });
  }

  return json({ error: "Not found" }, 404);
};

export const config: Config = {
  path: ["/api/deals", "/api/deals/*"],
};
