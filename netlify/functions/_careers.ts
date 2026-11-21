// Pure helpers for the careers API (no dependencies, so they are easy to test).
// Keep in step with src/jobs.py: parse_board_url() and validate_url().

export const SUPPORTED_ATS = ["ashby", "greenhouse", "lever", "workable"] as const;
export type Ats = (typeof SUPPORTED_ATS)[number];

/** Normalise an admin-supplied careers URL, or throw. http/https only. */
export function validateCareersUrl(input: unknown): string {
  const url = typeof input === "string" ? input.trim() : "";
  if (!url || url.length > 500 || /\s/.test(url)) {
    throw new Error("URL is empty, too long, or contains spaces");
  }
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    throw new Error("URL is not valid");
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") {
    throw new Error("URL must start with http:// or https://");
  }
  if (!u.hostname || !u.hostname.includes(".")) {
    throw new Error("URL has no valid host");
  }
  if (u.username || u.password) {
    throw new Error("URL must not contain a username or password");
  }
  return url;
}

/** (ats, slug) for a supported job-board URL, else null. */
export function parseBoardUrl(url: string): { ats: Ats; slug: string } | null {
  let u: URL;
  try {
    u = new URL(url.trim());
  } catch {
    return null;
  }
  const host = u.hostname.toLowerCase();
  const parts = u.pathname.split("/").filter(Boolean);
  const first = parts[0] || "";

  if (host === "jobs.ashbyhq.com" && first) return { ats: "ashby", slug: first };

  if (
    ["boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"].includes(host)
  ) {
    if (first === "embed") {
      const slug = u.searchParams.get("for") || "";
      return slug ? { ats: "greenhouse", slug } : null;
    }
    return first ? { ats: "greenhouse", slug: first } : null;
  }

  if (["jobs.lever.co", "jobs.eu.lever.co"].includes(host) && first) return { ats: "lever", slug: first };

  if (host === "apply.workable.com" && first) return { ats: "workable", slug: first };
  const labels = host.split(".");
  if (host.endsWith(".workable.com") && labels.length === 3 && !["www", "apply"].includes(labels[0])) {
    return { ats: "workable", slug: labels[0] };
  }
  return null;
}

export const NEW_BADGE_DAYS = 7; // keep equal to jobs.new_badge_days in config.yaml

/** A role is "new" for a few days after we first saw it. */
export function isNew(firstSeen: string | null | undefined, now: Date = new Date()): boolean {
  if (!firstSeen) return false;
  const seen = new Date(`${firstSeen}T00:00:00Z`).getTime();
  if (Number.isNaN(seen)) return false;
  return now.getTime() - seen <= NEW_BADGE_DAYS * 24 * 3600 * 1000;
}

// ---------------------------------------------------------------------------
// Company names -> company_key. Keep in step with company_key() in src/store.py
// (tests/fixtures/company_keys.json holds shared examples).
// ---------------------------------------------------------------------------
// Python's \b treats any letter or digit as a word character, so use lookarounds with unicode classes.
const NOT_WORD_BEFORE = "(?<![\\p{L}\\p{N}_])";
const NOT_WORD_AFTER = "(?![\\p{L}\\p{N}_])";
const SUFFIXES = new RegExp(
  `${NOT_WORD_BEFORE}(inc|llc|ltd|limited|corp|corporation|co|plc|gmbh|sa|nv|bv|ag|` +
    `technologies|technology|labs|lab|group|holdings|systems|software|platform|ai|io)${NOT_WORD_AFTER}\\.?`,
  "giu",
);
const DOMAIN_TAIL = new RegExp(`\\.(ai|io|com|co)${NOT_WORD_AFTER}`, "giu");

export function companyKey(name: string): string {
  const raw = name || "";
  let s = raw.toLowerCase();
  s = s.replace(/[’']/g, "");
  s = s.replace(DOMAIN_TAIL, " ");
  s = s.replace(SUFFIXES, " ");
  s = s.replace(/[^a-z0-9]+/g, "");
  return s || raw.toLowerCase().replace(/[^a-z0-9]+/g, "");
}
