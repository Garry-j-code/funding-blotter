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
from .backend import open_backend
from .main import ROOT, _github_cfg, setup_logging

log = logging.getLogger("backfill")


def _listing_url(page: int) -> str:
    if page <= 1:
        return "https://www.finsmes.com/category/usa/"
    return f"https://www.finsmes.com/category/usa/page/{page}/"


def _fetch_listing(page: int, attempts: int = 3) -> str | None:
    """GET one listing page. Retries 429 / 5xx / network errors with backoff.

    Returns the HTML, or None if every attempt failed.
    """
    url = _listing_url(page)
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, headers=sources.BROWSER_HEADERS, timeout=sources.TIMEOUT)
            if resp.status_code == 429 or resp.status_code >= 500:
                reason = f"HTTP {resp.status_code}"
            else:
                resp.raise_for_status()
                return resp.text
        except requests.RequestException as exc:
            reason = str(exc)[:80]
        if attempt < attempts:
            wait = 6 * attempt
            log.warning("page %d: %s; retry %d/%d in %ds", page, reason, attempt, attempts - 1, wait)
            time.sleep(wait)
        else:
            log.warning("page %d: %s; giving up after %d attempts", page, reason, attempts)
    return None


def _parse_listing(html: str) -> list[tuple[str, date, str]]:
    """[(post url, listing date, raw date string)] for each dated post on a page."""
    soup = BeautifulSoup(html, "html.parser")
    posts: list[tuple[str, date, str]] = []
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
        if href:
            posts.append((href, pub, raw_dt))
    return posts


def collect_dated_links(
    pages: int,
    start: date,
    end: date,
    delay: float = 0.35,
    skip_urls: set[str] | None = None,
    report: dict | None = None,
) -> list[tuple[str, str]]:
    """Return [(url, YYYY-MM-DD), ...] for posts whose listing date is in range.

    Category pages are newest-first; we still require a few consecutive
    fully-too-old pages before stopping in case a page is thin or noisy.
    URLs in skip_urls (already in scanned_posts / deals) are not returned.

    If `report` is given it is filled with what could have been missed:
      pages_scanned, stopped_page, reached_start (False = hit the page cap
      before reaching `start`), failed_pages, empty_pages (still unresolved
      after a recheck), recovered_pages, days_without_posts (weekdays in range
      with no listed post at all).
    """
    report = report if report is not None else {}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    skip_urls = skip_urls or set()
    skipped = 0
    days_seen: set[date] = set()
    # How many back-to-back pages with newest < start before we stop.
    stale_pages_needed = 2
    stale_streak = 0
    failed: list[int] = []
    empty: list[int] = []
    recovered: list[int] = []
    reached_start = False
    last_page = 0

    def process(page: int, html: str) -> tuple[list[date], int]:
        """Collect in-range links from one page. Returns (all page dates, new hits)."""
        nonlocal skipped
        page_hits = 0
        page_dates: list[date] = []
        for href, pub, raw_dt in _parse_listing(html):
            if href in seen:
                continue
            seen.add(href)
            page_dates.append(pub)
            if not (start <= pub <= end):
                continue
            days_seen.add(pub)
            if href in skip_urls:
                skipped += 1
                continue
            out.append((href, raw_dt))
            page_hits += 1
        return page_dates, page_hits

    for page in range(1, pages + 1):
        last_page = page
        html = _fetch_listing(page)
        if html is None:
            failed.append(page)
            continue

        page_dates, page_hits = process(page, html)
        if not page_dates:
            empty.append(page)

        newest = max(page_dates).isoformat() if page_dates else "?"
        oldest = min(page_dates).isoformat() if page_dates else "?"
        log.info(
            "page %d: %d new in-range (total %d, skipped %d known) newest=%s oldest=%s",
            page, page_hits, len(out), skipped, newest, oldest,
        )

        # A page with no readable dates says nothing about how old we are, so it
        # neither extends nor resets the stale streak.
        if page_dates:
            if max(page_dates) < start:
                stale_streak += 1
                if page > 1 and stale_streak >= stale_pages_needed:
                    log.info(
                        "reached %d consecutive pages older than %s; stopping scan",
                        stale_streak,
                        start,
                    )
                    reached_start = True
                    break
            else:
                stale_streak = 0

        if delay:
            time.sleep(delay)

    # Second chance for pages that failed or came back empty (often transient
    # rate limiting). Cool down first.
    retry_pages = sorted(set(failed) | set(empty))
    if retry_pages:
        log.info("rechecking %d failed/empty listing pages after a cool-down", len(retry_pages))
        time.sleep(15)
        for page in retry_pages:
            html = _fetch_listing(page)
            if html is None:
                continue
            page_dates, page_hits = process(page, html)
            if page_dates:
                recovered.append(page)
                log.info("page %d recovered on recheck: %d new in-range", page, page_hits)
            time.sleep(max(delay, 1.0))
    still_failed = [p for p in failed if p not in recovered]
    still_empty = [p for p in empty if p not in recovered]

    today = datetime.now().date()
    gaps: list[str] = []
    d = start
    while d <= min(end, today):
        if d.weekday() < 5 and d not in days_seen:
            gaps.append(d.isoformat())
        d = date.fromordinal(d.toordinal() + 1)

    report.update(
        pages_scanned=last_page,
        stopped_page=last_page,
        reached_start=reached_start,
        failed_pages=still_failed,
        empty_pages=still_empty,
        recovered_pages=recovered,
        days_without_posts=gaps,
    )
    if skipped:
        log.info("skipped %d already-scanned urls in date range", skipped)
    return out


def scan_has_issues(report: dict) -> bool:
    return bool(
        report.get("failed_pages")
        or report.get("empty_pages")
        or not report.get("reached_start", True)
    )


def log_scan_report(report: dict, start: date, end: date) -> None:
    """Say plainly what the listing scan may have missed."""
    log.info("=" * 60)
    log.info("SCAN REPORT %s .. %s", start, end)
    log.info("  pages scanned: %s", report.get("pages_scanned"))
    if report.get("recovered_pages"):
        log.info("  recovered on recheck: %s", report["recovered_pages"])
    if not report.get("reached_start", True):
        log.warning(
            "  PAGE CAP REACHED at page %s before reaching %s. Nothing older was "
            "scanned. Re-run with a larger --pages.",
            report.get("stopped_page"),
            start,
        )
    if report.get("failed_pages"):
        log.warning("  FAILED pages (never loaded): %s", report["failed_pages"])
    if report.get("empty_pages"):
        log.warning("  EMPTY pages (loaded, no dated posts found): %s", report["empty_pages"])
    if report.get("days_without_posts"):
        log.info(
            "  weekdays in range with no listed posts (holidays are normal): %s",
            ", ".join(report["days_without_posts"]),
        )
    if scan_has_issues(report):
        log.warning(
            "  Some articles in this range may have been missed. Re-run the same "
            "command: already-scanned articles are skipped, so it only picks up the gaps."
        )
    else:
        log.info("  no failed or empty pages; scan looks complete")
    log.info("=" * 60)


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
    ap.add_argument("--backend", choices=("sqlite", "supabase"), default=None)
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
    backend_name = args.backend or cfg.get("store", {}).get("backend", "supabase")
    db = open_backend(backend_name, db_path if backend_name == "sqlite" else None)
    skip_urls = db.scanned_urls()

    log.info(
        "scanning listings for %s .. %s (%d urls already scanned)",
        start,
        end,
        len(skip_urls),
    )
    scan_report: dict = {}
    pairs = collect_dated_links(args.pages, start, end, skip_urls=skip_urls, report=scan_report)
    log_scan_report(scan_report, start, end)
    if not pairs:
        log.info("no new in-range posts (all known or none listed)")
        if backend_name == "sqlite":
            html_path = str(ROOT / cfg["output"]["html_path"])
            csv_path = str(ROOT / cfg["output"]["csv_path"])
            recent = db.recent(cfg["output"]["window_days"])
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

    blocked = db.blocked_keys()
    if blocked:
        before = len(deals)
        deals = [d for d in deals if store.company_key(d["company"]) not in blocked]
        dropped = before - len(deals)
        if dropped:
            log.info("skipped %d deals for blocked companies", dropped)

    if not args.no_enrich:
        deals = enrich.enrich_all(deals, cfg, db)
        enrich.purge_unrelated(db, cfg)

    db.upsert(deals, cfg["filters"], cfg["store"]["dedupe_window_days"])
    db.collapse_duplicate_deals()
    by_url = {d.get("url"): d for d in scanned_batch if d.get("url")}
    db.mark_scanned([by_url.get(r.get("url"), r) for r in raw])
    recent = db.recent(cfg["output"]["window_days"])
    if backend_name == "sqlite":
        html_path = str(ROOT / cfg["output"]["html_path"])
        csv_path = str(ROOT / cfg["output"]["csv_path"])
        render.write_html(recent, html_path, _github_cfg(cfg))
        render.write_csv(recent, csv_path)

    by_day: dict[str, list[str]] = {}
    for d in recent:
        pub = d["published_at"]
        if start.isoformat() <= pub <= end.isoformat():
            by_day.setdefault(pub, []).append(d["company"])
    for day in sorted(by_day):
        log.info("%s (%d): %s", day, len(by_day[day]), ", ".join(by_day[day]))
    if scan_has_issues(scan_report):
        log_scan_report(scan_report, start, end)  # repeat, so it is the last thing you read
    log.info("done: %d deals on page total", len(recent))
    return 0


if __name__ == "__main__":
    sys.exit(main())