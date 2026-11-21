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
      const parsedWindow = parseInt(url.searchParams.get("window_days") || "30", 10);
      // window_days=0 (or invalid) means "all time": no date cutoff.
      const windowDays = Number.isFinite(parsedWindow) && parsedWindow > 0 ? parsedWindow : 0;
      const publishedAt = url.searchParams.get("published_at") || "";
      const q = (url.searchParams.get("q") || "").toLowerCase();

      // Explicit inclusive date range (YYYY-MM-DD). Takes precedence over window_days.
      const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
      const fromParam = url.searchParams.get("from") || "";
      const toParam = url.searchParams.get("to") || "";
      const fromIso = ISO_DATE.test(fromParam) ? fromParam : "";
      const toIso = ISO_DATE.test(toParam) ? toParam : "";
      const hasRange = Boolean(fromIso || toIso);

      let cutoffIso = "";
      if (!hasRange && windowDays > 0) {
        const cutoff = new Date();
        cutoff.setDate(cutoff.getDate() - windowDays);
        cutoffIso = cutoff.toISOString().slice(0, 10);
      }

      const sb = supabaseAdmin();
      const { data: blocked, error: blockedErr } = await sb
        .from("blocked_companies")
        .select("company_key");
      if (blockedErr) return json({ error: blockedErr.message }, 500);

      const blockedSet = new Set((blocked || []).map((r) => r.company_key));

      // ?months=1 -> months that have (non-blocked) deals, newest first.
      if (url.searchParams.get("months")) {
        const counts = new Map<string, number>();
        for (let from = 0; ; from += 1000) {
          const { data: page, error } = await sb
            .from("deals")
            .select("published_at, company_key")
            .order("id", { ascending: true })
            .range(from, from + 999);
          if (error) return json({ error: error.message }, 500);
          for (const r of page || []) {
            if (!r.published_at || blockedSet.has(r.company_key)) continue;
            const m = String(r.published_at).slice(0, 7);
            counts.set(m, (counts.get(m) || 0) + 1);
          }
          if (!page || page.length < 1000) break;
        }
        const months = [...counts.entries()]
          .sort((x, y) => y[0].localeCompare(x[0]))
          .map(([month, count]) => ({ month, count }));
        return json(months);
      }

      // Supabase caps each response at ~1000 rows, so page through the results.
      // `id` is a tiebreaker so pages are stable.
      const PAGE = 1000;
      const data: Record<string, any>[] = [];
      for (let from = 0; ; from += PAGE) {
        let query = sb
          .from("deals")
          .select("*")
          .order("published_at", { ascending: false })
          .order("id", { ascending: false })
          .range(from, from + PAGE - 1);
        if (hasRange) {
          if (fromIso) query = query.gte("published_at", fromIso);
          if (toIso) query = query.lte("published_at", toIso);
        } else if (cutoffIso) {
          query = query.gte("published_at", cutoffIso);
        }
        if (publishedAt) query = query.eq("published_at", publishedAt);

        const { data: page, error } = await query;
        if (error) return json({ error: error.message }, 500);
        data.push(...(page || []));
        if (!page || page.length < PAGE) break;
      }

      let rows = data.filter((d) => !blockedSet.has(d.company_key));
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
