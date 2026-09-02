"""Unified database backend: SQLite (local) or Supabase (production)."""

from __future__ import annotations

import sqlite3
from typing import Protocol, runtime_checkable

from . import store
from .supabase_store import SupabaseStore


@runtime_checkable
class Backend(Protocol):
    def blocked_keys(self) -> set[str]: ...
    def block_company(self, key: str, company: str = "", reason: str = "") -> dict: ...
    def scanned_urls(self) -> set[str]: ...
    def mark_scanned(self, items: list[dict]) -> int: ...
    def upsert(self, deals: list[dict], filters: dict, dedupe_days: int) -> tuple[int, int]: ...
    def collapse_duplicate_deals(self) -> int: ...
    def recent(self, window_days: int) -> list[dict]: ...
    def cache_get(self, key: str) -> tuple[str, str] | None: ...
    def cache_put(self, key: str, label: str, reason: str) -> None: ...
    def companies_for_purge(self) -> list[dict]: ...
    def sync_deal_sectors_from_cache(self) -> None: ...
    def delete_unrelated_deals(self, keep_labels: set[str]) -> int: ...


class SqliteBackend:
    """Wraps sqlite3 connection + store module functions."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def blocked_keys(self) -> set[str]:
        return store.blocked_keys(self.conn)

    def block_company(self, key: str, company: str = "", reason: str = "") -> dict:
        return store.block_company(self.conn, key, company=company, reason=reason)

    def scanned_urls(self) -> set[str]:
        return store.scanned_urls(self.conn)

    def mark_scanned(self, items: list[dict]) -> int:
        return store.mark_scanned(self.conn, items)

    def upsert(self, deals: list[dict], filters: dict, dedupe_days: int) -> tuple[int, int]:
        return store.upsert(self.conn, deals, filters, dedupe_days)

    def collapse_duplicate_deals(self) -> int:
        return store.collapse_duplicate_deals(self.conn)

    def recent(self, window_days: int) -> list[dict]:
        return store.recent(self.conn, window_days)

    def cache_get(self, key: str) -> tuple[str, str] | None:
        row = self.conn.execute(
            "SELECT label, reason FROM company_sector WHERE company_key = ?", (key,)
        ).fetchone()
        return (row["label"], row["reason"]) if row else None

    def cache_put(self, key: str, label: str, reason: str) -> None:
        from datetime import date

        self.conn.execute(
            "INSERT INTO company_sector (company_key, label, reason, enriched_at) "
            "VALUES (?,?,?,?) ON CONFLICT(company_key) DO UPDATE SET "
            "label=excluded.label, reason=excluded.reason, enriched_at=excluded.enriched_at",
            (key, label, reason, date.today().isoformat()),
        )
        self.conn.commit()

    def companies_for_purge(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT DISTINCT company_key, company, location, description, "
            "COALESCE(sector_label, '') AS sector_label FROM deals"
        ).fetchall()
        return [dict(r) for r in rows]

    def sync_deal_sectors_from_cache(self) -> None:
        cache_rows = self.conn.execute(
            "SELECT company_key, label, reason FROM company_sector"
        ).fetchall()
        for r in cache_rows:
            self.conn.execute(
                "UPDATE deals SET sector_label = ?, sector_reason = ? WHERE company_key = ?",
                (r["label"], r["reason"], r["company_key"]),
            )
        self.conn.commit()

    def delete_unrelated_deals(self, keep_labels: set[str]) -> int:
        self.sync_deal_sectors_from_cache()
        placeholders = ",".join("?" for _ in keep_labels)
        cur = self.conn.execute(
            f"DELETE FROM deals WHERE sector_label = 'unrelated' "
            f"OR (sector_label != '' AND sector_label NOT IN ({placeholders}) "
            f"AND sector_label != 'unknown')",
            tuple(keep_labels),
        )
        deleted = cur.rowcount
        self.conn.commit()
        if deleted:
            import logging
            logging.getLogger(__name__).info("store: purged %d unrelated deals", deleted)
        return deleted


def open_backend(kind: str, sqlite_path: str | None = None) -> Backend:
    if kind == "supabase":
        return SupabaseStore.from_env()
    if not sqlite_path:
        raise ValueError("sqlite_path required for sqlite backend")
    return SqliteBackend(store.connect(sqlite_path))
