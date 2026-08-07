"""Source adapters. Each returns a list of RawItem dicts with a common shape.

RawItem = {
    "source": str,        # which site it came from
    "title": str,
    "text": str,          # summary / body text used for extraction
    "url": str,
    "published_at": str,  # ISO date, best effort
    "template_hint": str, # "finsmes" | "generic"
}
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from html import unescape
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# A real browser UA. Sites like FinSMEs (WordPress + WAF) 403 obvious bot
# strings, so present as a normal browser. Not an attempt to hide — one polite
# request per page per day, see fetch_finsmes.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# FinSMEs blocks /feed but serves rendered pages fine, so those need a fuller,
# browser-like header set.
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 25


def _strip_html(s: str) -> str:
    if not s:
        return ""
    txt = BeautifulSoup(s, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", unescape(txt)).strip()


def _iso(struct_time) -> str:
    if not struct_time:
        return datetime.now(timezone.utc).date().isoformat()
    try:
        return datetime(*struct_time[:6], tzinfo=timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).date().isoformat()


def fetch_rss(name: str, cfg: dict) -> list[dict]:
    """Pull entries from one or more RSS/Atom feeds."""
    items: list[dict] = []
    seen_urls: set[str] = set()

    for url in cfg.get("urls", []):
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:
            log.warning("%s: feed %s failed: %s", name, url, exc)
            continue

        if parsed.bozo and not parsed.entries:
            log.warning("%s: feed %s parsed empty (bozo=%s)", name, url, parsed.bozo_exception)
            continue

        for entry in parsed.entries:
            link = entry.get("link", "").strip()
            if not link or link in seen_urls:
                continue
            seen_urls.add(link)

            body = entry.get("summary", "") or ""
            if entry.get("content"):
                body = entry["content"][0].get("value", body)

            items.append(
                {
                    "source": name,
                    "title": _strip_html(entry.get("title", "")),
                    "text": _strip_html(body),
                    "url": link,
                    "published_at": _iso(
                        entry.get("published_parsed") or entry.get("updated_parsed")
                    ),
                    "template_hint": cfg.get("template_hint", "generic"),
                }
            )

        log.info("%s: %s -> %d entries", name, url, len(parsed.entries))

    return items


def fetch_html(name: str, cfg: dict) -> list[dict]:
    """Try JSON endpoints first, then fall back to parsing HTML rows.

    aifunding.me has no confirmed feed, so this adapter is deliberately
    defensive. Use --probe to see what it actually returns before trusting it.
    """
    # 1. JSON endpoints are far more stable than scraped markup.
    for probe in cfg.get("json_probe", []):
        try:
            resp = requests.get(probe, headers={"User-Agent": UA}, timeout=TIMEOUT)
            if resp.ok and "json" in resp.headers.get("content-type", ""):
                payload = resp.json()
                items = _items_from_json(name, payload, cfg)
                if items:
                    log.info("%s: JSON endpoint %s -> %d items", name, probe, len(items))
                    return items
        except Exception as exc:
            log.debug("%s: json probe %s failed: %s", name, probe, exc)

    # 2. Fall back to HTML.
    items: list[dict] = []
    today = datetime.now(timezone.utc).date().isoformat()

    for url in cfg.get("urls", []):
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("%s: %s failed: %s", name, url, exc)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # Next.js and similar frameworks embed the page data as JSON. If it's
        # there, it beats scraping the rendered table every time.
        for tag in soup.find_all("script", {"type": "application/json"}):
            try:
                embedded = json.loads(tag.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            found = _items_from_json(name, embedded, cfg)
            if found:
                log.info("%s: embedded JSON -> %d items", name, len(found))
                return found

        rows = soup.select(cfg.get("selectors", {}).get("row", "tr"))
        for row in rows:
            text = re.sub(r"\s+", " ", row.get_text(" ")).strip()
            if len(text) < 25:
                continue
            anchor = row.select_one(cfg.get("selectors", {}).get("link", "a"))
            href = anchor["href"] if anchor and anchor.has_attr("href") else url
            if href.startswith("/"):
                href = url.split("/", 3)[0] + "//" + url.split("/")[2] + href

            items.append(
                {
                    "source": name,
                    "title": text[:160],
                    "text": text,
                    "url": href,
                    "published_at": today,
                    "template_hint": cfg.get("template_hint", "generic"),
                }
            )

        log.info("%s: %s -> %d rows scraped", name, url, len(items))

    return items


def _items_from_json(name: str, payload, cfg: dict) -> list[dict]:
    """Walk an arbitrary JSON blob looking for a list of deal-shaped dicts."""
    today = datetime.now(timezone.utc).date().isoformat()
    candidates: list[dict] = []

    def looks_like_deal(d: dict) -> bool:
        keys = {k.lower() for k in d}
        has_name = bool(keys & {"company", "name", "startup", "companyname"})
        has_money = bool(keys & {"amount", "raised", "round", "stage", "amountraised", "funding"})
        return has_name and has_money

    def walk(node):
        if isinstance(node, dict):
            if looks_like_deal(node):
                candidates.append(node)
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)

    def pick(d: dict, *names):
        for n in names:
            for k, v in d.items():
                if k.lower() == n and v not in (None, ""):
                    return str(v)
        return ""

    items = []
    for d in candidates:
        company = pick(d, "company", "name", "startup", "companyname")
        if not company:
            continue
        bits = [
            company,
            pick(d, "description", "summary", "about", "tagline"),
            pick(d, "amount", "raised", "amountraised", "funding"),
            pick(d, "round", "stage", "roundtype"),
            pick(d, "investors", "leadinvestor", "investor"),
            pick(d, "location", "city", "hq"),
        ]
        items.append(
            {
                "source": name,
                "title": company,
                "text": " | ".join(b for b in bits if b),
                "url": pick(d, "url", "link", "source", "website") or "",
                "published_at": (pick(d, "date", "announced", "publisheddate") or today)[:10],
                "template_hint": "generic",
            }
        )

    return items


# A FinSMEs article URL: https://www.finsmes.com/2026/07/<slug>.html
FINSMES_POST_RE = re.compile(r"^https?://(?:www\.)?finsmes\.com/\d{4}/\d{2}/[^\"'?#]+\.html$")


def _finsmes_article(name: str, url: str, template_hint: str) -> dict | None:
    """Fetch one FinSMEs post and pull the title, body, and publish date."""
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("finsmes: article %s failed: %s", url, exc)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    title_tag = soup.select_one("h1.entry-title, h1.tdb-title-text, h1")
    og = soup.select_one('meta[property="og:title"]')
    title = _strip_html(
        title_tag.get_text(" ") if title_tag else (og.get("content", "") if og else "")
    )

    paras = [
        re.sub(r"\s+", " ", p.get_text(" ")).strip()
        for p in soup.select("div.td-post-content p, .tdb-block-inner p, article p")
    ]
    text = " ".join(p for p in paras if p).strip()
    # Bodies open with a stray bullet/dash ("- Dili , a NYC-based...") that would
    # otherwise land in the regex-captured company name.
    text = re.sub(r"^[\s\-\u2013\u2014\u2022*]+", "", text)
    if not text:
        return None

    meta = soup.select_one('meta[property="article:published_time"]')
    if meta and meta.get("content"):
        published_at = meta["content"][:10]
    else:
        # URL carries /YYYY/MM/; fall back to that, then today.
        m = re.search(r"/(\d{4})/(\d{2})/", url)
        published_at = (
            f"{m.group(1)}-{m.group(2)}-01" if m
            else datetime.now(timezone.utc).date().isoformat()
        )

    return {
        "source": name,
        "title": title,
        "text": text[:4000],
        "url": url,
        "published_at": published_at,
        "template_hint": template_hint,
    }


def _page_url(base: str, page: int) -> str:
    base = base.rstrip("/") + "/"
    if page <= 1:
        return base
    return urljoin(base, f"page/{page}/")


def _dated_listing_posts(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Return [(article_url, YYYY-MM-DD), ...] from a FinSMEs listing page."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for time_el in soup.select("time[datetime]"):
        raw_dt = (time_el.get("datetime") or "")[:10]
        try:
            date.fromisoformat(raw_dt)
        except ValueError:
            continue
        node = time_el
        href = None
        for _ in range(8):
            node = node.parent
            if not node:
                break
            a = node.find("a", href=FINSMES_POST_RE)
            if a and a.get("href"):
                href = a["href"].strip()
                break
        if not href or href in seen:
            continue
        seen.add(href)
        out.append((href, raw_dt))
    return out


def fetch_finsmes(
    name: str, cfg: dict, skip_urls: set[str] | None = None
) -> list[dict]:
    """FinSMEs' /feed is WAF-blocked (403), but its category and article pages
    serve normally.

    Walks category pages newest-first until the page's newest post is older
    than the lookback window (default: stop once we reach day-before-yesterday).
    Skips article URLs already in skip_urls (scanned_posts).
    """
    template_hint = cfg.get("template_hint", "finsmes")
    delay = float(cfg.get("article_delay", 0.4))
    lookback_days = int(cfg.get("lookback_days", 1))
    # Safety only — normal stop is date-based. Prevents infinite loops if the
    # site stops embedding dates.
    safety_pages = int(cfg.get("max_pages", 100))
    skip_urls = skip_urls or set()
    cutoff = (
        datetime.now(timezone.utc).date() - timedelta(days=lookback_days)
    ).isoformat()

    items: list[dict] = []
    fetched = skipped = 0

    for base in cfg.get("urls", []):
        for page in range(1, safety_pages + 1):
            page_url = _page_url(base, page)
            try:
                resp = requests.get(page_url, headers=BROWSER_HEADERS, timeout=TIMEOUT)
                resp.raise_for_status()
            except Exception as exc:
                log.warning("finsmes: listing %s failed: %s", page_url, exc)
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            posts = _dated_listing_posts(soup)
            if not posts:
                log.info("finsmes: listing %s -> 0 dated posts; stopping", page_url)
                break

            dates = [d for _, d in posts]
            newest, oldest = max(dates), min(dates)
            log.info(
                "finsmes: listing %s -> %d posts (%s .. %s)",
                page_url,
                len(posts),
                oldest,
                newest,
            )

            # Entire page is before lookback (day-before-yesterday and older).
            if newest < cutoff:
                log.info(
                    "finsmes: stopping — page newest %s is before cutoff %s",
                    newest,
                    cutoff,
                )
                break

            for href, listed_date in posts:
                if listed_date < cutoff:
                    continue
                if href in skip_urls:
                    skipped += 1
                    continue
                art = _finsmes_article(name, href, template_hint)
                fetched += 1
                if art:
                    pub = (art.get("published_at") or listed_date)[:10]
                    art["published_at"] = pub
                    if pub >= cutoff:
                        items.append(art)
                if delay:
                    time.sleep(delay)

            # Page straddles the cutoff — in-window posts handled; don't go deeper.
            if oldest < cutoff:
                log.info(
                    "finsmes: stopping — page oldest %s crossed cutoff %s",
                    oldest,
                    cutoff,
                )
                break

    log.info(
        "finsmes: %d new articles (fetched %d, skipped %d scanned, "
        "lookback %d day(s), cutoff %s)",
        len(items),
        fetched,
        skipped,
        lookback_days,
        cutoff,
    )
    return items


FETCHERS = {"rss": fetch_rss, "html": fetch_html, "finsmes": fetch_finsmes}


def fetch_all(
    sources_cfg: dict,
    only: str | None = None,
    skip_urls: set[str] | None = None,
) -> list[dict]:
    items: list[dict] = []
    for name, cfg in sources_cfg.items():
        if only and name != only:
            continue
        if not cfg.get("enabled", True):
            continue
        kind = cfg.get("kind", "rss")
        fetcher = FETCHERS.get(kind)
        if not fetcher:
            log.warning("%s: unknown kind %r", name, kind)
            continue
        try:
            if kind == "finsmes":
                items.extend(fetcher(name, cfg, skip_urls=skip_urls))
            else:
                items.extend(fetcher(name, cfg))
        except Exception as exc:
            log.exception("%s: adapter crashed: %s", name, exc)
    return items
