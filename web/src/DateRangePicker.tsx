import { useEffect, useMemo, useRef, useState } from "react";

interface Props {
  id?: string;
  /** Inclusive YYYY-MM-DD, or "" for none. */
  from: string;
  to: string;
  onChange: (from: string, to: string) => void;
}

const WEEKDAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];
const pad = (n: number) => String(n).padStart(2, "0");
/** month is zero-based. */
const iso = (y: number, month: number, d: number) => `${y}-${pad(month + 1)}-${pad(d)}`;

function parseMonth(s: string): { y: number; m: number } {
  const [y, m] = s.split("-").map(Number);
  return { y, m: m - 1 };
}

function fmt(s: string): string {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-US", {
    month: "short",
    day: "2-digit",
    year: "numeric",
    timeZone: "UTC",
  });
}

function todayIso(): string {
  const d = new Date();
  return iso(d.getFullYear(), d.getMonth(), d.getDate());
}

/**
 * One field that opens a calendar pop-up: click a start date, then an end date.
 * Clicking the same day twice selects a single day. The range is only reported
 * once both ends are chosen.
 */
export default function DateRangePicker({ id, from, to, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const [pendingStart, setPendingStart] = useState("");
  const [hover, setHover] = useState("");
  const [view, setView] = useState(() => parseMonth(from || todayIso()));
  const ref = useRef<HTMLDivElement>(null);
  const today = todayIso();

  const close = () => {
    setOpen(false);
    setPendingStart("");
    setHover("");
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggle = () => {
    if (open) {
      close();
      return;
    }
    setView(parseMonth(from || today));
    setOpen(true);
  };

  const shiftMonth = (delta: number) =>
    setView((v) => {
      const idx = v.y * 12 + v.m + delta;
      return { y: Math.floor(idx / 12), m: ((idx % 12) + 12) % 12 };
    });

  const pick = (day: string) => {
    if (!pendingStart) {
      setPendingStart(day);
      setHover(day);
      return;
    }
    const [a, b] = day < pendingStart ? [day, pendingStart] : [pendingStart, day];
    onChange(a, b);
    close();
  };

  // Range to highlight: the in-progress selection, otherwise the applied one.
  const [hiFrom, hiTo] = pendingStart
    ? [pendingStart, hover || pendingStart].sort()
    : [from, to || from];

  const cells = useMemo(() => {
    const lead = new Date(Date.UTC(view.y, view.m, 1)).getUTCDay();
    const count = new Date(Date.UTC(view.y, view.m + 1, 0)).getUTCDate();
    const out: (string | null)[] = Array(lead).fill(null);
    for (let d = 1; d <= count; d++) out.push(iso(view.y, view.m, d));
    return out;
  }, [view]);

  const monthTitle = new Date(Date.UTC(view.y, view.m, 1)).toLocaleDateString("en-US", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });

  const label = !from
    ? "Select dates"
    : !to || to === from
      ? fmt(from)
      : `${fmt(from)} → ${fmt(to)}`;

  return (
    <div className="rp" ref={ref}>
      <button
        id={id}
        type="button"
        className={`date-filter rp-trigger${from ? " set" : ""}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={toggle}
      >
        {label}
      </button>
      {open && (
        <div className="rp-pop" role="dialog" aria-label="Choose a date range">
          <div className="rp-head">
            <button type="button" className="rp-nav" aria-label="Previous month" onClick={() => shiftMonth(-1)}>
              ‹
            </button>
            <span className="rp-title">{monthTitle}</span>
            <button type="button" className="rp-nav" aria-label="Next month" onClick={() => shiftMonth(1)}>
              ›
            </button>
          </div>
          <div className="rp-grid rp-dow" aria-hidden="true">
            {WEEKDAYS.map((w) => (
              <span key={w}>{w}</span>
            ))}
          </div>
          <div className="rp-grid" onMouseLeave={() => setHover(pendingStart)}>
            {cells.map((day, i) =>
              day ? (
                <button
                  key={day}
                  type="button"
                  className={[
                    "rp-day",
                    day === today ? "today" : "",
                    hiFrom && day >= hiFrom && day <= hiTo ? "in" : "",
                    day === hiFrom ? "edge" : "",
                    day === hiTo ? "edge" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  aria-label={fmt(day)}
                  onClick={() => pick(day)}
                  onMouseEnter={() => pendingStart && setHover(day)}
                >
                  {Number(day.slice(8))}
                </button>
              ) : (
                <span key={`blank-${i}`} />
              ),
            )}
          </div>
          <div className="rp-hint" aria-live="polite">
            {pendingStart ? `Start ${fmt(pendingStart)}. Now pick the end date.` : "Pick the start date."}
          </div>
        </div>
      )}
    </div>
  );
}
