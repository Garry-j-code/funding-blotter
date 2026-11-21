import { useCallback, useEffect, useMemo, useState } from "react";
import {
  dayLabel,
  fetchCareers,
  fetchDeals,
  fetchMonths,
  BASE_STAGES,
  filterDeals,
  fmtAmount,
  removeDeal,
  sectorLabel,
  srcLabel,
  stageKey,
  triggerFetch,
} from "./api";
import CareersPanel from "./Careers";
import DateRangePicker from "./DateRangePicker";
import JobsPage from "./Jobs";
import type { MonthInfo } from "./api";
import type { Careers, CareersMap, Deal, FilterMode } from "./types";

const MODES: { id: FilterMode; label: string }[] = [
  { id: "all", label: "All" },
  { id: "priority", label: "Flagged" },
  { id: "disclosed", label: "Disclosed" },
  { id: "over5m", label: "Over $5M" },
];

const ALL_TIME = "all";

type View = "blotter" | "jobs";
const viewFromHash = (): View => (window.location.hash === "#/jobs" ? "jobs" : "blotter");

function currentMonth(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function monthBounds(month: string): { from: string; to: string } {
  const [y, m] = month.split("-").map(Number);
  const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
  return { from: `${month}-01`, to: `${month}-${String(last).padStart(2, "0")}` };
}

function monthLabel(month: string): string {
  const [y, m] = month.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString("en-US", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}

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
  const [stage, setStage] = useState("");
  const [q, setQ] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [month, setMonth] = useState(currentMonth);
  const [monthList, setMonthList] = useState<MonthInfo[]>([]);
  const [careers, setCareers] = useState<CareersMap>({});
  const [careersLoaded, setCareersLoaded] = useState(false);
  const [view, setView] = useState<View>(viewFromHash);
  const [busy, setBusy] = useState(false);
  const [isAdmin, setIsAdmin] = useState(hasAdminSecret);

  const load = useCallback(async () => {
    if (dateFrom && dateTo && dateFrom > dateTo) {
      setError("The From date is after the To date.");
      setLoading(false);
      return;
    }
    setLoading(true);
    setError("");
    try {
      // A date range, when set, takes precedence over the month selection.
      const rows =
        dateFrom || dateTo
          ? await fetchDeals({ from: dateFrom, to: dateTo })
          : month === ALL_TIME
            ? await fetchDeals({ windowDays: 0 })
            : await fetchDeals(monthBounds(month));
      setDeals(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [month, dateFrom, dateTo]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    const onHash = () => setView(viewFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    fetchCareers()
      .then(setCareers)
      .catch(() => {
        /* careers are optional; cards just show no careers info */
      })
      .finally(() => setCareersLoaded(true));
  }, [isAdmin]);

  const onCareersRemoved = useCallback((key: string) => {
    setCareers((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
  }, []);

  const onCareersSaved = useCallback((key: string, entry: Careers) => {
    setCareers((prev) => ({ ...prev, [key]: { ...entry, added_at: entry.added_at ?? prev[key]?.added_at } }));
  }, []);

  useEffect(() => {
    fetchMonths()
      .then(setMonthList)
      .catch(() => {
        /* month list is optional; the current month still works */
      });
  }, []);

  // Months that have data, newest first. The current month is always offered,
  // since it is the default even when it has no deals yet.
  const monthOptions = useMemo(() => {
    const byMonth = new Map(monthList.map((m) => [m.month, m.count]));
    const cur = currentMonth();
    if (!byMonth.has(cur)) byMonth.set(cur, 0);
    return [...byMonth.entries()]
      .sort((a, b) => b[0].localeCompare(a[0]))
      .map(([m, count]) => ({ month: m, count }));
  }, [monthList]);

  const rangeActive = Boolean(dateFrom || dateTo);

  // Seed + Series A-H are always offered; any other stage seen in the loaded
  // deals (Pre-Seed, Debt, Strategic, ...) is added after them. The selected
  // stage is kept even if the current month has no deals of that stage.
  const stageOptions = useMemo(() => {
    const seen = new Map<string, number>();
    for (const d of deals) {
      const k = stageKey(d.stage);
      seen.set(k, (seen.get(k) || 0) + 1);
    }
    if (stage && !BASE_STAGES.includes(stage)) seen.set(stage, seen.get(stage) || 0);
    const others = [...seen.entries()]
      .filter(([k]) => !BASE_STAGES.includes(k))
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([k]) => k);
    return [...BASE_STAGES, ...others];
  }, [deals, stage]);

  const visible = useMemo(
    () => filterDeals(deals, mode, q, dateFrom, dateTo, stage),
    [deals, mode, q, dateFrom, dateTo, stage],
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

  // Shown in the viewer's own time zone (taken from the browser), e.g. "20 NOV 2026 17:56 GMT-6".
  const built = new Date()
    .toLocaleString("en-GB", {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZoneName: "short",
    })
    .replace(",", "")
    .toUpperCase();

  return (
    <div className="wrap">
      <header className="mast">
        <h1>Funding Blotter</h1>
        <div className="sub">
          finsmes &nbsp;·&nbsp; aifunding.me
          &nbsp;&nbsp;|&nbsp;&nbsp; built <b>{built}</b>
          {view === "blotter" ? (
            <>
              &nbsp;&nbsp;|&nbsp;&nbsp; <b>{visible.length} rounds</b>
            </>
          ) : (
            <>&nbsp;&nbsp;|&nbsp;&nbsp; open roles from company job boards</>
          )}
        </div>
        <nav className="nav" aria-label="Pages">
          <a href="#/" aria-current={view === "blotter" ? "page" : undefined}>
            Blotter
          </a>
          <a href="#/jobs" aria-current={view === "jobs" ? "page" : undefined}>
            Jobs
          </a>
        </nav>
        <div className="rule" />
      </header>

      {view === "jobs" ? (
        <JobsPage
          careers={careers}
          loaded={careersLoaded}
          isAdmin={isAdmin}
          onSetSecret={onSetSecret}
          onSaved={onCareersSaved}
          onRemoved={onCareersRemoved}
        />
      ) : (
        <>

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
          <select
            className={`chip chip-select${stage ? " on" : ""}`}
            aria-label="Filter by funding stage"
            value={stage}
            onChange={(e) => setStage(e.target.value)}
          >
            <option value="">Stage: All</option>
            {stageOptions.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
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
            <label htmlFor="month-filter">Month</label>
            <select
              id="month-filter"
              className="date-filter"
              aria-label="Month to show"
              value={month}
              disabled={rangeActive}
              title={rangeActive ? "A date range is set. Clear it to use the month." : undefined}
              onChange={(e) => setMonth(e.target.value)}
            >
              {monthOptions.map((m) => (
                <option key={m.month} value={m.month}>
                  {monthLabel(m.month)} ({m.count})
                </option>
              ))}
              <option value={ALL_TIME}>All time</option>
            </select>
          </div>
          <div className="date-wrap">
            <label htmlFor="date-range">Dates</label>
            <DateRangePicker
              id="date-range"
              from={dateFrom}
              to={dateTo}
              onChange={(f, t) => {
                setDateFrom(f);
                setDateTo(t);
              }}
            />
            <button
              type="button"
              className="date-clear"
              disabled={!rangeActive}
              onClick={() => {
                setDateFrom("");
                setDateTo("");
              }}
            >
              Clear
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
                  careers={careers[d.company_key]}
                  onCareersSaved={onCareersSaved}
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
        </>
      )}
    </div>
  );
}

function DealRow({
  deal: d,
  index: i,
  onRemove,
  busy,
  canRemove,
  careers,
  onCareersSaved,
}: {
  deal: Deal;
  index: number;
  onRemove: (d: Deal) => void;
  busy: boolean;
  canRemove: boolean;
  careers?: Careers;
  onCareersSaved: (companyKey: string, entry: Careers) => void;
}) {
  const [showCareers, setShowCareers] = useState(false);
  const roleCount = careers?.jobs.length ?? 0;
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
          <button
            type="button"
            className="careers-btn"
            aria-expanded={showCareers}
            title="Careers page and open roles"
            onClick={() => setShowCareers((v) => !v)}
          >
            Careers
            {roleCount > 0 && <span className="careers-count">{roleCount}</span>}
          </button>
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
        {showCareers && (
          <CareersPanel deal={d} entry={careers} isAdmin={canRemove} onSaved={onCareersSaved} />
        )}
      </div>
    </div>
  );
}
