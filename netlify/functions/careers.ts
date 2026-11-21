import type { Config, Context } from "@netlify/functions";
import { corsPreflight, json, requireAdmin, supabaseAdmin } from "./_lib";
import { companyKey as makeCompanyKey, isNew, parseBoardUrl, validateCareersUrl } from "./_careers";

type Row = Record<string, any>;

/** Every row of a table (Supabase returns at most 1000 rows per request). */
async function fetchAll(
  sb: ReturnType<typeof supabaseAdmin>,
  table: string,
  order: string,
  filter?: (q: any) => any,
  columns = "*",
) {
  const rows: Row[] = [];
  for (let from = 0; ; from += 1000) {
    let q: any = sb.from(table).select(columns).order(order).range(from, from + 999);
    if (filter) q = filter(q);
    const { data, error } = await q;
    if (error) throw new Error(error.message);
    rows.push(...(data || []));
    if (!data || data.length < 1000) return rows;
  }
}

function publicJob(j: Row) {
  return {
    id: j.id,
    title: j.title,
    location: j.location,
    url: j.url,
    role_type: j.role_type,
    posted_at: j.posted_at,
    first_seen: j.first_seen,
    is_new: isNew(j.first_seen),
  };
}

function entry(care: Row, jobs: Row[], admin: boolean, standalone?: boolean, addedAt?: string | null) {
  const out: Row = {
    company: care.company,
    added_at: addedAt ?? null,   // newest blotter date for the company, or the day it was added on the Jobs page
    careers_url: care.careers_url,
    ats: care.ats,
    last_checked: care.last_checked,
    // False for a custom page we cannot read roles from (status other than ok); its stale roles stay hidden.
    roles_tracked: !(care.ats === "custom" && care.status !== "ok"),
    jobs: care.ats === "custom" && care.status !== "ok" ? [] : jobs.map(publicJob),
  };
  if (admin) {
    out.verified = care.verified;
    out.status = care.status;
    out.source = care.source;
    out.consecutive_errors = care.consecutive_errors;
    out.needs_refresh = Boolean(care.needs_refresh);
    out.standalone = Boolean(standalone);   // added on the Jobs page; the company is not in the blotter
  }
  return out;
}

/** The newest blotter row for this company_key, or null. */
async function dealCompany(sb: ReturnType<typeof supabaseAdmin>, key: string): Promise<{ company: string } | null> {
  const { data, error } = await sb
    .from("deals").select("company").eq("company_key", key).order("id", { ascending: false }).limit(1);
  if (error) throw new Error(error.message);
  return data && data.length ? { company: data[0].company as string } : null;
}

async function careersRow(sb: ReturnType<typeof supabaseAdmin>, key: string): Promise<Row | undefined> {
  const { data, error } = await sb.from("company_careers").select("*").eq("company_key", key).limit(1);
  if (error) throw new Error(error.message);
  return data?.[0];
}

export default async (req: Request, _context: Context) => {
  try {
    if (req.method === "OPTIONS") return corsPreflight();

    const url = new URL(req.url);
    const m = url.pathname.match(/\/careers\/([^/]+)\/?$/);
    const companyKey = m ? decodeURIComponent(m[1]) : null;
    const sb = supabaseAdmin();

    // ---- GET /api/careers : everything the cards need, in one request ----
    if (req.method === "GET" && !companyKey) {
      const admin = requireAdmin(req);
      const [careers, openJobs, dealRows] = await Promise.all([
        fetchAll(sb, "company_careers", "company_key"),
        fetchAll(sb, "jobs", "id", (q) => q.eq("is_open", true)),
        fetchAll(sb, "deals", "id", undefined, "id,company_key,published_at"),
      ]);
      const dealKeys = new Set<string>();
      const latestDeal: Record<string, string> = {};     // company_key -> newest published_at in the blotter
      for (const d of dealRows) {
        dealKeys.add(d.company_key);
        const day = String(d.published_at || "").slice(0, 10);
        if (day && day > (latestDeal[d.company_key] || "")) latestDeal[d.company_key] = day;
      }
      const jobsByCompany: Record<string, Row[]> = {};
      for (const j of openJobs) (jobsByCompany[j.company_key] ||= []).push(j);

      const result: Record<string, Row> = {};
      for (const c of careers) {
        // Visitors never see unverified guesses or companies with nothing to show.
        if (!admin && !(c.verified && c.careers_url)) continue;
        if (admin && !c.careers_url && !c.status) continue;
        const jobs = (jobsByCompany[c.company_key] || []).sort(
          (a, b) => (b.first_seen || "").localeCompare(a.first_seen || ""),
        );
        const standalone = c.source === "manual" && !dealKeys.has(c.company_key);
        const addedAt = standalone ? String(c.created_at || "").slice(0, 10) || null : latestDeal[c.company_key] || null;
        result[c.company_key] = entry(c, jobs, admin, standalone, addedAt);
      }
      return json(result);
    }

    // ---- POST /api/careers : admin adds a company that is not in the blotter ----
    if (req.method === "POST" && !companyKey) {
      if (!requireAdmin(req)) return json({ error: "Unauthorized" }, 401);
      const body = (await req.json().catch(() => ({}))) as { company?: unknown; careers_url?: unknown };
      const name = typeof body.company === "string" ? body.company.trim().replace(/\s+/g, " ") : "";
      if (!name || name.length > 100 || /[\u0000-\u001f]/.test(name)) {
        return json({ error: "Enter a company name (up to 100 characters)." }, 400);
      }
      const key = makeCompanyKey(name);
      if (!key) return json({ error: "That company name has no letters or digits." }, 400);
      let careersUrl: string;
      try {
        careersUrl = validateCareersUrl(body.careers_url);
      } catch (e) {
        return json({ error: e instanceof Error ? e.message : "Invalid URL" }, 400);
      }

      const inBlotter = await dealCompany(sb, key);
      if (inBlotter) {
        return json({
          error: `${inBlotter.company} is already in the blotter. Open its card and add the URL in its Careers panel.`,
          code: "in_blotter", company_key: key,
        }, 409);
      }
      const onJobsPage = await careersRow(sb, key);
      if (onJobsPage) {
        return json({
          error: `${onJobsPage.company || name} is already on this page. Edit its URL in the list below.`,
          code: "exists", company_key: key,
        }, 409);
      }

      const board = parseBoardUrl(careersUrl);
      const row = {
        company_key: key, company: name, careers_url: careersUrl,
        ats: board ? board.ats : "custom", ats_slug: board ? board.slug : null,
        source: "manual", verified: true, status: board ? "ok" : "unsupported",
        needs_refresh: true, consecutive_errors: 0,
      };
      const { error: insErr } = await sb.from("company_careers").upsert(row, { onConflict: "company_key" });
      if (insErr) return json({ error: insErr.message }, 500);
      return json({ company_key: key, ...entry(row, [], true, true, new Date().toISOString().slice(0, 10)) }, 201);
    }

    // ---- DELETE /api/careers/:company_key : admin removes a company they added here ----
    if (req.method === "DELETE" && companyKey) {
      if (!requireAdmin(req)) return json({ error: "Unauthorized" }, 401);
      if (await dealCompany(sb, companyKey)) {
        return json({ error: "This company is in the blotter. Use Reset on its card instead." }, 400);
      }
      const existing = await careersRow(sb, companyKey);
      if (!existing || existing.source !== "manual") return json({ error: "Unknown company" }, 404);
      const { error: delErr } = await sb.from("company_careers").delete().eq("company_key", companyKey);   // its jobs cascade
      if (delErr) return json({ error: delErr.message }, 500);
      return json({ company_key: companyKey, ok: true });
    }

    // ---- PUT /api/careers/:company_key : admin edits the URL ----
    if (req.method === "PUT" && companyKey) {
      if (!requireAdmin(req)) return json({ error: "Unauthorized" }, 401);
      const body = (await req.json().catch(() => ({}))) as {
        careers_url?: unknown;
        accept?: unknown;
        clear?: unknown;
      };

      // The company is either in the blotter or was added on the Jobs page (a manual careers row).
      const deal = await dealCompany(sb, companyKey);
      const existing = await careersRow(sb, companyKey);
      const standalone = !deal && existing?.source === "manual";
      if (!deal && !standalone) return json({ error: "Unknown company" }, 404);
      const company = (deal?.company ?? existing?.company) as string;
      if (standalone && body.clear === true) {
        return json({ error: "Use Remove to delete a company you added here." }, 400);
      }

      let row: Row;
      if (body.clear === true) {
        // Forget the manual URL; the next run looks for a board again.
        row = {
          company_key: companyKey, company, careers_url: null, ats: null, ats_slug: null,
          source: "auto", verified: false, status: "pending", needs_refresh: true,
        };
      } else if (body.accept === true) {
        if (!existing || !existing.careers_url) return json({ error: "Nothing to accept" }, 400);
        row = { company_key: companyKey, company, source: "manual", verified: true, status: "ok", needs_refresh: true };
      } else {
        let careersUrl: string;
        try {
          careersUrl = validateCareersUrl(body.careers_url);
        } catch (e) {
          return json({ error: e instanceof Error ? e.message : "Invalid URL" }, 400);
        }
        const board = parseBoardUrl(careersUrl);
        row = {
          company_key: companyKey, company, careers_url: careersUrl,
          ats: board ? board.ats : "custom", ats_slug: board ? board.slug : null,
          source: "manual", verified: true, status: board ? "ok" : "unsupported",
          needs_refresh: true, consecutive_errors: 0,
        };
      }

      const { error: upErr } = await sb.from("company_careers").upsert(row, { onConflict: "company_key" });
      if (upErr) return json({ error: upErr.message }, 500);

      const { data: saved } = await sb.from("company_careers").select("*").eq("company_key", companyKey).limit(1);
      const { data: jobs } = await sb.from("jobs").select("*").eq("company_key", companyKey).eq("is_open", true);
      return json({ company_key: companyKey, ...entry(saved?.[0] || row, jobs || [], true, standalone) });
    }

    return json({ error: "Not found" }, 404);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error("careers function error:", message);
    return json({ error: message }, 500);
  }
};

export const config: Config = {
  path: ["/api/careers", "/api/careers/*"],
};
