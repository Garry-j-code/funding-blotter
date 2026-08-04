"""Sector enrichment — search tool + LLM classifier.

For each extracted company the LLM is given a `web_search` tool. It calls the
tool to look up what the company actually does; we execute that search via
Tavily (web search API), feed the snippets back, and the model returns a
sector label. Only fintech, financial services, and clear finance enablers
are kept. Everything else is dropped.

Labels:
  ai_fintech         - AI-native product whose primary use case is finance / fintech
  fintech            - builds financial technology products
  financial_services - is itself a bank / broker / asset manager / insurer / ...
  enabler            - primarily sells to or enables financial institutions
  unrelated          - none of the above (dropped)

Classifications are cached per company in the DB (company_sector table) so a
daily run never re-searches or re-spends on a company it has already seen.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from datetime import date

import requests

from .extract import GroqExtractor
from .store import company_key

log = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
VALID_LABELS = {"ai_fintech", "fintech", "financial_services", "enabler", "unrelated"}

# Groq tool schema — the model calls this; we execute it.
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for what a company does. Use this before classifying "
            "any company whose business is not already obvious from the article."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query, e.g. 'Dili AI compliance company NYC'",
                }
            },
            "required": ["query"],
        },
    },
}

CLASSIFY_SYSTEM = """You classify companies by their relationship to the \
financial sector. You have a web_search tool — call it to learn what the \
company actually does before deciding.

Labels (choose exactly one — prefer the most specific):
  ai_fintech          - AI is core to the product AND the primary use case is \
finance/fintech (e.g. AI trading, AI lending underwriting, AI-native banking \
compliance, agentic finance workflows). Prefer this over fintech/enabler when \
both AI and finance are central.
  fintech             - builds financial technology products (payments, \
lending, trading, wealth, banking software, crypto, insurtech, capital markets) \
where AI is not the defining feature.
  financial_services  - is itself a financial institution or service: bank, \
broker-dealer, exchange, asset/investment manager, insurer, lender, fund.
  enabler             - NOT itself finance, but PRIMARILY sells to or enables \
financial institutions: e.g. KYC/AML for banks, payments compliance, trading \
risk, market data for brokers. Use this (not ai_fintech) when the product is \
general infra that happens to serve banks.
  unrelated           - none of the above (biotech, consumer goods, space, \
generic staffing, restaurants, generic security/devtools, general enterprise \
AI/data platforms with no clear finance focus).

Be strict: serving "Fortune 500" or "enterprises including banks" is not enough \
— the primary customer must be financial. Incidental finance mentions do not \
count. After you have enough \
information, reply with JSON only (no markdown fences):
{"label": "<label>", "reason": "<under 15 words>"}"""


class TavilySearch:
    """Executes the web_search tool via Tavily."""

    def __init__(self, cfg: dict):
        self.api_key = os.environ.get("TAVILY_API_KEY", "").strip()
        self.max_results = int(cfg.get("max_results", 3))

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str) -> str:
        try:
            resp = requests.post(
                TAVILY_URL,
                json={
                    "api_key": self.api_key,
                    "query": query,
                    "max_results": self.max_results,
                    "search_depth": "basic",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("web_search failed for %r: %s", query[:60], exc)
            return f"Search failed: {exc}"
        parts = [
            f"{r.get('title', '')}: {r.get('content', '')}"
            for r in data.get("results", [])
        ]
        blob = " ".join(parts).strip()
        return blob[:1200] if blob else "No results found."


def _parse_label(content: str) -> dict | None:
    content = re.sub(r"^```(?:json)?|```$", "", (content or "").strip()).strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # Model sometimes wraps or trails prose; pull the first JSON object.
        m = re.search(r"\{[^{}]+\}", content, re.DOTALL)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    label = str(parsed.get("label", "") or "").strip().lower()
    if label not in VALID_LABELS:
        return None
    return {"label": label, "reason": str(parsed.get("reason", "") or "").strip()[:140]}


def _classify_one(
    extractor: GroqExtractor,
    searcher: TavilySearch,
    company: str,
    location: str,
    description: str,
    max_tool_rounds: int = 2,
) -> dict | None:
    """One company: LLM may call web_search, then returns a label JSON."""
    user = (
        f'Company: {company}\n'
        f'Location: {location or "unknown"}\n'
        f'Article description: {description or "(none)"}\n\n'
        f'Use web_search if needed, then classify.'
    )
    messages: list[dict] = [
        {"role": "system", "content": CLASSIFY_SYSTEM},
        {"role": "user", "content": user},
    ]

    for _ in range(max_tool_rounds + 1):
        body = {
            "model": extractor.model,
            "temperature": 0,
            "max_tokens": 400,
            "tools": [WEB_SEARCH_TOOL],
            "tool_choice": "auto",
            "messages": messages,
        }
        data = extractor._post(body)
        extractor.calls += 1
        if not data:
            return None

        choice = data["choices"][0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []

        if tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content") or None,
                    "tool_calls": tool_calls,
                }
            )
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "web_search":
                    query = str(args.get("query") or f"{company} company what does it do")
                    log.debug("tool web_search(%r)", query)
                    result = searcher.search(query)
                else:
                    result = f"Unknown tool: {name}"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", "call_0"),
                        "name": name,
                        "content": result,
                    }
                )
            continue

        # Final answer — force JSON if the model replied in prose.
        content = msg.get("content") or ""
        parsed = _parse_label(content)
        if parsed:
            return parsed

        # One more nudge without tools if the model forgot the JSON shape.
        messages.append({"role": "assistant", "content": content})
        messages.append(
            {
                "role": "user",
                "content": 'Reply with JSON only: {"label": "...", "reason": "..."}',
            }
        )
        body = {
            "model": extractor.model,
            "temperature": 0,
            "max_tokens": 200,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        data = extractor._post(body)
        extractor.calls += 1
        if not data:
            return None
        content = data["choices"][0]["message"].get("content") or ""
        return _parse_label(content)

    return None


def _cache_get(conn: sqlite3.Connection, key: str) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT label, reason FROM company_sector WHERE company_key = ?", (key,)
    ).fetchone()
    return (row["label"], row["reason"]) if row else None


def _cache_put(conn: sqlite3.Connection, key: str, label: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO company_sector (company_key, label, reason, enriched_at) "
        "VALUES (?,?,?,?) ON CONFLICT(company_key) DO UPDATE SET "
        "label=excluded.label, reason=excluded.reason, enriched_at=excluded.enriched_at",
        (key, label, reason, date.today().isoformat()),
    )
    conn.commit()


def _classify_companies(
    companies: list[tuple[str, str, str, str]],
    extractor: GroqExtractor,
    searcher: TavilySearch,
    conn: sqlite3.Connection,
    delay: float,
) -> dict[str, tuple[str, str]]:
    """companies: [(company_key, name, location, description)] -> {key: (label, reason)}."""
    out: dict[str, tuple[str, str]] = {}
    for i, (key, name, location, description) in enumerate(companies):
        cached = _cache_get(conn, key)
        if cached:
            out[key] = cached
            continue
        log.info("enrich: tool-calling web_search for %s", name)
        res = _classify_one(extractor, searcher, name, location, description)
        if res is None:
            log.warning("enrich: no classification for %s; marking unknown", name)
            out[key] = ("unknown", "")
        else:
            out[key] = (res["label"], res["reason"])
            _cache_put(conn, key, res["label"], res["reason"])
            log.info("enrich: %s -> %s (%s)", name, res["label"], res["reason"])
        if delay and i < len(companies) - 1:
            time.sleep(delay)
    return out


def enrich_all(deals: list[dict], cfg: dict, conn: sqlite3.Connection) -> list[dict]:
    """Classify each deal via web_search tool; drop unrelated companies.

    Fails safe: if search/LLM is unavailable, deals are returned unfiltered.
    """
    ecfg = cfg.get("enrichment", {})
    if not ecfg.get("enabled", True):
        return deals

    searcher = TavilySearch(ecfg)
    if not searcher.available:
        log.warning("TAVILY_API_KEY not set; skipping sector enrichment (keeping all deals).")
        return deals

    extractor = GroqExtractor(cfg["extraction"])
    if not extractor.available:
        log.warning("GROQ_API_KEY not set; cannot classify sectors (keeping all deals).")
        return deals

    keep_labels = set(
        ecfg.get("keep_labels", ["ai_fintech", "fintech", "financial_services", "enabler"])
    )
    delay = float(ecfg.get("seconds_between_searches", 1.0))

    # Deduplicate by company_key so we only tool-call once per company.
    seen: set[str] = set()
    to_classify: list[tuple[str, str, str, str]] = []
    for d in deals:
        key = company_key(d["company"])
        if key in seen:
            continue
        seen.add(key)
        to_classify.append(
            (key, d["company"], d.get("location", "") or "", d.get("description", "") or "")
        )

    labels = _classify_companies(to_classify, extractor, searcher, conn, delay)

    for d in deals:
        key = company_key(d["company"])
        label, reason = labels.get(key, ("unknown", ""))
        d["sector_label"] = label
        d["sector_reason"] = reason

    kept = [
        d for d in deals
        if d.get("sector_label") in keep_labels or d.get("sector_label") == "unknown"
    ]
    dropped = [d for d in deals if d not in kept]
    if dropped:
        names = ", ".join(d["company"] for d in dropped)
        log.info("enrich: dropped %d unrelated -> %s", len(dropped), names)
    log.info("enrich: %d in-sector rounds kept of %d", len(kept), len(deals))
    return kept


def purge_unrelated(conn: sqlite3.Connection, cfg: dict) -> int:
    """Reclassify stored deals missing a sector label and delete unrelated ones.

    Keeps the blotter honest after the sector filter was introduced: old rows
    from before enrichment would otherwise linger forever.
    """
    ecfg = cfg.get("enrichment", {})
    if not ecfg.get("enabled", True):
        return 0

    searcher = TavilySearch(ecfg)
    extractor = GroqExtractor(cfg["extraction"])
    if not searcher.available or not extractor.available:
        return 0

    keep_labels = set(
        ecfg.get("keep_labels", ["ai_fintech", "fintech", "financial_services", "enabler"])
    )
    delay = float(ecfg.get("seconds_between_searches", 1.0))

    rows = conn.execute(
        "SELECT DISTINCT company_key, company, location, description, "
        "COALESCE(sector_label, '') AS sector_label FROM deals"
    ).fetchall()

    need: list[tuple[str, str, str, str]] = []
    for r in rows:
        cached = _cache_get(conn, r["company_key"])
        if cached:
            continue
        if r["sector_label"] in VALID_LABELS:
            _cache_put(conn, r["company_key"], r["sector_label"], "")
            continue
        need.append(
            (r["company_key"], r["company"], r["location"] or "", r["description"] or "")
        )

    if need:
        log.info("enrich: classifying %d stored companies for purge", len(need))
        _classify_companies(need, extractor, searcher, conn, delay)

    # Stamp deal rows from cache, then delete unrelated.
    cache_rows = conn.execute("SELECT company_key, label, reason FROM company_sector").fetchall()
    for r in cache_rows:
        conn.execute(
            "UPDATE deals SET sector_label = ?, sector_reason = ? WHERE company_key = ?",
            (r["label"], r["reason"], r["company_key"]),
        )

    placeholders = ",".join("?" for _ in keep_labels)
    # unknown / empty: leave them (fail-safe). Only drop explicit unrelated.
    cur = conn.execute(
        f"DELETE FROM deals WHERE sector_label = 'unrelated' "
        f"OR (sector_label != '' AND sector_label NOT IN ({placeholders}) "
        f"AND sector_label != 'unknown')",
        tuple(keep_labels),
    )
    deleted = cur.rowcount
    conn.commit()
    if deleted:
        log.info("enrich: purged %d unrelated deals from DB", deleted)
    return deleted
