"""Fast backfill of FinSMEs deals for a date range.

Walks the same `/category/usa` listing as the daily scraper (pages embed
`<time datetime>`), downloads only articles inside the requested window, then
runs extract → enrich → store → render.

  uv run python -m src.backfill --from 2026-07-31 --to 2026-08-03
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from . import enrich, extract, render, sources, store
from .main import ROOT, _github_cfg, setup_logging

log = logging.getLogger("backfill")


def _listing_url(page: int) -> str:
    if page <= 1:
        return "https://www.finsmes.com/category/usa/"
    return f"https://www.finsmes.com/category/usa/page/{page}/"


def collect_dated_links(
    pages: int,
    start: date,
    end: date,
    delay: float = 0.35,
    skip_urls: set[str] | None = None,
) -> list[tuple[str, str]]:
    """Return [(url, YYYY-MM-DD), ...] for posts whose listing date is in range.

    Category pages are newest-first; we still require a few consecutive
    fully-too-old pages before stopping in case a page is thin or noisy.
    URLs in skip_urls (already in scanned_posts / deals) are not returned.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    skip_urls = skip_urls or set()
    skipped = 0
    # How many back-to-back pages with newest < start before we stop.
    stale_pages_needed = 2
    stale_streak = 0

    for page in range(1, pages + 1):
        url = _listing_url(page)
        try:
            resp = requests.get(url, headers=sources.BROWSER_HEADERS, timeout=sources.TIMEOUT)
            if resp.status_code == 429:
                log.warning("rate limited on page %d; sleep 6s", page)
                time.sleep(6)
                resp = requests.get(url, headers=sources.BROWSER_HEADERS, timeout=sources.TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("listing page %d failed: %s", page, exc)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        page_hits = 0
        page_dates: list[date] = []

        for time_el in soup.select("time[datetime]"):
            raw_dt = (time_el.get("datetime") or "")[:10]
            try:
                pub = date.fromisoformat(raw_dt)
            except ValueError:
                continue

            node = time_el
            href = None
            for _ in range(8):
                node = node.parent
                if not node:
                    break
                a = node.find("a", href=sources.FINSMES_POST_RE)
                if a and a.get("href"):
                    href = a["href"].strip()
                    break
            if not href or href in seen:
                continue
            seen.add(href)
            page_dates.append(pub)

            if not (start <= pub <= end):
                continue
            if href in skip_urls:
                skipped += 1
                continue
            out.append((href, raw_dt))
            page_hits += 1

        newest = max(page_dates).isoformat() if page_dates else "?"
        oldest = min(page_dates).isoformat() if page_dates else "?"
        log.info(
            "page %d: %d new in-range (total %d, skipped %d known) newest=%s oldest=%s",
            page, page_hits, len(out), skipped, newest, oldest,
        )

        if page_dates and max(page_dates) < start:
            stale_streak += 1
            if page > 1 and stale_streak >= stale_pages_needed:
                log.info(
                    "reached %d consecutive pages older than %s; stopping scan",
                    stale_streak,
                    start,
                )
                break
        else:
            stale_streak = 0

        if delay:
            time.sleep(delay)

    if skipped:
        log.info("skipped %d already-scanned urls in date range", skipped)
    return out


def fetch_articles(pairs: list[tuple[str, str]], workers: int = 4) -> list[dict]:
    """Fetch full article bodies concurrently."""
    items: list[dict] = []

    def one(url: str, listed_date: str) -> dict | None:
        art = sources._finsmes_article("finsmes", url, "finsmes")
        if not art:
            return None
        # Prefer article meta date; fall back to listing date.
        if not art.get("published_at"):
            art["published_at"] = listed_date
        return art

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(one, u, d): u for u, d in pairs}
        for fut in as_completed(futs):
            art = fut.result()
            if art:
                items.append(art)
                log.info("fetched %s %s", art["published_at"], art.get("title", "")[:55])
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description="Fast FinSMEs date-range backfill")
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--pages", type=int, default=10, help="max category pages to scan")
    ap.add_argument("--workers", type=int, default=4, help="parallel article fetches")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args()

    setup_logging(args.v)
    load_dotenv(ROOT / ".env")
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    start = date.fromisoformat(args.date_from)
    end = date.fromisoformat(args.date_to)
    if end < start:
        log.error("--to must be on or after --from")
        return 1

    db_path = str(ROOT / cfg["store"]["db_path"])
    html_path = str(ROOT / cfg["output"]["html_path"])
    csv_path = str(ROOT / cfg["output"]["csv_path"])
    conn = store.connect(db_path)
    skip_urls = store.scanned_urls(conn)

    log.info(
        "scanning listings for %s .. %s (%d urls already scanned)",
        start,
        end,
        len(skip_urls),
    )
    pairs = collect_dated_links(args.pages, start, end, skip_urls=skip_urls)
    if not pairs:
        log.info("no new in-range posts (all known or none listed); refreshing page")
        recent = store.recent(conn, cfg["output"]["window_days"])
        render.write_html(recent, html_path, _github_cfg(cfg))
        render.write_csv(recent, csv_path)
        return 0
    log.info("%d new posts in range — fetching article bodies (%d workers)", len(pairs), args.workers)

    raw = fetch_articles(pairs, workers=args.workers)
    # Strict filter on article meta date too.
    raw = [
        r for r in raw
        if start.isoformat() <= (r.get("published_at") or "")[:10] <= end.isoformat()
    ]
    if not raw:
        log.error("no articles survived date filter after fetch")
        return 1
    log.info("backfill: %d articles to extract", len(raw))

    deals = extract.extract_all(raw, cfg["extraction"])
    scanned_batch = [dict(d) for d in deals]

    if not args.no_enrich:
        deals = enrich.enrich_all(deals, cfg, conn)
        enrich.purge_unrelated(conn, cfg)

    store.upsert(conn, deals, cfg["filters"], cfg["store"]["dedupe_window_days"])
    store.collapse_duplicate_deals(conn)
    by_url = {d.get("url"): d for d in scanned_batch if d.get("url")}
    store.mark_scanned(conn, [by_url.get(r.get("url"), r) for r in raw])
    recent = store.recent(conn, cfg["output"]["window_days"])
    render.write_html(recent, html_path, _github_cfg(cfg))
    render.write_csv(recent, csv_path)

    by_day: dict[str, list[str]] = {}
    for d in recent:
        pub = d["published_at"]
        if start.isoformat() <= pub <= end.isoformat():
            by_day.setdefault(pub, []).append(d["company"])
    for day in sorted(by_day):
        log.info("%s (%d): %s", day, len(by_day[day]), ", ".join(by_day[day]))
    log.info("done: %d deals on page total", len(recent))
    return 0


if __name__ == "__main__":
    sys.exit(main())