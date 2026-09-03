import type { Config, Context } from "@netlify/functions";
import {
  corsPreflight,
  json,
  requireAdmin,
  slimDeal,
  supabaseAdmin,
} from "./_lib";

export default async (req: Request, _context: Context) => {
  try {
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
      const { data: blocked, error: blockedErr } = await sb
        .from("blocked_companies")
        .select("company_key");
      if (blockedErr) return json({ error: blockedErr.message }, 500);

      const blockedSet = new Set((blocked || []).map((r) => r.company_key));

      let query = sb
        .from("deals")
        .select("*")
        .gte("published_at", cutoffIso)
        .order("published_at", { ascending: false });

      if (publishedAt) {
        query = query.eq("published_at", publishedAt);
      }

      const { data, error } = await query;
      if (error) return json({ error: error.message }, 500);

      let rows = (data || []).filter((d) => !blockedSet.has(d.company_key));
      rows.sort((a, b) => {
        const pub = (b.published_at || "").localeCompare(a.published_at || "");
        if (pub !== 0) return pub;
        const pri = (b.priority || 0) - (a.priority || 0);
        if (pri !== 0) return pri;
        return (b.score || 0) - (a.score || 0);
      });

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

      const { error: blockErr } = await sb.from("blocked_companies").upsert(
        {
          company_key: companyKey,
          company,
          blocked_at: today,
          reason: "manual-remove",
        },
        { onConflict: "company_key" },
      );
      if (blockErr) return json({ error: blockErr.message }, 500);

      const { error: delErr } = await sb.from("deals").delete().eq("company_key", companyKey);
      if (delErr) return json({ error: delErr.message }, 500);

      await sb.from("company_sector").delete().eq("company_key", companyKey);

      return json({ company_key: companyKey, company, ok: true });
    }

    return json({ error: "Not found" }, 404);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error("deals function error:", message);
    return json({ error: message }, 500);
  }
};

export const config: Config = {
  path: ["/api/deals", "/api/deals/*"],
};
