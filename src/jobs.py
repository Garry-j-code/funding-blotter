"""Careers: find each company's public job board and keep its matching open roles in sync.

Uses the public job-board APIs of Ashby, Greenhouse, Lever and Workable. A company's own
careers page (a URL the admin saved) is only read for (1) a link or embed of one of those
job boards and (2) standard schema.org JobPosting data. No LLM calls, so it costs no
Groq/Tavily tokens.

  uv run python -m src.jobs --dry-run                 # report only, writes nothing
  uv run python -m src.jobs                           # discover + refresh everything due
  uv run python -m src.jobs --company-key hanoverpark
  uv run python -m src.jobs --discover-only --report /tmp/careers.csv

Rules that matter (see AGENTS.md):
  * A failed fetch NEVER closes jobs.
  * A board that had open roles and suddenly returns nothing is held for one run first.
  * A URL the admin set (source = "manual") is never overwritten by discovery.
  * Guessed boards that cannot be verified stay `verified = false` (admin-only in the API).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html as htmllib
import ipaddress
import json
import logging
import re
import socket
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
import yaml
from dotenv import load_dotenv

log = logging.getLogger("jobs")

ROOT = Path(__file__).resolve().parent.parent
HEADERS = {"User-Agent": "funding-blotter-careers/1.0"}
TIMEOUT = 20
ATS_ORDER = ("ashby", "greenhouse", "lever", "workable")
HUMAN_URL = {
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "greenhouse": "https://job-boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "workable": "https://apply.workable.com/{slug}",
}


# ======================================================================
# Data types
# ======================================================================
@dataclass
class Posting:
    external_id: str
    title: str
    department: str = ""
    location: str = ""
    employment_type: str = ""
    url: str = ""
    posted_at: str | None = None


@dataclass
class Board:
    ats: str
    slug: str
    company_name: str | None
    postings: list[Posting]
    text_sample: str = ""          # a little posting text, used to verify the company


@dataclass
class Outcome:
    """Result of processing one company. Built by workers (HTTP only), applied by the main thread."""
    company_key: str
    company: str
    action: str                    # discover | refresh | skip
    record: dict | None = None     # company_careers row to upsert
    matches: list[dict] | None = None   # matching jobs (None = no successful fetch)
    board_total: int | None = None
    error: str | None = None
    verified: bool = False
    limited: bool = False        # could not be checked because a host was throttling us


class FetchError(Exception):
    pass


class RateLimited(FetchError):
    """A host is throttling us (HTTP 429, or paused after repeated 429s). Not a problem with our data."""


class HostLimiter:
    """Spaces requests to one host (shared by every worker thread) and pauses the host if it keeps saying 429."""

    def __init__(self, interval: float, trip_after: int = 6, cooldown: float = 300.0) -> None:
        self.interval, self.trip_after, self.cooldown = interval, trip_after, cooldown
        self._lock = threading.Lock()
        self._next = 0.0
        self._tripped_until = 0.0
        self._streak = 0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._tripped_until:
                raise RateLimited("host is cooling down after repeated 429s")
            slot = max(now, self._next)
            self._next = slot + self.interval
        wait = slot - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def penalize(self, seconds: float) -> None:
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)

    def ok(self) -> None:
        with self._lock:
            self._streak = 0

    def trip(self, seconds: float) -> None:
        """Pause this host for `seconds` (the server told us to go away for that long)."""
        with self._lock:
            self._tripped_until = time.monotonic() + seconds
            self._streak = 0

    def limited(self) -> None:
        with self._lock:
            self._streak += 1
            if self._streak >= self.trip_after:
                self._tripped_until = time.monotonic() + self.cooldown
                self._streak = 0


_SETTINGS = {"default_interval": 0.15, "host_intervals": {}, "trip_after": 6, "cooldown": 300.0, "max_wait": 120.0}
_LIMITERS: dict[str, HostLimiter] = {}
_LIMITERS_LOCK = threading.Lock()
MAX_429_WAIT = 120.0            # longest we will sit and wait out one 429; a longer Retry-After pauses the host


def configure(jobs_cfg: dict) -> None:
    """Apply config.yaml -> jobs (request pacing). Resets all host limiters."""
    hosts = dict(jobs_cfg.get("host_min_interval") or {})
    _SETTINGS.update(
        default_interval=float(hosts.pop("default", jobs_cfg.get("request_delay", 0.15))),
        host_intervals={k: float(v) for k, v in hosts.items()},
        trip_after=int(jobs_cfg.get("rate_limit_trip_after", 6)),
        cooldown=float(jobs_cfg.get("rate_limit_cooldown", 300)),
        max_wait=float(jobs_cfg.get("rate_limit_max_wait", MAX_429_WAIT)),
    )
    with _LIMITERS_LOCK:
        _LIMITERS.clear()


def limiter_for(host: str) -> HostLimiter:
    with _LIMITERS_LOCK:
        lim = _LIMITERS.get(host)
        if lim is None:
            lim = HostLimiter(_SETTINGS["host_intervals"].get(host, _SETTINGS["default_interval"]),
                              _SETTINGS["trip_after"], _SETTINGS["cooldown"])
            _LIMITERS[host] = lim
        return lim


def _retry_after(resp) -> float | None:
    try:
        return float(resp.headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        return None


def _get(url: str, params: dict | None = None, attempts: int = 3) -> requests.Response | None:
    """Paced GET with retries. Returns None on 404 ("no such board").

    Raises RateLimited if the host keeps answering 429, FetchError for other failures.
    """
    limiter = limiter_for(urlparse(url).hostname or "")
    last, was_limited = "unknown error", False
    for attempt in range(1, attempts + 2):
        limiter.acquire()
        was_limited = False
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last = str(exc)[:80]
        else:
            if resp.status_code == 404:
                limiter.ok()
                return None
            if resp.status_code == 200:
                limiter.ok()
                return resp
            last = f"HTTP {resp.status_code}"
            if resp.status_code == 429:
                was_limited = True
                wait = _retry_after(resp)
                if wait is not None and wait > _SETTINGS["max_wait"]:
                    limiter.trip(wait)                  # told to stay away for a long time: stop now
                    raise RateLimited(f"HTTP 429, retry after {int(wait)}s")
                limiter.penalize(min(wait or 5.0 * 2 ** (attempt - 1), _SETTINGS["max_wait"]))
            elif resp.status_code < 500:
                raise FetchError(last)
        if attempt >= (attempts + 1 if was_limited else attempts):
            break
        if not was_limited:
            time.sleep(2 * attempt)
    if was_limited:
        limiter.limited()
        raise RateLimited(last)
    raise FetchError(last)


def _iso_from_ms(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _clean_ts(value) -> str | None:
    """A timestamp the database will accept, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def fetch_ashby(slug: str) -> Board | None:
    resp = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if resp is None:
        return None
    jobs = [j for j in (resp.json().get("jobs") or []) if j.get("isListed", True)]
    postings = [
        Posting(
            external_id=str(j.get("id") or (j.get("jobUrl") or "").rstrip("/").rsplit("/", 1)[-1]),
            title=j.get("title") or "",
            department=j.get("department") or j.get("team") or "",
            location=j.get("location") or "",
            employment_type=j.get("employmentType") or "",
            url=j.get("jobUrl") or "",
            posted_at=j.get("publishedAt"),
        )
        for j in jobs
    ]
    text = " ".join((j.get("descriptionPlain") or "")[:600] for j in jobs[:6])
    return Board("ashby", slug, None, postings, text)


def fetch_greenhouse(slug: str) -> Board | None:
    meta = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
    if meta is None:
        return None
    name = meta.json().get("name")
    resp = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    jobs = (resp.json().get("jobs") if resp is not None else []) or []
    postings = [
        Posting(
            external_id=str(j.get("id")),
            title=j.get("title") or "",
            location=(j.get("location") or {}).get("name", "") or "",
            url=j.get("absolute_url") or "",
            posted_at=j.get("first_published") or j.get("updated_at"),
        )
        for j in jobs
    ]
    if not name and jobs:
        name = jobs[0].get("company_name")
    return Board("greenhouse", slug, name, postings)


def fetch_lever(slug: str) -> Board | None:
    resp = _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if resp is None:
        return None
    jobs = resp.json()
    if not isinstance(jobs, list):
        return None
    postings = []
    for j in jobs:
        cats = j.get("categories") or {}
        postings.append(Posting(
            external_id=str(j.get("id")),
            title=j.get("text") or "",
            department=cats.get("team") or cats.get("department") or "",
            location=cats.get("location") or "",
            employment_type=cats.get("commitment") or "",
            url=j.get("hostedUrl") or "",
            posted_at=_iso_from_ms(j.get("createdAt")),
        ))
    text = " ".join((j.get("descriptionPlain") or "")[:600] for j in jobs[:6])
    return Board("lever", slug, None, postings, text)


def fetch_workable(slug: str) -> Board | None:
    resp = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    if resp is None:
        return None
    data = resp.json()
    postings = []
    for j in data.get("jobs") or []:
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        postings.append(Posting(
            external_id=str(j.get("shortcode") or j.get("id") or ""),
            title=j.get("title") or "",
            department=j.get("department") or "",
            location=loc,
            employment_type=j.get("employment_type") or "",
            url=j.get("url") or "",
            posted_at=j.get("published_on") or j.get("created_at"),
        ))
    return Board("workable", slug, data.get("name"), postings)


FETCHERS = {
    "ashby": fetch_ashby,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "workable": fetch_workable,
}


# ======================================================================
# Names, URLs, verification
# ======================================================================
_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|company|co|technologies|technology|labs|holdings|group)\b\.?",
    re.I,
)


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def clean_name(name: str) -> str:
    """'Foo (fka Bar), Inc' -> 'Foo'."""
    n = re.sub(r"\(.*?\)", "", name or "")
    n = re.sub(r"\b(fka|formerly known as|doing business as|dba)\b.*", "", n, flags=re.I)
    return re.sub(r"\s+", " ", n).strip(" ,.")


def slug_candidates(name: str, limit: int = 6) -> list[str]:
    """Likely job-board names for a company: joined and hyphenated, with and without Inc/Labs/AI."""
    n = clean_name(name).lower()
    bases = []
    for b in (n, _SUFFIX.sub("", n)):
        b = re.sub(r"[^a-z0-9 ]", "", b).strip()
        b = re.sub(r"\s+", " ", b)
        if b and b not in bases:
            bases.append(b)
    out: list[str] = []
    for b in bases:
        variants = [b]
        if b.endswith(" ai"):
            variants.append(b[:-3])
        for v in variants:
            for s in (v.replace(" ", ""), v.replace(" ", "-")):
                if s and s not in out:
                    out.append(s)
    return out[:limit]


def verify_board(company: str, board: Board) -> bool:
    """Is this board really this company's? Short names (<5 chars) need an exact board-name match."""
    cn = norm(clean_name(company))
    names = {cn, norm(_SUFFIX.sub("", clean_name(company)))} - {""}
    if not names:
        return False
    bn = norm(board.company_name or "")
    if bn and bn in names:
        return True
    if len(cn) < 5:
        return False
    if bn and (cn in bn or bn in cn):
        return True
    return cn in norm(board.text_sample)


def human_url(ats: str, slug: str) -> str:
    return HUMAN_URL[ats].format(slug=slug)


def parse_board_url(url: str) -> tuple[str, str] | None:
    """(ats, slug) for a supported job-board URL, else None."""
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    first = parts[0] if parts else ""
    if host == "jobs.ashbyhq.com" and first:
        return "ashby", first
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io",
                "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"):
        if first in ("embed",):
            slug = (parse_qs(u.query).get("for") or [""])[0]
            return ("greenhouse", slug) if slug else None
        return ("greenhouse", first) if first else None
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and first:
        return "lever", first
    if host == "apply.workable.com" and first:
        return "workable", first
    if host.endswith(".workable.com") and host.count(".") == 2 and host.split(".")[0] not in ("www", "apply"):
        return "workable", host.split(".")[0]
    return None


def validate_url(url: str) -> str:
    """Normalise an admin-supplied URL or raise ValueError. http/https only."""
    url = (url or "").strip()
    if not url or len(url) > 500 or re.search(r"\s", url):
        raise ValueError("URL is empty, too long, or contains spaces")
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    if not u.hostname or "." not in u.hostname:
        raise ValueError("URL has no valid host")
    if u.username or u.password:
        raise ValueError("URL must not contain a username or password")
    return url


# ======================================================================
# Role matching
# ======================================================================
class RoleMatcher:
    """Matches a job title against the configured roles (config.yaml -> jobs.roles)."""

    ENGINEER = re.compile(r"engineer|engineering|developer|\bFDE\b", re.I)

    def __init__(self, jobs_cfg: dict) -> None:
        self.roles = [
            (r["role_type"], [re.compile(p, re.I) for p in r["patterns"]], r.get("requires_engineer", True))
            for r in jobs_cfg["roles"]
        ]
        self.exclude = re.compile(jobs_cfg["exclude_title"], re.I) if jobs_cfg.get("exclude_title") else None

    def match(self, title: str) -> tuple[str, list[str]] | None:
        """(primary role_type, all matching role types) or None."""
        if not title or (self.exclude and self.exclude.search(title)):
            return None
        tags = []
        for role_type, patterns, needs_engineer in self.roles:
            if needs_engineer and not self.ENGINEER.search(title):
                continue
            if any(p.search(title) for p in patterns):
                tags.append(role_type)
        return (tags[0], tags) if tags else None


def matching_jobs(board: Board, matcher: RoleMatcher) -> list[dict]:
    out = []
    for p in board.postings:
        m = matcher.match(p.title)
        if not m or not p.external_id or not p.url:
            continue
        out.append({
            "external_id": p.external_id, "title": p.title[:200], "role_type": m[0], "tags": m[1],
            "department": p.department[:100] or None, "location": p.location[:150] or None,
            "employment_type": p.employment_type[:50] or None, "url": p.url,
            "posted_at": _clean_ts(p.posted_at),
        })
    return out


# ======================================================================
# Custom careers pages (a URL the admin saved that is not a job-board URL)
# ======================================================================
# The page is read once per run, only to find (1) a link/embed of a supported job board, which is
# then used through its API like any other board, or (2) standard schema.org JobPosting data
# (JSON-LD). Nothing else on the page is interpreted, and no LLM is involved.
_ATS_URL = re.compile(
    r"https?://[a-z0-9.-]*(?:ashbyhq\.com|greenhouse\.io|lever\.co|workable\.com)[^\s\"'<>)\\]*", re.I)
_ATS_API_URL = (
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([\w-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)", re.I)),
    ("lever", re.compile(r"api\.lever\.co/v0/postings/([\w-]+)", re.I)),
)
_NOT_A_SLUG = {"embed", "api", "static", "assets", "js", "job_board", "jobs", "v1", "posting-api", "careers", "search"}
_LD_BLOCK = re.compile(r"<script[^>]+type=[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>", re.I | re.S)
MAX_PAGE_CHARS = 3_000_000


def _assert_public(host: str) -> None:
    """Refuse hosts that point at this machine or a private network."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise FetchError(f"cannot resolve {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise FetchError(f"{host} is not a public address")


def _robots_allow(url: str) -> bool:
    """False only if the site's robots.txt clearly forbids us. Any trouble reading it counts as allowed."""
    p = urlparse(url)
    try:
        resp = _get(f"{p.scheme}://{p.netloc}/robots.txt", attempts=1)
    except FetchError:
        return True
    if resp is None:
        return True
    rp = RobotFileParser()
    rp.parse(resp.text.splitlines())
    return rp.can_fetch(HEADERS["User-Agent"], url)


def fetch_page(url: str) -> str | None:
    """The page's HTML, or None when robots.txt forbids it or it is not an HTML page.

    Raises RateLimited / FetchError when the page cannot be read (the caller keeps old data).
    """
    host = urlparse(url).hostname or ""
    _assert_public(host)
    if not _robots_allow(url):
        log.info("careers: robots.txt does not allow reading %s", url)
        return None
    resp = _get(url, attempts=2)
    if resp is None:
        raise FetchError("page not found (404)")
    _assert_public(urlparse(resp.url).hostname or host)           # a redirect must not lead somewhere private
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if ctype and "html" not in ctype and "xml" not in ctype:
        return None
    return resp.text[:MAX_PAGE_CHARS]


def find_board_links(page: str) -> list[tuple[str, str]]:
    """Supported job boards linked or embedded in the page, most mentioned first."""
    text = htmllib.unescape(page).replace("\\/", "/")
    found: list[tuple[str, str]] = []
    for m in _ATS_URL.finditer(text):
        board = parse_board_url(m.group(0).rstrip(".,;"))
        if board:
            found.append(board)
    for ats, rx in _ATS_API_URL:
        found.extend((ats, slug) for slug in rx.findall(text))
    counts = Counter((ats, slug.lower() if ats != "workable" else slug) for ats, slug in found
                     if slug and slug.lower() not in _NOT_A_SLUG)
    return [b for b, _ in counts.most_common()]


def _ld_nodes(node):
    if isinstance(node, list):
        for x in node:
            yield from _ld_nodes(x)
    elif isinstance(node, dict):
        types = node.get("@type")
        if "JobPosting" in (types if isinstance(types, list) else [types]):
            yield node
        for k in ("@graph", "itemListElement", "item", "mainEntity"):
            if k in node:
                yield from _ld_nodes(node[k])


def _text(value) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("value") or ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", htmllib.unescape(str(value or "")))).strip()


def _ld_location(node: dict) -> str:
    parts: list[str] = []
    locs = node.get("jobLocation")
    for loc in (locs if isinstance(locs, list) else [locs]):
        addr = loc.get("address") if isinstance(loc, dict) else None
        if isinstance(addr, dict):
            parts.append(", ".join(x for x in (_text(addr.get("addressLocality")), _text(addr.get("addressRegion")),
                                               _text(addr.get("addressCountry"))) if x))
        elif isinstance(addr, str):
            parts.append(_text(addr))
    if node.get("jobLocationType") == "TELECOMMUTE":
        parts.append("Remote")
    return "; ".join(p for p in parts if p)


def extract_job_postings(page: str, page_url: str) -> list[Posting]:
    """schema.org JobPosting entries (JSON-LD) found in the page."""
    out: dict[str, Posting] = {}
    today = datetime.now(timezone.utc)
    for block in _LD_BLOCK.findall(page):
        try:
            data = json.loads(htmllib.unescape(block.strip()))
        except ValueError:
            continue
        for node in _ld_nodes(data):
            title = _text(node.get("title") or node.get("name"))
            if not title:
                continue
            valid_to = _clean_ts(node.get("validThrough"))
            if valid_to and datetime.fromisoformat(valid_to).replace(tzinfo=None) < today.replace(tzinfo=None):
                continue                                           # expired posting
            url = urljoin(page_url, str(node.get("url") or "")) if node.get("url") else page_url
            ident = _text(node.get("identifier"))
            location = _ld_location(node)
            ext = "ld:" + (ident or hashlib.sha1(f"{title}|{url}|{location}".encode()).hexdigest()[:16])
            emp = node.get("employmentType")
            out.setdefault(ext, Posting(
                external_id=ext, title=title, department=_text(node.get("occupationalCategory")),
                location=location, employment_type=_text(", ".join(emp) if isinstance(emp, list) else emp),
                url=url, posted_at=_clean_ts(node.get("datePosted"))))
    return list(out.values())


# ======================================================================
# Sync logic (pure: no network, no database)
# ======================================================================
@dataclass
class SyncPlan:
    upserts: list[dict] = field(default_factory=list)
    close_ids: list[int] = field(default_factory=list)
    held: bool = False
    new_empty_streak: int = 0
    inserted: int = 0
    reopened: int = 0


def plan_sync(
    company_key: str,
    existing: list[dict],
    matches: list[dict],
    board_total: int,
    empty_streak: int,
    today: date,
) -> SyncPlan:
    """Decide what to write after a SUCCESSFUL fetch of a company's board.

    * matches are upserted (first_seen is kept for jobs we already knew)
    * open jobs that are no longer listed are closed ...
    * ... except when the whole board came back empty and we still had open jobs: hold one run
    """
    existing_by_ext = {e["external_id"]: e for e in existing}
    plan = SyncPlan()
    seen_ids = set()
    for m in matches:
        seen_ids.add(m["external_id"])
        prev = existing_by_ext.get(m["external_id"])
        if prev is None:
            plan.inserted += 1
        elif not prev.get("is_open"):
            plan.reopened += 1
        plan.upserts.append({
            "company_key": company_key, **m,
            "first_seen": (prev or {}).get("first_seen") or today.isoformat(),
            "last_seen": today.isoformat(), "closed_at": None, "is_open": True,
        })
    missing = [e for e in existing if e.get("is_open") and e["external_id"] not in seen_ids]
    if board_total == 0 and missing and empty_streak < 1:
        plan.held, plan.new_empty_streak = True, empty_streak + 1
    else:
        plan.close_ids = [e["id"] for e in missing]
        plan.new_empty_streak = 0
    return plan


# ======================================================================
# Per-company processing (HTTP only; safe to run in worker threads)
# ======================================================================
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def discover(company: str, cfg: dict) -> tuple[Board | None, bool]:
    """Find a board for the company. Returns (board, verified); a verified board wins immediately.

    Ashby, Greenhouse and Lever are tried for every name guess first. Workable (which throttles hard)
    is only asked afterwards, and only for the first `workable_max_candidates` guesses.
    """
    unverified: Board | None = None
    failed = limited = 0
    candidates = slug_candidates(company, cfg["max_slug_candidates"])
    for group in (("ashby", "greenhouse", "lever"), ("workable",)):
        for i, slug in enumerate(candidates):
            for ats in group:
                if ats == "workable" and i >= cfg.get("workable_max_candidates", 2):
                    continue
                try:
                    board = FETCHERS[ats](slug)
                except RateLimited:
                    limited += 1
                    continue
                except FetchError:
                    failed += 1
                    continue
                if board is None:
                    continue
                if verify_board(company, board):
                    return board, True
                unverified = unverified or board
    if unverified is None:
        # Some requests did not get an answer, so "nothing found" cannot be trusted.
        if failed:
            raise FetchError(f"{failed} request(s) failed during discovery")
        if limited:
            raise RateLimited(f"{limited} request(s) were rate limited during discovery")
    return unverified, False


def process_company(item: dict, cfg: dict, matcher: RoleMatcher) -> Outcome:
    """item = {company_key, company, care (existing company_careers row or None), force}"""
    key, name, care = item["company_key"], item["company"], item.get("care")
    base = dict(care or {})
    base.update({"company_key": key, "company": name, "last_checked": _now(), "needs_refresh": False})
    base.setdefault("source", "auto")

    # Admin set a URL that is not a supported job board: read the page itself (see process_custom).
    if care and care.get("source") == "manual" and care.get("ats") not in FETCHERS:
        if care.get("careers_url"):
            return process_custom(key, name, base, care, matcher)
        clear = {"company_key": key, "needs_refresh": False} if care.get("needs_refresh") else None
        return Outcome(key, name, "skip", record=clear)

    try:
        if care and care.get("ats") in FETCHERS and care.get("ats_slug"):
            board = FETCHERS[care["ats"]](care["ats_slug"])
            if board is None:
                return _board_missing(key, name, base, care)
            verified = bool(care.get("verified"))
            action = "refresh"
        else:
            board, verified = discover(name, cfg)
            action = "discover"
            if board is None:
                base.update({"status": "no_board", "ats": None, "ats_slug": None,
                             "careers_url": None, "verified": False})
                return Outcome(key, name, action, record=base)
    except RateLimited:
        # Throttled, not broken: try again next run, and do not count it as an error.
        if care and care.get("ats") in FETCHERS and care.get("ats_slug"):
            return Outcome(key, name, "refresh", record=base, limited=True)
        base["status"] = "pending"
        return Outcome(key, name, "discover", record=base, limited=True)
    except FetchError as exc:
        base.update({"status": "error", "consecutive_errors": int((care or {}).get("consecutive_errors") or 0) + 1})
        return Outcome(key, name, "refresh" if care else "discover", record=base, error=str(exc))

    base.update({
        "ats": board.ats, "ats_slug": board.slug, "verified": verified,
        "status": "ok" if verified else "unverified", "consecutive_errors": 0, "last_ok": _now(),
    })
    if not (care and care.get("source") == "manual" and care.get("careers_url")):
        base["careers_url"] = human_url(board.ats, board.slug)
    return Outcome(key, name, action, record=base, matches=matching_jobs(board, matcher),
                   board_total=len(board.postings), verified=verified)


def process_custom(key: str, name: str, base: dict, care: dict, matcher: RoleMatcher) -> Outcome:
    """The admin's own careers URL is not a job-board URL. Look for a job board linked on the page, then for
    JobPosting data; if neither is there, the URL is still shown on the card but no roles are listed."""
    url = care["careers_url"]
    action = "refresh" if care.get("last_ok") else "discover"
    try:
        page = fetch_page(url)
        board: Board | None = None
        if page is not None:
            for ats, slug in find_board_links(page)[:3]:
                board = FETCHERS[ats](slug)
                if board is not None:
                    break
            if board is not None:
                log.info("careers: %s -> %s board '%s' found on %s", name, board.ats, board.slug, url)
                base.update({"ats": board.ats, "ats_slug": board.slug, "status": "ok", "verified": True,
                             "consecutive_errors": 0, "last_ok": _now()})
                return Outcome(key, name, action, record=base, matches=matching_jobs(board, matcher),
                               board_total=len(board.postings), verified=True)
            postings = extract_job_postings(page, url)
            if postings:
                board = Board("custom", urlparse(url).hostname or "", None, postings)
                base.update({"ats": "custom", "ats_slug": None, "status": "ok", "verified": True,
                             "consecutive_errors": 0, "last_ok": _now()})
                return Outcome(key, name, action, record=base, matches=matching_jobs(board, matcher),
                               board_total=len(postings), verified=True)
    except RateLimited:
        return Outcome(key, name, action, record=base, limited=True)
    except FetchError as exc:
        base.update({"status": "error", "consecutive_errors": int(care.get("consecutive_errors") or 0) + 1})
        return Outcome(key, name, action, record=base, error=str(exc))
    # Page read fine (or robots.txt says no) but it holds nothing we can read. Roles that came from an earlier
    # source count as gone: an empty board, so plan_sync holds them one run and then closes them.
    base.update({"ats": "custom", "ats_slug": None, "verified": True, "consecutive_errors": 0,
                 "status": "ok" if care.get("ats") == "custom" and care.get("status") == "ok" else "unsupported"})
    return Outcome(key, name, action, record=base, matches=[], board_total=0, verified=True)


def _board_missing(key: str, name: str, base: dict, care: dict) -> Outcome:
    """A board we knew about now returns 404. Count it as an error; jobs stay untouched."""
    errors = int(care.get("consecutive_errors") or 0) + 1
    base.update({"status": "error", "consecutive_errors": errors})
    if care.get("source") != "manual" and errors >= 3:
        # Gone for good: forget the guess so discovery can look again.
        base.update({"status": "no_board", "ats": None, "ats_slug": None, "careers_url": None,
                     "verified": False, "consecutive_errors": 0})
    return Outcome(key, name, "refresh", record=base, error="board not found (404)")


# ======================================================================
# Orchestration
# ======================================================================
def standalone_companies(deal_companies: list[dict], careers: dict[str, dict]) -> list[dict]:
    """Companies the admin added on the Jobs page: a manual careers row whose company is not in the blotter."""
    in_deals = {c["company_key"] for c in deal_companies}
    return [{"company_key": key, "company": row.get("company") or key}
            for key, row in careers.items()
            if row.get("source") == "manual" and key not in in_deals]


def _stale(care: dict, retry_days: int, today_dt: datetime) -> bool:
    """Not checked for `retry_days` days (or never)."""
    last = care.get("last_checked")
    try:
        age = today_dt - datetime.fromisoformat(last.replace("Z", "+00:00")) if last else None
    except ValueError:
        age = None
    return age is None or age >= timedelta(days=retry_days)


def due_items(companies, careers, retry_days, only_key, discover_only, today_dt) -> list[dict]:
    """Which companies to process this run, with needs_refresh first."""
    items = []
    for c in companies:
        care = careers.get(c["company_key"])
        if only_key:
            if c["company_key"] == only_key:
                items.append({**c, "care": care})
            continue
        has_board = bool(care and care.get("ats") in FETCHERS and care.get("ats_slug"))
        if care is None or care.get("needs_refresh"):
            due = True
        elif has_board:
            due = not discover_only
        elif care.get("source") == "manual":
            # The admin's own page (not a job board). Daily while we can read roles from it (or it errored),
            # otherwise just now and then, in case it starts embedding a job board.
            due = (not discover_only) if care.get("status") in ("ok", "error") else _stale(care, retry_days, today_dt)
        elif care.get("status") in ("error", "pending"):
            due = True                       # lookup failed or never finished: try again
        elif care.get("status") == "no_board":
            due = _stale(care, retry_days, today_dt)
        else:
            due = False
        if due:
            items.append({**c, "care": care})
    items.sort(key=lambda i: 0 if (i["care"] or {}).get("needs_refresh") else 1)
    return items


def _load(fn, empty, dry_run: bool):
    """Read a careers table. In a dry run a missing table (migration not applied yet) counts as empty."""
    try:
        return fn()
    except Exception as exc:                     # postgrest APIError: table not found
        text = str(exc)
        if dry_run and ("PGRST205" in text or "Could not find the table" in text or "does not exist" in text):
            log.warning("careers: tables not found (migration 002 not applied yet); dry run treats them as empty")
            return empty
        raise


def run(store, cfg: dict, *, dry_run: bool, discover_only: bool, only_key: str | None,
        limit: int | None, report_path: str | None) -> int:
    jobs_cfg = cfg["jobs"]
    configure(jobs_cfg)
    matcher = RoleMatcher(jobs_cfg)
    today_dt = datetime.now(timezone.utc)
    today = today_dt.date()

    careers = _load(store.careers_all, {}, dry_run)
    deal_cos = store.deal_companies()
    companies = deal_cos + standalone_companies(deal_cos, careers)
    jobs_by_company = _load(store.jobs_all, {}, dry_run)
    items = due_items(companies, careers, jobs_cfg["retry_no_board_days"], only_key, discover_only, today_dt)
    if limit:
        items = items[:limit]
    log.info("careers: %d companies in deals, %d to process%s", len(companies), len(items),
             " (DRY RUN, nothing is written)" if dry_run else "")

    with ThreadPoolExecutor(max_workers=jobs_cfg["workers"]) as pool:
        outcomes = list(pool.map(lambda it: process_company(it, jobs_cfg, matcher), items))

    rows, fetch_attempts, fetch_errors, fetch_limited = [], 0, 0, 0
    totals = {"inserted": 0, "reopened": 0, "closed": 0, "held": 0}
    for o in outcomes:
        if o.action == "skip":
            if o.record and not dry_run:
                store.careers_upsert(o.record)      # just clears the needs_refresh flag
            continue
        if o.record is None:
            continue
        fetch_attempts += 1
        row = {"company": o.company, "company_key": o.company_key, "action": o.action,
               "status": o.record.get("status"), "ats": o.record.get("ats") or "",
               "slug": o.record.get("ats_slug") or "", "verified": o.verified if o.matches is not None else "",
               "board_postings": o.board_total if o.board_total is not None else "",
               "matching": "", "new": 0, "closed": 0, "titles": "", "error": o.error or ""}
        if o.error:
            fetch_errors += 1
        if o.limited:
            fetch_limited += 1
            row["error"] = "rate limited (will retry next run)"
        if o.matches is not None:
            plan = plan_sync(o.company_key, jobs_by_company.get(o.company_key, []), o.matches,
                             o.board_total or 0, int(o.record.get("empty_streak") or 0), today)
            o.record["empty_streak"] = plan.new_empty_streak
            row.update(matching=len(o.matches), new=plan.inserted + plan.reopened,
                       closed=len(plan.close_ids), titles="; ".join(m["title"] for m in o.matches[:4]))
            totals["inserted"] += plan.inserted
            totals["reopened"] += plan.reopened
            totals["closed"] += len(plan.close_ids)
            totals["held"] += int(plan.held)
            if not dry_run:
                store.careers_upsert(o.record)           # first: jobs reference this row
                if plan.upserts:
                    store.jobs_upsert(plan.upserts)
                if plan.close_ids:
                    store.jobs_close(plan.close_ids, today.isoformat())
        elif not dry_run:
            store.careers_upsert(o.record)
        rows.append(row)

    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    log.info("careers: processed %d | by status %s | %d rate limited (retried next run) | "
             "jobs +%d new, %d reopened, %d closed, %d held",
             len(rows), by_status, fetch_limited, totals["inserted"], totals["reopened"],
             totals["closed"], totals["held"])
    for r in rows:
        if r["matching"]:
            log.info("  %-28s %-10s %s  %s", r["company"][:27], r["ats"], r["matching"], r["titles"][:90])
    if report_path and rows:
        with open(report_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        log.info("careers: report written to %s", report_path)

    checked = fetch_attempts - fetch_limited          # throttled lookups are not failures
    rate = fetch_errors / checked if checked > 0 else 0.0
    if rate > jobs_cfg["max_error_rate"]:
        log.error("careers: %d of %d fetches failed (%.0f%%), above the %.0f%% limit",
                  fetch_errors, checked, rate * 100, jobs_cfg["max_error_rate"] * 100)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    from .main import setup_logging
    from .supabase_store import SupabaseStore

    ap = argparse.ArgumentParser(description="Find company job boards and sync matching open roles")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    ap.add_argument("--discover-only", action="store_true", help="only look for boards, do not refresh known ones")
    ap.add_argument("--company-key", help="process just this company")
    ap.add_argument("--limit", type=int, help="process at most N companies")
    ap.add_argument("--report", metavar="CSV", help="write a per-company report to this CSV")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    load_dotenv(ROOT / ".env")
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    store = SupabaseStore.from_env()
    return run(store, cfg, dry_run=args.dry_run, discover_only=args.discover_only,
               only_key=args.company_key, limit=args.limit, report_path=args.report)


if __name__ == "__main__":
    sys.exit(main())
