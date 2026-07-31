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
    conn.commit()
    return conn


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


def upsert(conn: sqlite3.Connection, deals: list[dict], filters: dict,
           dedupe_days: int) -> tuple[int, int]:
    """Insert new deals, skip duplicates. Returns (inserted, skipped)."""
    inserted = skipped = 0
    today = date.today().isoformat()

    for deal in deals:
        key = company_key(deal["company"])
        pub = (deal.get("published_at") or today)[:10]

        try:
            pub_date = datetime.fromisoformat(pub).date()
        except ValueError:
            pub_date = date.today()
        lo = (pub_date - timedelta(days=dedupe_days)).isoformat()
        hi = (pub_date + timedelta(days=dedupe_days)).isoformat()

        existing = conn.execute(
            "SELECT id, amount_usd, investors, location FROM deals "
            "WHERE company_key = ? AND stage = ? AND published_at BETWEEN ? AND ?",
            (key, deal.get("stage", ""), lo, hi),
        ).fetchone()

        if existing:
            # A second source often has detail the first one lacked; fill gaps
            # rather than creating a duplicate row.
            fills = {}
            if existing["amount_usd"] is None and deal.get("amount_usd") is not None:
                fills["amount_usd"] = deal["amount_usd"]
                fills["amount_raw"] = deal.get("amount_raw", "")
            if not existing["investors"] and deal.get("investors"):
                fills["investors"] = deal["investors"]
            if not existing["location"] and deal.get("location"):
                fills["location"] = deal["location"]
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
                deal.get("stage", ""), deal.get("location", ""),
                deal.get("description", ""), deal.get("investors", ""),
                deal.get("source", ""), deal.get("url", ""), pub, today,
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
        "SELECT * FROM deals WHERE published_at >= ? "
        "ORDER BY published_at DESC, priority DESC, score DESC, "
        "COALESCE(amount_usd, 0) DESC",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]
