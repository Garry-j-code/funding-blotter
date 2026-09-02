"""Supabase Postgres persistence — mirrors src/store.py operations."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any

from supabase import Client, create_client

from .store import (
    _merge_fills,
    _stage_rank,
    _vague_stage,
    company_key,
    score_deal,
)

log = logging.getLogger(__name__)

DEAL_COLS = (
    "company_key", "company", "amount_usd", "amount_raw", "stage", "location",
    "description", "investors", "source", "url", "published_at", "first_seen",
    "priority", "score", "extracted_by", "sector_label", "sector_reason",
)


class SupabaseStore:
    def __init__(self, client: Client) -> None:
        self.client = client

    @classmethod
    def from_env(cls) -> SupabaseStore:
        url = os.environ.get("SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
        return cls(create_client(url, key))

    def _deals(self) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        page = 1000
        while True:
            resp = (
                self.client.table("deals")
                .select("*")
                .order("id")
                .range(offset, offset + page - 1)
                .execute()
            )
            chunk = resp.data or []
            rows.extend(chunk)
            if len(chunk) < page:
                break
            offset += page
        return rows

    def blocked_keys(self) -> set[str]:
        resp = self.client.table("blocked_companies").select("company_key").execute()
        return {r["company_key"] for r in (resp.data or []) if r.get("company_key")}

    def block_company(self, key: str, company: str = "", reason: str = "") -> dict:
        key = (key or "").strip()
        if not key:
            raise ValueError("company_key is required")
        today = date.today().isoformat()
        self.client.table("blocked_companies").upsert(
            {
                "company_key": key,
                "company": company or key,
                "blocked_at": today,
                "reason": reason,
            },
            on_conflict="company_key",
        ).execute()
        deals_resp = (
            self.client.table("deals").delete().eq("company_key", key).execute()
        )
        sector_resp = (
            self.client.table("company_sector").delete().eq("company_key", key).execute()
        )
        deals_removed = len(deals_resp.data or [])
        sector_removed = len(sector_resp.data or [])
        log.info(
            "supabase: blocked %s (%s) — %d deals, %d sector rows removed",
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

    def scanned_urls(self) -> set[str]:
        resp = self.client.table("scanned_posts").select("url").execute()
        return {r["url"] for r in (resp.data or []) if r.get("url")}

    def mark_scanned(self, items: list[dict]) -> int:
        today = date.today().isoformat()
        rows = []
        for it in items:
            url = (it.get("url") or "").strip()
            if not url:
                continue
            key = it.get("company_key") or ""
            if not key and it.get("company"):
                key = company_key(it["company"])
            rows.append({
                "url": url,
                "company_key": key,
                "published_at": (it.get("published_at") or "")[:10],
                "scanned_at": today,
            })
        if rows:
            self.client.table("scanned_posts").upsert(rows, on_conflict="url").execute()
            log.info("supabase: marked %d urls as scanned", len(rows))
        return len(rows)

    def collapse_duplicate_deals(self) -> int:
        deleted = 0
        all_deals = self._deals()
        by_url: dict[str, list[dict]] = {}
        for d in all_deals:
            url = (d.get("url") or "").strip()
            if url:
                by_url.setdefault(url, []).append(d)

        drop_ids: set[int] = set()
        for twins in by_url.values():
            if len(twins) < 2:
                continue
            keep = max(twins, key=lambda r: (_stage_rank(r.get("stage", "")), -(r.get("id") or 0)))
            for t in twins:
                if t["id"] != keep["id"]:
                    drop_ids.add(t["id"])

        by_key: dict[str, list[dict]] = {}
        for d in all_deals:
            if d["id"] in drop_ids:
                continue
            by_key.setdefault(d["company_key"], []).append(d)

        for twins in by_key.values():
            if len(twins) < 2:
                continue
            twins = sorted(twins, key=lambda r: r.get("id") or 0)
            for i, a in enumerate(twins):
                if a["id"] in drop_ids:
                    continue
                for b in twins[i + 1 :]:
                    if b["id"] in drop_ids:
                        continue
                    try:
                        da = datetime.fromisoformat((a.get("published_at") or "")[:10]).date()
                        db = datetime.fromisoformat((b.get("published_at") or "")[:10]).date()
                    except (TypeError, ValueError):
                        continue
                    if abs((da - db).days) > 21:
                        continue
                    if _vague_stage(a.get("stage", "")) or _vague_stage(b.get("stage", "")):
                        loser = (
                            a
                            if _stage_rank(a.get("stage", "")) < _stage_rank(b.get("stage", ""))
                            else b
                        )
                        drop_ids.add(loser["id"])

        for did in drop_ids:
            self.client.table("deals").delete().eq("id", did).execute()
            deleted += 1

        if deleted:
            log.info("supabase: collapsed %d duplicate deal rows", deleted)
        return deleted

    def upsert(self, deals: list[dict], filters: dict, dedupe_days: int) -> tuple[int, int]:
        inserted = skipped = 0
        today = date.today().isoformat()
        blocked = self.blocked_keys()
        existing_deals = self._deals()

        def find_existing(key: str, stage: str, url: str, lo: str, hi: str) -> dict | None:
            if url:
                for d in existing_deals:
                    if (d.get("url") or "").strip() == url:
                        return d
            for d in existing_deals:
                if d["company_key"] != key:
                    continue
                pub = (d.get("published_at") or "")[:10]
                if not (lo <= pub <= hi):
                    continue
                if d.get("stage") == stage:
                    return d
            if _vague_stage(stage):
                for d in existing_deals:
                    if d["company_key"] == key and lo <= (d.get("published_at") or "")[:10] <= hi:
                        return d
            else:
                for d in existing_deals:
                    if d["company_key"] != key:
                        continue
                    pub = (d.get("published_at") or "")[:10]
                    if not (lo <= pub <= hi):
                        continue
                    if not _vague_stage(d.get("stage", "")):
                        if (d.get("stage") or "").lower() == stage.lower():
                            return d
                        continue
                    return d
            return None

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

            existing = find_existing(key, stage, url, lo, hi)
            if existing:
                fills = _merge_fills_row(existing, deal)
                if fills:
                    self.client.table("deals").update(fills).eq("id", existing["id"]).execute()
                    existing.update(fills)
                skipped += 1
                continue

            priority, score = score_deal(deal, filters)
            row = {
                "company_key": key,
                "company": deal["company"],
                "amount_usd": deal.get("amount_usd"),
                "amount_raw": deal.get("amount_raw", ""),
                "stage": stage,
                "location": deal.get("location", ""),
                "description": deal.get("description", ""),
                "investors": deal.get("investors", ""),
                "source": deal.get("source", ""),
                "url": url,
                "published_at": pub,
                "first_seen": today,
                "priority": priority,
                "score": score,
                "extracted_by": deal.get("extracted_by", ""),
                "sector_label": deal.get("sector_label", ""),
                "sector_reason": deal.get("sector_reason", ""),
            }
            resp = self.client.table("deals").insert(row).execute()
            if resp.data:
                existing_deals.append(resp.data[0])
            inserted += 1

        log.info("supabase: %d inserted, %d duplicates merged", inserted, skipped)
        return inserted, skipped

    def recent(self, window_days: int) -> list[dict]:
        cutoff = (date.today() - timedelta(days=window_days)).isoformat()
        blocked = self.blocked_keys()
        resp = (
            self.client.table("deals")
            .select("*")
            .gte("published_at", cutoff)
            .order("published_at", desc=True)
            .order("priority", desc=True)
            .order("score", desc=True)
            .execute()
        )
        rows = [r for r in (resp.data or []) if r.get("company_key") not in blocked]
        rows.sort(
            key=lambda d: (
                d.get("published_at") or "",
                d.get("priority") or 0,
                d.get("score") or 0,
                d.get("amount_usd") or 0,
            ),
            reverse=True,
        )
        return rows

    def cache_get(self, key: str) -> tuple[str, str] | None:
        resp = (
            self.client.table("company_sector")
            .select("label, reason")
            .eq("company_key", key)
            .limit(1)
            .execute()
        )
        if resp.data:
            r = resp.data[0]
            return r.get("label", ""), r.get("reason", "")
        return None

    def cache_put(self, key: str, label: str, reason: str) -> None:
        self.client.table("company_sector").upsert(
            {
                "company_key": key,
                "label": label,
                "reason": reason,
                "enriched_at": date.today().isoformat(),
            },
            on_conflict="company_key",
        ).execute()

    def companies_for_purge(self) -> list[dict]:
        resp = self.client.table("deals").select(
            "company_key, company, location, description, sector_label"
        ).execute()
        seen: set[str] = set()
        out: list[dict] = []
        for r in resp.data or []:
            k = r["company_key"]
            if k in seen:
                continue
            seen.add(k)
            out.append({
                "company_key": k,
                "company": r.get("company", ""),
                "location": r.get("location") or "",
                "description": r.get("description") or "",
                "sector_label": r.get("sector_label") or "",
            })
        return out

    def sync_deal_sectors_from_cache(self) -> None:
        cache = self.client.table("company_sector").select("company_key, label, reason").execute()
        for r in cache.data or []:
            self.client.table("deals").update({
                "sector_label": r["label"],
                "sector_reason": r["reason"],
            }).eq("company_key", r["company_key"]).execute()

    def delete_unrelated_deals(self, keep_labels: set[str]) -> int:
        self.sync_deal_sectors_from_cache()
        all_deals = self._deals()
        deleted = 0
        for d in all_deals:
            label = d.get("sector_label") or ""
            if label == "unrelated":
                self.client.table("deals").delete().eq("id", d["id"]).execute()
                deleted += 1
            elif label and label != "unknown" and label not in keep_labels:
                self.client.table("deals").delete().eq("id", d["id"]).execute()
                deleted += 1
        if deleted:
            log.info("supabase: purged %d unrelated deals", deleted)
        return deleted


def _merge_fills_row(existing: dict, deal: dict) -> dict:
    """Dict version of store._merge_fills for Supabase rows."""
    class Row:
        def __init__(self, d: dict) -> None:
            self._d = d

        def __getitem__(self, k: str) -> Any:
            return self._d.get(k)

    return _merge_fills(Row(existing), deal)
