"""SQLite persistence plus cross-source dedupe and priority scoring.

Dedupe matters more than you'd think: all three sources cover the same
rounds, so without it every deal shows up two or three times.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS deals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_key   TEXT NOT NULL,
    company       TEXT NOT NULL,
    amount_usd    REAL,
    amount_raw    TEXT,
    stage         TEXT,
    location      TEXT,
    description   TEXT,
    investors     TEXT,
    source        TEXT,
    url           TEXT,
    published_at  TEXT,
    first_seen    TEXT,
    priority      INTEGER DEFAULT 0,
    score         INTEGER DEFAULT 0,
    extracted_by  TEXT,
    sector_label  TEXT,
    sector_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_key   ON deals(company_key, published_at);
CREATE INDEX IF NOT EXISTS idx_pub   ON deals(published_at);

-- Per-company sector classification cache, so repeated daily runs don't
-- re-search or re-spend on companies already seen. Keyed independently of
-- deals rows so a classification survives even if a deal row changes.
CREATE TABLE IF NOT EXISTS company_sector (
    company_key TEXT PRIMARY KEY,
    label       TEXT,
    reason      TEXT,
    enriched_at TEXT
);

-- Article URLs already fetched/classified. Daily runs skip these so we don't
-- re-download or re-enrich the same FinSMEs post when lookback overlaps.
CREATE TABLE IF NOT EXISTS scanned_posts (
    url          TEXT PRIMARY KEY,
    company_key  TEXT,
    published_at TEXT,
    scanned_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_scanned_pub ON scanned_posts(published_at);

-- Companies manually removed from the blotter. Future pipeline runs skip them
-- even if a new article URL appears.
CREATE TABLE IF NOT EXISTS blocked_companies (
    company_key TEXT PRIMARY KEY,
    company     TEXT,
    blocked_at  TEXT,
    reason      TEXT
);
"""

# Suffixes and generic tails stripped before comparing company names, so
# "Obin AI" and "Obin" collapse to the same key.
SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|co|plc|gmbh|sa|nv|bv|ag|"
    r"technologies|technology|labs|lab|group|holdings|systems|"
    r"software|platform|ai|io)\b\.?",
    re.IGNORECASE,
)


def company_key(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"[\u2019']", "", s)
    s = re.sub(r"\.(ai|io|com|co)\b", " ", s)
    s = SUFFIXES.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Migrate older DBs that predate the sector columns.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
    for col in ("sector_label", "sector_reason"):
        if col not in cols:
            conn.execute(f"ALTER TABLE deals ADD COLUMN {col} TEXT")
    # Seed scanned_posts from deals already stored (one-time / idempotent).
    conn.execute(
        "INSERT OR IGNORE INTO scanned_posts (url, company_key, published_at, scanned_at) "
        "SELECT url, company_key, published_at, COALESCE(first_seen, ?) "
        "FROM deals WHERE url IS NOT NULL AND url != ''",
        (date.today().isoformat(),),
    )
    conn.commit()
    return conn


def blocked_keys(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT company_key FROM blocked_companies").fetchall()
    return {r["company_key"] for r in rows if r["company_key"]}


def block_company(
    conn: sqlite3.Connection,
    key: str,
    company: str = "",
    reason: str = "",
) -> dict:
    """Permanently block a company and delete all stored deals for it."""
    key = (key or "").strip()
    if not key:
        raise ValueError("company_key is required")
    today = date.today().isoformat()
    conn.execute(
        "INSERT INTO blocked_companies (company_key, company, blocked_at, reason) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(company_key) DO UPDATE SET "
        "company = COALESCE(NULLIF(excluded.company, ''), blocked_companies.company), "
        "blocked_at = excluded.blocked_at, "
        "reason = COALESCE(NULLIF(excluded.reason, ''), blocked_companies.reason)",
        (key, company or key, today, reason),
    )
    deals_removed = conn.execute(
        "DELETE FROM deals WHERE company_key = ?", (key,)
    ).rowcount
    sector_removed = conn.execute(
        "DELETE FROM company_sector WHERE company_key = ?", (key,)
    ).rowcount
    conn.commit()
    log.info(
        "store: blocked %s (%s) — %d deals, %d sector rows removed",
        company or key,
        key,
        deals_removed,
        sector_removed,
    )
    return {
        "company_key": key,
        "company": company or key,
        "deals_removed": deals_removed,
        "sector_removed": sector_removed,
    }


def scanned_urls(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT url FROM scanned_posts").fetchall()
    return {r["url"] for r in rows if r["url"]}


def mark_scanned(
    conn: sqlite3.Connection,
    items: list[dict],
) -> int:
    """Record article URLs so future runs skip them. Returns rows upserted."""
    today = date.today().isoformat()
    n = 0
    for it in items:
        url = (it.get("url") or "").strip()
        if not url:
            continue
        key = it.get("company_key") or ""
        if not key and it.get("company"):
            key = company_key(it["company"])
        conn.execute(
            "INSERT INTO scanned_posts (url, company_key, published_at, scanned_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET "
            "company_key = COALESCE(NULLIF(excluded.company_key, ''), scanned_posts.company_key), "
            "published_at = COALESCE(NULLIF(excluded.published_at, ''), scanned_posts.published_at)",
            (url, key, (it.get("published_at") or "")[:10], today),
        )
        n += 1
    conn.commit()
    if n:
        log.info("store: marked %d urls as scanned", n)
    return n


def _hits(blob: str, keywords: list[str]) -> int:
    """Count word-boundary matches. Substring matching produced nonsense like
    'ny' firing inside 'Nyca Partners'."""
    n = 0
    for kw in keywords:
        if re.search(rf"\b{re.escape(kw.lower())}\b", blob):
            n += 1
    return n


def score_deal(deal: dict, filters: dict) -> tuple[int, int]:
    """Return (priority, score). Priority 1 means sector AND location match."""
    # Investors are deliberately excluded: VC firm names are full of city and
    # sector words and would flag every round they touch.
    blob = " ".join(
        str(deal.get(f, "") or "") for f in ("company", "description", "location")
    ).lower()

    sector_hits = _hits(blob, filters.get("priority_sectors", []))
    loc_hits = _hits(blob, filters.get("priority_locations", []))
    penalty = _hits(blob, filters.get("deprioritize", []))

    score = sector_hits * 2 + loc_hits * 3 - penalty * 6
    priority = 1 if (sector_hits and loc_hits and not penalty) else 0
    return priority, score


def _vague_stage(stage: str) -> bool:
    s = (stage or "").strip().lower()
    return s in {"", "undisclosed", "new", "funding", "investment", "n/a", "na"}


def _stage_rank(stage: str) -> int:
    """Higher = more specific label to prefer when collapsing duplicates."""
    if _vague_stage(stage):
        return 0
    return 1 + len((stage or "").strip())


def _merge_fills(existing: sqlite3.Row, deal: dict) -> dict:
    fills = {}
    if existing["amount_usd"] is None and deal.get("amount_usd") is not None:
        fills["amount_usd"] = deal["amount_usd"]
        fills["amount_raw"] = deal.get("amount_raw", "")
    if not existing["investors"] and deal.get("investors"):
        fills["investors"] = deal["investors"]
    if not existing["location"] and deal.get("location"):
        fills["location"] = deal["location"]
    # Prefer a concrete stage over Undisclosed / empty.
    if _vague_stage(existing["stage"]) and not _vague_stage(deal.get("stage", "")):
        fills["stage"] = deal.get("stage", "")
    return fills


def collapse_duplicate_deals(conn: sqlite3.Connection) -> int:
    """Remove duplicate deal rows (same URL, or same company in a close window
    with a vague stage). Keeps the more specific stage. Returns rows deleted.
    """
    deleted = 0
    # 1) Exact same article URL
    urls = conn.execute(
        "SELECT url FROM deals WHERE url IS NOT NULL AND url != '' "
        "GROUP BY url HAVING COUNT(*) > 1"
    ).fetchall()
    for row in urls:
        twins = conn.execute(
            "SELECT id, stage FROM deals WHERE url = ? ORDER BY id",
            (row["url"],),
        ).fetchall()
        keep = max(twins, key=lambda r: (_stage_rank(r["stage"]), -r["id"]))
        for t in twins:
            if t["id"] != keep["id"]:
                conn.execute("DELETE FROM deals WHERE id = ?", (t["id"],))
                deleted += 1

    # 2) Same company_key + near dates, one stage vague
    keys = conn.execute(
        "SELECT company_key FROM deals GROUP BY company_key HAVING COUNT(*) > 1"
    ).fetchall()
    for row in keys:
        twins = conn.execute(
            "SELECT id, stage, published_at FROM deals WHERE company_key = ? ORDER BY id",
            (row["company_key"],),
        ).fetchall()
        drop: set[int] = set()
        for i, a in enumerate(twins):
            if a["id"] in drop:
                continue
            for b in twins[i + 1 :]:
                if b["id"] in drop:
                    continue
                try:
                    da = datetime.fromisoformat(a["published_at"][:10]).date()
                    db = datetime.fromisoformat(b["published_at"][:10]).date()
                except (TypeError, ValueError):
                    continue
                if abs((da - db).days) > 21:
                    continue
                if _vague_stage(a["stage"]) or _vague_stage(b["stage"]):
                    loser = a if _stage_rank(a["stage"]) < _stage_rank(b["stage"]) else b
                    drop.add(loser["id"])
        for did in drop:
            conn.execute("DELETE FROM deals WHERE id = ?", (did,))
            deleted += 1

    if deleted:
        conn.commit()
        log.info("store: collapsed %d duplicate deal rows", deleted)
    return deleted


def upsert(conn: sqlite3.Connection, deals: list[dict], filters: dict,
           dedupe_days: int) -> tuple[int, int]:
    """Insert new deals, skip duplicates. Returns (inserted, skipped)."""
    inserted = skipped = 0
    today = date.today().isoformat()
    blocked = blocked_keys(conn)

    for deal in deals:
        key = company_key(deal["company"])
        if key in blocked:
            skipped += 1
            continue
        pub = (deal.get("published_at") or today)[:10]
        stage = deal.get("stage", "") or ""
        url = (deal.get("url") or "").strip()

        try:
            pub_date = datetime.fromisoformat(pub).date()
        except ValueError:
            pub_date = date.today()
        lo = (pub_date - timedelta(days=dedupe_days)).isoformat()
        hi = (pub_date + timedelta(days=dedupe_days)).isoformat()

        existing = None
        # Same article URL always means the same round.
        if url:
            existing = conn.execute(
                "SELECT id, amount_usd, investors, location, stage FROM deals WHERE url = ?",
                (url,),
            ).fetchone()
        # Same company + stage in the dedupe window.
        if not existing:
            existing = conn.execute(
                "SELECT id, amount_usd, investors, location, stage FROM deals "
                "WHERE company_key = ? AND stage = ? AND published_at BETWEEN ? AND ?",
                (key, stage, lo, hi),
            ).fetchone()
        # Same company in-window when either side has a vague stage (Undisclosed).
        if not existing and _vague_stage(stage):
            existing = conn.execute(
                "SELECT id, amount_usd, investors, location, stage FROM deals "
                "WHERE company_key = ? AND published_at BETWEEN ? AND ? "
                "ORDER BY id LIMIT 1",
                (key, lo, hi),
            ).fetchone()
        elif not existing:
            existing = conn.execute(
                "SELECT id, amount_usd, investors, location, stage FROM deals "
                "WHERE company_key = ? AND published_at BETWEEN ? AND ? "
                "ORDER BY id LIMIT 1",
                (key, lo, hi),
            ).fetchone()
            if existing and not _vague_stage(existing["stage"]):
                # Concrete stage already stored and incoming is also concrete but
                # different — treat as a distinct round (e.g. Seed then Series A).
                if (existing["stage"] or "").lower() != stage.lower():
                    existing = None

        if existing:
            fills = _merge_fills(existing, deal)
            if fills:
                sets = ", ".join(f"{k} = ?" for k in fills)
                conn.execute(
                    f"UPDATE deals SET {sets} WHERE id = ?",
                    (*fills.values(), existing["id"]),
                )
            skipped += 1
            continue

        priority, score = score_deal(deal, filters)
        conn.execute(
            "INSERT INTO deals (company_key, company, amount_usd, amount_raw, "
            "stage, location, description, investors, source, url, published_at, "
            "first_seen, priority, score, extracted_by, sector_label, sector_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key, deal["company"], deal.get("amount_usd"), deal.get("amount_raw", ""),
                stage, deal.get("location", ""),
                deal.get("description", ""), deal.get("investors", ""),
                deal.get("source", ""), url, pub, today,
                priority, score, deal.get("extracted_by", ""),
                deal.get("sector_label", ""), deal.get("sector_reason", ""),
            ),
        )
        inserted += 1

    conn.commit()
    log.info("store: %d inserted, %d duplicates merged", inserted, skipped)
    return inserted, skipped


def recent(conn: sqlite3.Connection, window_days: int) -> list[dict]:
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        "SELECT d.* FROM deals d "
        "LEFT JOIN blocked_companies b ON b.company_key = d.company_key "
        "WHERE d.published_at >= ? AND b.company_key IS NULL "
        "ORDER BY d.published_at DESC, d.priority DESC, d.score DESC, "
        "COALESCE(d.amount_usd, 0) DESC",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]
