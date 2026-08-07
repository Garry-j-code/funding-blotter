"""Entrypoint.

  python -m src.main                    # full daily run
  python -m src.main --dry-run          # fetch + extract, don't write anything
  python -m src.main --probe aifunding_me   # dump what a source returns
  python -m src.main --no-llm           # regex only, zero API calls
  python -m src.main --render-only      # rebuild the page from the existing DB
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from . import enrich, extract, render, sources, store

ROOT = Path(__file__).resolve().parent.parent

# Load secrets (e.g. GROQ_API_KEY) from a .env at the repo root. An already-set
# environment variable always wins, so CI secrets are never clobbered.
load_dotenv(ROOT / ".env")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _github_cfg(cfg: dict) -> dict:
    out = cfg.get("output") or {}
    return {
        "owner": out.get("github_owner", "Garry-j-code"),
        "repo": out.get("github_repo", "funding-blotter"),
        "workflow": out.get("workflow_file", "daily.yml"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily funding-round blotter")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", metavar="SOURCE",
                    help="fetch one source and print raw items, then exit")
    ap.add_argument("--no-llm", action="store_true", help="regex only")
    ap.add_argument("--no-enrich", action="store_true",
                    help="skip the web-search sector filter, keep all companies")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    log = logging.getLogger("main")
    cfg = load_config(args.config)

    db_path = str(ROOT / cfg["store"]["db_path"])
    html_path = str(ROOT / cfg["output"]["html_path"])
    csv_path = str(ROOT / cfg["output"]["csv_path"])

    # --probe: see exactly what a source hands back. Use this on aifunding.me
    # before trusting its selectors.
    if args.probe:
        items = sources.fetch_all(cfg["sources"], only=args.probe)
        print(f"\n{len(items)} raw items from {args.probe}\n" + "=" * 70)
        for item in items[:12]:
            print(json.dumps(item, indent=2, ensure_ascii=False)[:900])
            print("-" * 70)
        return 0 if items else 1

    if args.render_only:
        conn = store.connect(db_path)
        deals = store.recent(conn, cfg["output"]["window_days"])
        render.write_html(deals, html_path, _github_cfg(cfg))
        render.write_csv(deals, csv_path)
        return 0

    conn = store.connect(db_path)
    skip_urls = store.scanned_urls(conn)

    # 1. Fetch (skip article URLs already in scanned_posts)
    raw = sources.fetch_all(cfg["sources"], skip_urls=skip_urls)
    if not raw:
        # Lookback fully covered / nothing new — still refresh the page from DB.
        recent = store.recent(conn, cfg["output"]["window_days"])
        if recent:
            log.info("no new articles; re-rendering %d stored deals", len(recent))
            render.write_html(recent, html_path, _github_cfg(cfg))
            render.write_csv(recent, csv_path)
            return 0
        log.error("no items fetched from any source; leaving previous page intact")
        return 1
    log.info("fetched %d new raw items (%d urls already scanned)", len(raw), len(skip_urls))

    # 2. Extract
    ex_cfg = dict(cfg["extraction"])
    if args.no_llm:
        ex_cfg["regex_fastpath"] = True
        import os
        os.environ.pop("GROQ_API_KEY", None)
    deals = extract.extract_all(raw, ex_cfg)
    # Remember every extracted row (including later-dropped unrelated) so we
    # can mark their URLs scanned and never re-fetch them.
    scanned_batch = [dict(d) for d in deals]

    # 3. Enrich: LLM calls web_search tool, then keeps only fintech / FS / enablers.
    if not args.no_enrich:
        deals = enrich.enrich_all(deals, cfg, conn)
        enrich.purge_unrelated(conn, cfg)

    if args.dry_run:
        for d in deals[:25]:
            amt = d.get("amount_raw") or "n/d"
            sector = d.get("sector_label") or "-"
            print(f"  {d['company'][:30]:<30} {amt:>10}  {d.get('stage',''):<10} "
                  f"{sector:<18} {d.get('location','')[:20]}")
        log.info("dry run: %d deals kept, nothing written", len(deals))
        return 0

    # 4. Store with dedupe; record all fetched article URLs as scanned.
    store.upsert(conn, deals, cfg["filters"], cfg["store"]["dedupe_window_days"])
    store.collapse_duplicate_deals(conn)
    # Prefer extracted rows (have company); fall back to raw urls alone.
    by_url = {d.get("url"): d for d in scanned_batch if d.get("url")}
    to_mark = []
    for item in raw:
        url = item.get("url") or ""
        row = by_url.get(url, item)
        to_mark.append(row)
    store.mark_scanned(conn, to_mark)

    # 5. Render
    recent = store.recent(conn, cfg["output"]["window_days"])
    render.write_html(recent, html_path, _github_cfg(cfg))
    render.write_csv(recent, csv_path)

    flagged = sum(1 for d in recent if d.get("priority"))
    log.info("done: %d rounds on the page, %d flagged", len(recent), flagged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
