import { createClient } from "@supabase/supabase-js";
import ws from "ws";

export function supabaseAdmin() {
  const url = (process.env.SUPABASE_URL || "").trim();
  const key = (
    process.env.SUPABASE_SERVICE_ROLE_KEY ||
    process.env.SUPABASE_SECRET_KEY ||
    ""
  ).trim();
  if (!url || !key) {
    throw new Error(
      "Missing Supabase env vars. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Netlify.",
    );
  }
  return createClient(url, key, {
    auth: { persistSession: false, autoRefreshToken: false },
    // Netlify Functions use Node < 22 unless configured; realtime-js needs a WebSocket.
    realtime: { transport: ws as unknown as typeof WebSocket },
  });
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
