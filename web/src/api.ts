import type { Careers, CareersMap, Deal } from "./types";

const API = "/api";

function adminHeaders(): HeadersInit {
  const secret = localStorage.getItem("blotter_admin_secret");
  return secret ? { Authorization: `Bearer ${secret}` } : {};
}

export interface DealQuery {
  /** Inclusive YYYY-MM-DD bounds. Either may be empty. Take precedence over windowDays. */
  from?: string;
  to?: string;
  /** 0 = all time. Ignored when from/to are set. */
  windowDays?: number;
}

async function getJson<T>(path: string, what: string): Promise<T> {
  const resp = await fetch(path);
  if (!resp.ok) {
    const body = await resp.text();
    let detail = body;
    try {
      detail = (JSON.parse(body) as { error?: string }).error || body;
    } catch {
      /* use raw body */
    }
    throw new Error(`Failed to load ${what} (${resp.status}): ${detail.slice(0, 200)}`);
  }
  return resp.json();
}

export async function fetchDeals(query: DealQuery = {}): Promise<Deal[]> {
  const params = new URLSearchParams();
  if (query.from) params.set("from", query.from);
  if (query.to) params.set("to", query.to);
  if (!query.from && !query.to) params.set("window_days", String(query.windowDays ?? 30));
  return getJson<Deal[]>(`/api/deals?${params}`, "deals");
}

export interface MonthInfo {
  month: string; // YYYY-MM
  count: number;
}

export function fetchMonths(): Promise<MonthInfo[]> {
  return getJson<MonthInfo[]>("/api/deals?months=1", "months");
}

export async function removeDeal(companyKey: string, company: string): Promise<void> {
  const resp = await fetch(`${API}/deals/${encodeURIComponent(companyKey)}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json", ...adminHeaders() },
    body: JSON.stringify({ company }),
  });
  if (resp.status === 401) throw new Error("Unauthorized — set admin secret in settings");
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(body || `Remove failed (${resp.status})`);
  }
}

export async function triggerFetch(runner = "any"): Promise<{ actions_url?: string }> {
  const resp = await fetch(`${API}/fetch`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...adminHeaders() },
    body: JSON.stringify({ runner }),
  });
  if (resp.status === 401) throw new Error("Unauthorized — set admin secret in settings");
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error((body as { error?: string }).error || `Fetch trigger failed (${resp.status})`);
  }
  return resp.json();
}

export function fmtAmount(usd: number | null, raw?: string): string {
  const trim = (s: string) => s.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  if (usd == null) return raw && /undisclos/i.test(raw) ? "n/d" : raw || "n/d";
  if (usd >= 1e9) return `$${trim((usd / 1e9).toFixed(2))}B`;
  if (usd >= 1e6) return `$${trim((usd / 1e6).toFixed(1))}M`;
  return `$${Math.round(usd / 1e3)}K`;
}

export function dayLabel(iso: string): string {
  const d = new Date(iso + "T12:00:00Z");
  return d
    .toLocaleDateString("en-US", {
      day: "2-digit",
      month: "short",
      year: "numeric",
      timeZone: "UTC",
    })
    .toUpperCase();
}

const SRC: Record<string, string> = {
  finsmes: "FinSMEs",
  fintech_global: "fintech.global",
  aifunding_me: "aifunding.me",
};

export function srcLabel(s?: string) {
  return (s && SRC[s]) || s || "";
}

const SECTOR: Record<string, string> = {
  ai_fintech: "AI Fintech",
  fintech: "Fintech",
  financial_services: "Financial Services",
  enabler: "Fintech enabler",
};

export function sectorLabel(s?: string) {
  return (s && SECTOR[s]) || "";
}

/** Stage labels always offered in the stage dropdown, in display order. */
export const BASE_STAGES = [
  "Seed",
  "Series A",
  "Series B",
  "Series C",
  "Series D",
  "Series E",
  "Series F",
  "Series G",
  "Series H",
];

/** Collapse the free-text stage into one canonical label for filtering. */
export function stageKey(stage?: string): string {
  const s = (stage || "").trim();
  if (!s) return "Undisclosed";
  const low = s.toLowerCase();
  if (/^pre[\s-]?seed/.test(low)) return "Pre-Seed";
  if (/^seed/.test(low)) return "Seed";
  if (/^pre[\s-]?(series\s*)?a\b/.test(low)) return "Pre-A";
  const series = low.match(/^series\s+([a-z])\b/);
  if (series) return `Series ${series[1].toUpperCase()}`;
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function filterDeals(
  deals: Deal[],
  mode: string,
  q: string,
  from: string,
  to: string,
  stage = "",
): Deal[] {
  const query = q.toLowerCase();
  const OVER_5M = 5_000_000;
  return deals.filter((d) => {
    if (mode === "priority" && !d.priority) return false;
    if (mode === "disclosed" && d.amount_usd == null) return false;
    if (stage && stageKey(d.stage) !== stage) return false;
    // Over $5M: keep undisclosed (n/d) and disclosed amounts strictly above $5M.
    if (mode === "over5m" && d.amount_usd != null && d.amount_usd <= OVER_5M) {
      return false;
    }
    if (from && d.published_at < from) return false;
    if (to && d.published_at > to) return false;
    if (
      query &&
      ![d.company, d.description, d.investors, d.location, d.stage]
        .join(" ")
        .toLowerCase()
        .includes(query)
    ) {
      return false;
    }
    return true;
  });
}

export async function fetchCareers(): Promise<CareersMap> {
  // The admin secret (if unlocked) makes the server include unverified guesses.
  const resp = await fetch(`${API}/careers`, { headers: adminHeaders() });
  if (!resp.ok) throw new Error(`Failed to load careers (${resp.status})`);
  return resp.json();
}

export async function saveCareers(
  companyKey: string,
  body: { careers_url?: string; accept?: boolean; clear?: boolean },
): Promise<Careers> {
  const resp = await fetch(`${API}/careers/${encodeURIComponent(companyKey)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...adminHeaders() },
    body: JSON.stringify(body),
  });
  if (resp.status === 401) throw new Error("Unauthorized: unlock admin first");
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error((err as { error?: string }).error || `Save failed (${resp.status})`);
  }
  return resp.json();
}

async function adminError(resp: Response, fallback: string): Promise<Error> {
  if (resp.status === 401) return new Error("Unauthorized: unlock admin first");
  const err = await resp.json().catch(() => ({}));
  return new Error((err as { error?: string }).error || `${fallback} (${resp.status})`);
}

/** Admin: add a company that is not in the blotter, with the URL to check on the next run. */
export async function addCareersCompany(company: string, careersUrl: string): Promise<Careers & { company_key: string }> {
  const resp = await fetch(`${API}/careers`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...adminHeaders() },
    body: JSON.stringify({ company, careers_url: careersUrl }),
  });
  if (!resp.ok) throw await adminError(resp, "Could not add the company");
  return resp.json();
}

/** Admin: remove a company that was added on the Jobs page (its roles go with it). */
export async function removeCareersCompany(companyKey: string): Promise<void> {
  const resp = await fetch(`${API}/careers/${encodeURIComponent(companyKey)}`, {
    method: "DELETE",
    headers: adminHeaders(),
  });
  if (!resp.ok) throw await adminError(resp, "Could not remove the company");
}

export const ROLE_TYPES = ["forward_deployed_engineer", "ai_engineer", "product_engineer", "ai_product_engineer"] as const;

const ROLE_LABELS: Record<string, string> = {
  forward_deployed_engineer: "Forward Deployed Engineer",
  ai_product_engineer: "AI Product Engineer",
  product_engineer: "Product Engineer",
  ai_engineer: "AI Engineer",
};

export function roleLabel(roleType: string): string {
  return ROLE_LABELS[roleType] || roleType;
}

export function timeAgo(iso?: string | null): string {
  if (!iso) return "not checked yet";
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return "not checked yet";
  const mins = Math.floor(ms / 60000);
  if (mins < 60) return "checked just now";
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `checked ${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `checked ${days} day${days === 1 ? "" : "s"} ago`;
}
