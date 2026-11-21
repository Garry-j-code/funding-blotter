import { useMemo, useState } from "react";
import { addCareersCompany, ROLE_TYPES, removeCareersCompany, roleLabel, saveCareers, timeAgo } from "./api";
import type { Careers, CareersMap } from "./types";

interface Props {
  careers: CareersMap;
  loaded: boolean;
  isAdmin: boolean;
  onSetSecret: () => void;
  onSaved: (companyKey: string, entry: Careers) => void;
  onRemoved: (companyKey: string) => void;
}

interface Row {
  id: number;
  key: string;
  company: string;
  careersUrl: string | null;
  title: string;
  url: string;
  location: string | null;
  roleType: string;
  firstSeen: string;
  postedAt: string | null;
  isNew: boolean;
  unverified: boolean;
  addedAt: string;
}

interface Group {
  key: string;
  company: string;
  careersUrl: string | null;
  addedAt: string;
  unverified: boolean;
  rows: Row[];
}

const prettyDate = (iso?: string | null) => {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
};

const prettyDay = (day: string) => {
  const d = new Date(`${day}T00:00:00`);
  return Number.isNaN(d.getTime()) ? day : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
};

function statusText(e: Careers): string {
  if (e.needs_refresh) return "Saved. Will be checked on the next run.";
  switch (e.status) {
    case "ok":
      return `Reading roles (${e.jobs.length} matching). ${timeAgo(e.last_checked)}`;
    case "unsupported":
      return `Nothing readable on this page, so no roles listed. ${timeAgo(e.last_checked)}`;
    case "error":
      return `Last check failed. ${timeAgo(e.last_checked)}`;
    default:
      return "Waiting for the next run.";
  }
}

/** The Jobs page: every open role we have fetched, plus (admin) a form to add a careers URL for a company. */
export default function JobsPage({ careers, loaded, isAdmin, onSetSecret, onSaved, onRemoved }: Props) {
  const [role, setRole] = useState("");
  const [company, setCompany] = useState("");
  const [q, setQ] = useState("");
  const [onlyNew, setOnlyNew] = useState(false);

  const rows = useMemo<Row[]>(() => {
    const out: Row[] = [];
    for (const [key, e] of Object.entries(careers)) {
      for (const j of e.jobs || []) {
        out.push({
          id: j.id, key, company: e.company || key, careersUrl: e.careers_url, title: j.title, url: j.url,
          location: j.location ?? null, roleType: j.role_type, firstSeen: j.first_seen, postedAt: j.posted_at ?? null,
          isNew: j.is_new, unverified: isAdmin && e.verified === false, addedAt: e.added_at ?? "",
        });
      }
    }
    // Within a company: newest first (the day we first saw the role, then the date the company posted it).
    out.sort(
      (a, b) =>
        b.firstSeen.localeCompare(a.firstSeen) ||
        (b.postedAt ?? "").localeCompare(a.postedAt ?? "") ||
        a.title.localeCompare(b.title),
    );
    return out;
  }, [careers, isAdmin]);

  const companies = useMemo(
    () => [...new Map(rows.map((r) => [r.key, r.company])).entries()].sort((a, b) => a[1].localeCompare(b[1])),
    [rows],
  );

  const query = q.trim().toLowerCase();
  const shown = rows.filter(
    (r) =>
      (!role || r.roleType === role) &&
      (!company || r.key === company) &&
      (!onlyNew || r.isNew) &&
      (!query || `${r.title} ${r.company} ${r.location ?? ""}`.toLowerCase().includes(query)),
  );
  // Group by company; companies are ordered by the date they were added, newest first.
  const groups = useMemo<Group[]>(() => {
    const byKey = new Map<string, Group>();
    for (const r of shown) {
      let g = byKey.get(r.key);
      if (!g) {
        g = { key: r.key, company: r.company, careersUrl: r.careersUrl, addedAt: r.addedAt, unverified: r.unverified, rows: [] };
        byKey.set(r.key, g);
      }
      g.rows.push(r);
    }
    return [...byKey.values()].sort((a, b) => b.addedAt.localeCompare(a.addedAt) || a.company.localeCompare(b.company));
  }, [shown]);
  const shownCompanies = groups.length;

  return (
    <main id="jobs-page">
      <div className="controls">
        <div className="chips">
          <button type="button" className="chip" aria-pressed={onlyNew} onClick={() => setOnlyNew((v) => !v)}>
            New
          </button>
          <select
            className={`chip chip-select${role ? " on" : ""}`}
            aria-label="Filter by role"
            value={role}
            onChange={(e) => setRole(e.target.value)}
          >
            <option value="">Role: All</option>
            {ROLE_TYPES.map((r) => (
              <option key={r} value={r}>
                {roleLabel(r)}
              </option>
            ))}
          </select>
          <select
            className={`chip chip-select${company ? " on" : ""}`}
            aria-label="Filter by company"
            value={company}
            onChange={(e) => setCompany(e.target.value)}
          >
            <option value="">Company: All</option>
            {companies.map(([key, name]) => (
              <option key={key} value={key}>
                {name}
              </option>
            ))}
          </select>
        </div>
        <div className="toolbar">
          <input
            className="search-input"
            type="search"
            placeholder="title, company, location"
            aria-label="Search roles"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <div className="actions">
            <button type="button" className="token-btn" onClick={onSetSecret}>
              {isAdmin ? "Clear admin" : "Unlock admin"}
            </button>
          </div>
        </div>
      </div>

      {isAdmin && <AdminCompanies careers={careers} onSaved={onSaved} onRemoved={onRemoved} />}

      <div className="daybar">
        <span>Open roles</span>
        <span>
          {shown.length} role{shown.length === 1 ? "" : "s"} · {shownCompanies} compan{shownCompanies === 1 ? "y" : "ies"}
        </span>
      </div>

      {!loaded && <div className="empty">Loading…</div>}
      {loaded && shown.length === 0 && (
        <div className="empty">
          {rows.length === 0 ? "No open roles have been fetched yet." : "No roles match. Widen the filters or clear the search."}
        </div>
      )}
      {groups.map((g) => (
        <section key={g.key} className="jobs-group">
          <div className="jobs-group-head">
            {g.careersUrl ? (
              <a className="jobs-company" href={g.careersUrl} target="_blank" rel="noopener noreferrer">
                {g.company}
              </a>
            ) : (
              <span className="jobs-company">{g.company}</span>
            )}
            {g.addedAt && <span className="careers-age">added {prettyDay(g.addedAt)}</span>}
            {g.unverified && <span className="careers-warn">unverified board</span>}
            <span className="careers-age">
              {g.rows.length} role{g.rows.length === 1 ? "" : "s"}
            </span>
          </div>
          <ul className="jobs-list">
            {g.rows.map((r) => (
              <li key={r.id} className="jobs-row">
                <a className="jobs-title" href={r.url} target="_blank" rel="noopener noreferrer">
                  {r.title}
                </a>
                <div className="job-meta">
                  {r.location && <span>{r.location}</span>}
                  <span className="chip-role">{roleLabel(r.roleType)}</span>
                  {r.isNew && <span className="badge-new">New</span>}
                  <span>{prettyDate(r.postedAt || r.firstSeen)}</span>
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </main>
  );
}

function AdminCompanies({
  careers,
  onSaved,
  onRemoved,
}: Pick<Props, "careers" | "onSaved" | "onRemoved">) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; kind: "ok" | "err" } | null>(null);

  const mine = Object.entries(careers)
    .filter(([, e]) => e.standalone)
    .sort((a, b) => (b[1].added_at ?? "").localeCompare(a[1].added_at ?? "") || (a[1].company || a[0]).localeCompare(b[1].company || b[0]));

  const add = async (ev: React.FormEvent) => {
    ev.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const saved = await addCareersCompany(name, url);
      onSaved(saved.company_key, saved);
      setName("");
      setUrl("");
      setMsg({ text: "Saved. The next run will check this URL for relevant roles.", kind: "ok" });
    } catch (e) {
      setMsg({ text: e instanceof Error ? e.message : String(e), kind: "err" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="admin-jobs">
      <summary className="daybar">
        <span>Add a careers URL</span>
        <span>admin</span>
      </summary>
      <div className="admin-body">
      <p className="careers-empty">
        For a company that is not in the blotter. The next daily run reads the URL and lists matching roles. For a company
        already in the blotter, use the Careers panel on its card.
      </p>
      <form className="careers-edit" onSubmit={add}>
        <label htmlFor="add-company">Company</label>
        <input id="add-company" value={name} maxLength={100} onChange={(e) => setName(e.target.value)} placeholder="Acme" required />
        <label htmlFor="add-url">URL</label>
        <input
          id="add-url"
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://jobs.ashbyhq.com/acme or https://acme.com/careers"
          required
        />
        <button type="submit" className="mini-btn" disabled={busy || !name.trim() || !url.trim()}>
          Add
        </button>
      </form>
      {msg && <div className={`status ${msg.kind}`} role={msg.kind === "err" ? "alert" : "status"}>{msg.text}</div>}

      {mine.length > 0 && (
        <details className="admin-details">
          <summary>Companies you added ({mine.length}), newest first</summary>
          <ul className="admin-list">
            {mine.map(([key, e]) => (
              <AdminRow key={key} companyKey={key} entry={e} onSaved={onSaved} onRemoved={onRemoved} />
            ))}
          </ul>
        </details>
      )}
      </div>
    </details>
  );
}

function AdminRow({
  companyKey,
  entry,
  onSaved,
  onRemoved,
}: {
  companyKey: string;
  entry: Careers;
  onSaved: Props["onSaved"];
  onRemoved: Props["onRemoved"];
}) {
  const [url, setUrl] = useState(entry.careers_url ?? "");
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [msg, setMsg] = useState<{ text: string; kind: "ok" | "err" } | null>(null);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
    } catch (e) {
      setMsg({ text: e instanceof Error ? e.message : String(e), kind: "err" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="admin-row">
      <div className="admin-row-head">
        <b>{entry.company || companyKey}</b>
        <span className="careers-age">{statusText(entry)}</span>
      </div>
      <div className="careers-edit">
        <input aria-label={`Careers URL for ${entry.company || companyKey}`} type="url" value={url} onChange={(e) => setUrl(e.target.value)} />
        <button
          type="button"
          className="mini-btn"
          disabled={busy || !url.trim() || url.trim() === (entry.careers_url ?? "")}
          onClick={() =>
            run(async () => {
              onSaved(companyKey, await saveCareers(companyKey, { careers_url: url }));
              setMsg({ text: "Saved. Will be checked on the next run.", kind: "ok" });
            })
          }
        >
          Save
        </button>
        {confirm ? (
          <>
            <button
              type="button"
              className="mini-btn"
              disabled={busy}
              onClick={() => run(async () => { await removeCareersCompany(companyKey); onRemoved(companyKey); })}
            >
              Confirm remove
            </button>
            <button type="button" className="mini-btn" disabled={busy} onClick={() => setConfirm(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button type="button" className="mini-btn" disabled={busy} onClick={() => setConfirm(true)}>
            Remove
          </button>
        )}
      </div>
      {msg && <div className={`status ${msg.kind}`} role={msg.kind === "err" ? "alert" : "status"}>{msg.text}</div>}
    </li>
  );
}
