import { createClient } from "@supabase/supabase-js";

export function supabaseAdmin() {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) {
    throw new Error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set");
  }
  return createClient(url, key);
}

export function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
    },
  });
}

export function corsPreflight() {
  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, DELETE, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
    },
  });
}

export function requireAdmin(req: Request) {
  const secret = process.env.ADMIN_SECRET;
  if (!secret) return false;
  const auth = req.headers.get("authorization") || "";
  return auth === `Bearer ${secret}`;
}

export const DEAL_FIELDS = [
  "company",
  "company_key",
  "description",
  "amount_usd",
  "amount_raw",
  "stage",
  "location",
  "investors",
  "source",
  "url",
  "published_at",
  "priority",
  "score",
  "sector_label",
  "sector_reason",
] as const;

export function slimDeal(row: Record<string, unknown>) {
  const out: Record<string, unknown> = {};
  for (const k of DEAL_FIELDS) {
    if (k in row) out[k] = row[k];
  }
  return out;
}
