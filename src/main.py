"""Entrypoint.

  python -m src.main                    # full daily run (default: supabase backend)
  python -m src.main --backend sqlite   # local SQLite + HTML render
  python -m src.main --dry-run          # fetch + extract, don't write anything
  python -m src.main --probe aifunding_me   # dump what a source returns
  python -m src.main --no-llm           # regex only, zero API calls
  python -m src.main --render-only      # rebuild static page from SQLite only
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
from .backend import Backend, open_backend

ROOT = Path(__file__).resolve().parent.parent

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


def _render_outputs(db: Backend, cfg: dict, backend: str) -> None:
    """Write static HTML/CSV only for sqlite backend (legacy GitHub Pages path)."""
    if backend != "sqlite":
        return
    html_path = str(ROOT / cfg["output"]["html_path"])
    csv_path = str(ROOT / cfg["output"]["csv_path"])
    deals = db.recent(cfg["output"]["window_days"])
    render.write_html(deals, html_path, _github_cfg(cfg))
    render.write_csv(deals, csv_path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily funding-round blotter")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument(
        "--backend",
        choices=("sqlite", "supabase"),
        default=None,
        help="storage backend (default from config.yaml store.backend)",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", metavar="SOURCE",
                    help="fetch one source and print raw items, then exit")
    ap.add_argument("--no-llm", action="store_true", help="regex only")
    ap.add_argument("--no-enrich", action="store_true",
                    help="skip the web-search sector filter, keep all companies")
    ap.add_argument("--render-only", action="store_true",
                    help="rebuild static HTML from SQLite only")
    ap.add_argument("--remove-company-key", metavar="KEY",
                    help="block a company and remove it from the DB")
    ap.add_argument("--remove-company-name", default="",
                    help="display name for logs when using --remove-company-key")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    log = logging.getLogger("main")
    cfg = load_config(args.config)

    backend_name = args.backend or cfg.get("store", {}).get("backend", "supabase")
    db_path = str(ROOT / cfg["store"]["db_path"])

    if args.probe:
        items = sources.fetch_all(cfg["sources"], only=args.probe)
        print(f"\n{len(items)} raw items from {args.probe}\n" + "=" * 70)
        for item in items[:12]:
            print(json.dumps(item, indent=2, ensure_ascii=False)[:900])
            print("-" * 70)
        return 0 if items else 1

    if args.render_only:
        if backend_name != "sqlite":
            log.error("--render-only requires --backend sqlite")
            return 1
        db = open_backend("sqlite", db_path)
        _render_outputs(db, cfg, "sqlite")
        return 0

    db: Backend = open_backend(backend_name, db_path if backend_name == "sqlite" else None)

    if args.remove_company_key:
        info = db.block_company(
            args.remove_company_key,
            company=args.remove_company_name,
            reason="manual-remove",
        )
        _render_outputs(db, cfg, backend_name)
        log.info(
            "removed %s from blotter (%d deals deleted)",
            info["company"],
            info["deals_removed"],
        )
        return 0

    skip_urls = db.scanned_urls()

    raw = sources.fetch_all(cfg["sources"], skip_urls=skip_urls)
    if not raw:
        recent = db.recent(cfg["output"]["window_days"])
        if recent:
            log.info("no new articles; %d stored deals in %s", len(recent), backend_name)
            _render_outputs(db, cfg, backend_name)
            return 0
        log.error("no items fetched from any source")
        return 1
    log.info("fetched %d new raw items (%d urls already scanned)", len(raw), len(skip_urls))

    ex_cfg = dict(cfg["extraction"])
    if args.no_llm:
        ex_cfg["regex_fastpath"] = True
        import os
        os.environ.pop("GROQ_API_KEY", None)
    deals = extract.extract_all(raw, ex_cfg)
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

    if args.dry_run:
        for d in deals[:25]:
            amt = d.get("amount_raw") or "n/d"
            sector = d.get("sector_label") or "-"
            print(f"  {d['company'][:30]:<30} {amt:>10}  {d.get('stage',''):<10} "
                  f"{sector:<18} {d.get('location','')[:20]}")
        log.info("dry run: %d deals kept, nothing written", len(deals))
        return 0

    db.upsert(deals, cfg["filters"], cfg["store"]["dedupe_window_days"])
    db.collapse_duplicate_deals()
    by_url = {d.get("url"): d for d in scanned_batch if d.get("url")}
    to_mark = []
    for item in raw:
        url = item.get("url") or ""
        row = by_url.get(url, item)
        to_mark.append(row)
    db.mark_scanned(to_mark)

    recent = db.recent(cfg["output"]["window_days"])
    _render_outputs(db, cfg, backend_name)

    flagged = sum(1 for d in recent if d.get("priority"))
    log.info("done: %d rounds in %s, %d flagged", len(recent), backend_name, flagged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
