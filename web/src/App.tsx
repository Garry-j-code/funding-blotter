import { useCallback, useEffect, useMemo, useState } from "react";
import {
  dayLabel,
  fetchDeals,
  filterDeals,
  fmtAmount,
  removeDeal,
  sectorLabel,
  srcLabel,
  triggerFetch,
} from "./api";
import type { Deal, FilterMode } from "./types";

const MODES: { id: FilterMode; label: string }[] = [
  { id: "all", label: "All" },
  { id: "priority", label: "Flagged" },
  { id: "early", label: "Seed & A" },
  { id: "disclosed", label: "Disclosed" },
];

const SECRET_KEY = "blotter_admin_secret";

function hasAdminSecret() {
  return Boolean(localStorage.getItem(SECRET_KEY)?.trim());
}

export default function App() {
  const [deals, setDeals] = useState<Deal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [statusKind, setStatusKind] = useState<"" | "ok" | "err">("");
  const [mode, setMode] = useState<FilterMode>("all");
  const [q, setQ] = useState("");
  const [date, setDate] = useState("");
  const [busy, setBusy] = useState(false);
  const [isAdmin, setIsAdmin] = useState(hasAdminSecret);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const rows = await fetchDeals(30);
      setDeals(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const dates = useMemo(() => {
    return [...new Set(deals.map((d) => d.published_at).filter(Boolean))].sort();
  }, [deals]);

  const visible = useMemo(
    () => filterDeals(deals, mode, q, date),
    [deals, mode, q, date],
  );

  const groups = useMemo(() => {
    const g: Record<string, Deal[]> = {};
    for (const d of visible) {
      (g[d.published_at] ||= []).push(d);
    }
    return Object.keys(g)
      .sort()
      .reverse()
      .map((day) => ({ day, items: g[day] }));
  }, [visible]);

  const setStatusMsg = (msg: string, kind: "" | "ok" | "err" = "") => {
    setStatus(msg);
    setStatusKind(kind);
  };

  const onRemove = async (d: Deal) => {
    if (!isAdmin) {
      setStatusMsg("Set admin secret in this browser to remove deals.", "err");
      return;
    }
    if (
      !confirm(
        `Remove "${d.company}" from the blotter permanently?\n\nIt will be deleted and blocked from future fetches.`,
      )
    ) {
      return;
    }
    setBusy(true);
    setStatusMsg(`Removing ${d.company}…`);
    try {
      await removeDeal(d.company_key, d.company);
      setDeals((prev) => prev.filter((x) => x.company_key !== d.company_key));
      setStatusMsg(`Removed ${d.company}.`, "ok");
    } catch (e) {
      setStatusMsg(e instanceof Error ? e.message : String(e), "err");
    } finally {
      setBusy(false);
    }
  };

  const onFetch = async () => {
    if (!isAdmin) {
      setStatusMsg("Set admin secret in this browser to fetch deals.", "err");
      return;
    }
    setBusy(true);
    setStatusMsg("Starting fetch on your self-hosted runner…");
    try {
      const res = await triggerFetch("any");
      setStatusMsg(
        `Fetch started. Keep Mac or Windows runner awake. Refresh in 2–3 minutes.${
          res.actions_url ? ` ${res.actions_url}` : ""
        }`,
        "ok",
      );
    } catch (e) {
      setStatusMsg(e instanceof Error ? e.message : String(e), "err");
    } finally {
      setBusy(false);
    }
  };

  const onSetSecret = () => {
    if (isAdmin) {
      localStorage.removeItem(SECRET_KEY);
      setIsAdmin(false);
      setStatusMsg("Admin unlocked cleared for this browser.", "");
      return;
    }
    const next = prompt(
      "Paste ADMIN_SECRET (same value as Netlify env var).\nStored in this browser only — other visitors never see it.",
    );
    if (next === null) return;
    if (!next.trim()) {
      setStatusMsg("No secret entered.", "err");
      return;
    }
    localStorage.setItem(SECRET_KEY, next.trim());
    setIsAdmin(true);
    setStatusMsg("Admin unlocked in this browser only. Fetch and Remove are now available here.", "ok");
  };

  const built = new Date().toUTCString().slice(5, 22).toUpperCase() + " UTC";

  return (
    <div className="wrap">
      <header className="mast">
        <h1>Funding Blotter</h1>
        <div className="sub">
          finsmes &nbsp;·&nbsp; aifunding.me
          &nbsp;&nbsp;|&nbsp;&nbsp; built <b>{built}</b>
          &nbsp;&nbsp;|&nbsp;&nbsp; <b>{visible.length} rounds</b>
        </div>
        <div className="rule" />
      </header>

      <div className="controls">
        <div className="chips">
          {MODES.map((m) => (
            <button
              key={m.id}
              type="button"
              className="chip"
              aria-pressed={mode === m.id}
              onClick={() => setMode(m.id)}
            >
              {m.label}
            </button>
          ))}
        </div>
        <div className="toolbar">
          <input
            className="search-input"
            type="search"
            placeholder="company, investor, city"
            aria-label="Search rounds"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <div className="date-wrap">
            <label htmlFor="date-filter">Date</label>
            <input
              id="date-filter"
              className="date-filter"
              type="date"
              aria-label="Filter by date"
              min={dates[0]}
              max={dates[dates.length - 1]}
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />
            <button
              type="button"
              className="date-clear"
              disabled={!date}
              onClick={() => setDate("")}
            >
              All
            </button>
          </div>
          <div className="actions">
            {isAdmin && (
              <button
                type="button"
                className="fetch-btn"
                disabled={busy}
                onClick={onFetch}
              >
                Fetch today&apos;s deals
              </button>
            )}
            <button type="button" className="token-btn" onClick={onSetSecret}>
              {isAdmin ? "Clear admin" : "Unlock admin"}
            </button>
            <button type="button" className="token-btn" onClick={load} disabled={loading}>
              Refresh
            </button>
          </div>
        </div>
        {status && (
          <div className={`status ${statusKind}`} aria-live="polite">
            {status}
          </div>
        )}
      </div>

      <main id="feed">
        {loading && <div className="empty">Loading…</div>}
        {error && <div className="empty">{error}</div>}
        {!loading && !error && visible.length === 0 && (
          <div className="empty">No rounds match. Widen the filter or clear the search.</div>
        )}
        {!loading &&
          !error &&
          groups.map(({ day, items }) => (
            <section key={day}>
              <div className="daybar">
                <span>{dayLabel(day)}</span>
                <span>
                  {items.length} · {items.filter((d) => d.priority).length} flagged
                </span>
              </div>
              {items.map((d, i) => (
                <DealRow
                  key={`${d.company_key}-${d.url || i}`}
                  deal={d}
                  index={i}
                  onRemove={onRemove}
                  busy={busy}
                  canRemove={isAdmin}
                />
              ))}
            </section>
          ))}
      </main>

      <div className="rule thin" />
      <p className="foot">
        Red rail = matches your sector and location filters. n/d = amount not disclosed.
        {isAdmin ? (
          <>
            &nbsp;·&nbsp; <b>Fetch</b> / <b>Remove</b> are unlocked in this browser only
            (admin secret stays on this device).
          </>
        ) : (
          <>
            &nbsp;·&nbsp; Read-only. Use <b>Unlock admin</b> in your browser to fetch or remove.
          </>
        )}
      </p>
    </div>
  );
}

function DealRow({
  deal: d,
  index: i,
  onRemove,
  busy,
  canRemove,
}: {
  deal: Deal;
  index: number;
  onRemove: (d: Deal) => void;
  busy: boolean;
  canRemove: boolean;
}) {
  const cls = ["deal", d.priority ? "pri" : "", d.score < 0 ? "dim" : ""]
    .filter(Boolean)
    .join(" ");
  const amt = fmtAmount(d.amount_usd, d.amount_raw);
  const sec = sectorLabel(d.sector_label);

  return (
    <div className={cls} style={{ animationDelay: `${Math.min(i * 14, 260)}ms` }}>
      <div className="gutter" />
      <div className="body">
        <div className="headline">
          <div className="name">
            {d.url ? (
              <a href={d.url} target="_blank" rel="noopener noreferrer">
                {d.company}
              </a>
            ) : (
              d.company
            )}
          </div>
          <div className="figures">
            <div className={`amt${d.amount_usd == null ? " nd" : ""}`}>{amt}</div>
            <div className="stage">{d.stage || ""}</div>
          </div>
        </div>
        {d.description && <div className="desc">{d.description}</div>}
        <div className="meta">
          {d.location && <span>{d.location}</span>}
          {sec && (
            <span className="sector" title={d.sector_reason || ""}>
              {sec}
            </span>
          )}
          {d.investors && <span className="inv">{d.investors}</span>}
          {d.url ? (
            <a className="src" href={d.url} target="_blank" rel="noopener noreferrer">
              via {srcLabel(d.source)}
            </a>
          ) : (
            <span className="src">via {srcLabel(d.source)}</span>
          )}
          {canRemove && (
            <button
              type="button"
              className="remove-btn"
              disabled={busy}
              title="Remove from blotter permanently"
              onClick={() => onRemove(d)}
            >
              Remove
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
