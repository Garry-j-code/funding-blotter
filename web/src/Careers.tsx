import { useEffect, useState } from "react";
import { roleLabel, saveCareers, timeAgo } from "./api";
import type { Careers, Deal } from "./types";

const isHttp = (u?: string | null) => Boolean(u && /^https?:\/\//i.test(u));

function postedLabel(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

interface Props {
  deal: Deal;
  entry?: Careers;
  isAdmin: boolean;
  onSaved: (companyKey: string, entry: Careers) => void;
}

/** The inline "Careers" panel under a deal card: URL, open roles, and (admin) the URL editor. */
export default function CareersPanel({ deal, entry, isAdmin, onSaved }: Props) {
  const [url, setUrl] = useState(entry?.careers_url ?? "");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; kind: "ok" | "err" } | null>(null);

  useEffect(() => {
    setUrl(entry?.careers_url ?? "");
  }, [entry?.careers_url]);

  const save = async (body: { careers_url?: string; accept?: boolean; clear?: boolean }, okText: string) => {
    setBusy(true);
    setMsg(null);
    try {
      const saved = await saveCareers(deal.company_key, body);
      onSaved(deal.company_key, saved);
      setMsg({ text: okText, kind: "ok" });
    } catch (e) {
      setMsg({ text: e instanceof Error ? e.message : String(e), kind: "err" });
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(entry?.careers_url ?? "");
      setMsg({ text: "Copied.", kind: "ok" });
    } catch {
      setMsg({ text: "Could not copy. Select the link instead.", kind: "err" });
    }
  };

  const jobs = entry?.jobs ?? [];
  const unverified = isAdmin && entry && entry.verified === false;
  const unsupported = entry?.roles_tracked === false;

  return (
    <div className="careers-panel" role="region" aria-label={`Careers at ${deal.company}`}>
      {entry?.careers_url ? (
        <div className="careers-url">
          {isHttp(entry.careers_url) ? (
            <a href={entry.careers_url} target="_blank" rel="noopener noreferrer">
              {entry.careers_url}
            </a>
          ) : (
            <span>{entry.careers_url}</span>
          )}
          <button type="button" className="mini-btn" onClick={copy}>
            Copy
          </button>
          <span className="careers-age">{timeAgo(entry.last_checked)}</span>
        </div>
      ) : (
        <div className="careers-empty">No careers page found yet.</div>
      )}

      {unverified && (
        <div className="careers-warn">
          Unverified guess: this board may belong to another company.
          <button type="button" className="mini-btn" disabled={busy} onClick={() => save({ accept: true }, "Accepted.")}>
            Accept
          </button>
        </div>
      )}

      {entry?.careers_url && unsupported && (
        <div className="careers-empty">Saved page. Open roles are not fetched automatically for this site.</div>
      )}
      {entry?.careers_url && !unsupported && jobs.length === 0 && (
        <div className="careers-empty">No matching open roles right now.</div>
      )}
      {jobs.length > 0 && (
        <ul className="job-list">
          {jobs.map((j) => (
            <li key={j.id} className="job-row">
              <a href={j.url} target="_blank" rel="noopener noreferrer">
                {j.title}
              </a>
              {j.is_new && <span className="badge-new">New</span>}
              <span className="job-meta">
                <span className="chip-role">{roleLabel(j.role_type)}</span>
                {j.location && <span>{j.location}</span>}
                {postedLabel(j.posted_at) && <span>posted {postedLabel(j.posted_at)}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}

      {isAdmin && (
        <form
          className="careers-edit"
          onSubmit={(e) => {
            e.preventDefault();
            save({ careers_url: url }, "Saved. Open roles appear after the next daily run.");
          }}
        >
          <label htmlFor={`cu-${deal.company_key}`}>Careers URL</label>
          <input
            id={`cu-${deal.company_key}`}
            type="url"
            value={url}
            placeholder="https://jobs.ashbyhq.com/company"
            onChange={(e) => setUrl(e.target.value)}
          />
          <button type="submit" className="mini-btn" disabled={busy || !url.trim()}>
            Save
          </button>
          {entry?.source === "manual" && (
            <button
              type="button"
              className="mini-btn"
              disabled={busy}
              onClick={() => save({ clear: true }, "Reset. The next run will look for the page again.")}
            >
              Reset
            </button>
          )}
          {entry?.status && <span className="careers-age">status: {entry.status}</span>}
        </form>
      )}

      {msg && (
        <div className={`status ${msg.kind}`} aria-live="polite">
          {msg.text}
        </div>
      )}
    </div>
  );
}
