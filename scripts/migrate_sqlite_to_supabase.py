#!/usr/bin/env python3
"""One-time migration: SQLite data/deals.db → Supabase Postgres.

Usage:
  uv run python scripts/migrate_sqlite_to_supabase.py
  uv run python scripts/migrate_sqlite_to_supabase.py --db data/deals.db --dry-run

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env or environment.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

BATCH = 200


def _client():
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        print("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env", file=sys.stderr)
        sys.exit(1)
    return create_client(url, key)


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if table == "deals" and "id" in d:
            d.pop("id", None)
        out.append({k: v for k, v in d.items() if k in cols or table != "deals" or k != "id"})
    return out


def _upsert_batches(client, table: str, rows: list[dict], on_conflict: str) -> int:
    n = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        client.table(table).upsert(chunk, on_conflict=on_conflict).execute()
        n += len(chunk)
        print(f"  {table}: {n}/{len(rows)}")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate SQLite blotter DB to Supabase")
    ap.add_argument("--db", default=str(ROOT / "data" / "deals.db"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"SQLite DB not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    tables = {
        "deals": ("id", None),  # omit id on insert
        "company_sector": ("company_key", "company_key"),
        "scanned_posts": ("url", "url"),
        "blocked_companies": ("company_key", "company_key"),
    }

    payload = {}
    for table, (_, conflict) in tables.items():
        rows = _rows(conn, table)
        if table == "deals":
            for r in rows:
                r.pop("id", None)
        payload[table] = (rows, conflict)
        print(f"{table}: {len(rows)} rows")

    if args.dry_run:
        print("Dry run — nothing written.")
        return 0

    client = _client()
    for table, (rows, on_conflict) in payload.items():
        if not rows:
            continue
        if on_conflict:
            _upsert_batches(client, table, rows, on_conflict)
        else:
            for i in range(0, len(rows), BATCH):
                client.table(table).insert(rows[i : i + BATCH]).execute()

    print("Migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
