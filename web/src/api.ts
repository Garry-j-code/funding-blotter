import type { Deal } from "./types";

const API = "/api";

function adminHeaders(): HeadersInit {
  const secret = localStorage.getItem("blotter_admin_secret");
  return secret ? { Authorization: `Bearer ${secret}` } : {};
}

export async function fetchDeals(windowDays = 30): Promise<Deal[]> {
  const resp = await fetch(`/api/deals?window_days=${windowDays}`);
  if (!resp.ok) {
    const body = await resp.text();
    let detail = body;
    try {
      detail = (JSON.parse(body) as { error?: string }).error || body;
    } catch {
      /* use raw body */
    }
    throw new Error(`Failed to load deals (${resp.status}): ${detail.slice(0, 200)}`);
  }
  return resp.json();
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

export function filterDeals(
  deals: Deal[],
  mode: string,
  q: string,
  date: string,
): Deal[] {
  const query = q.toLowerCase();
  const UNDER_5M = 5_000_000;
  return deals.filter((d) => {
    if (mode === "priority" && !d.priority) return false;
    if (mode === "disclosed" && d.amount_usd == null) return false;
    if (mode === "early" && !/seed|series a/i.test(d.stage || "")) return false;
    // Under $5M: keep undisclosed (n/d) and disclosed amounts strictly below $5M.
    if (mode === "under5m" && d.amount_usd != null && d.amount_usd >= UNDER_5M) {
      return false;
    }
    if (date && d.published_at !== date) return false;
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
