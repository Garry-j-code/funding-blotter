"""Turn free text into structured deal records.

Two paths:
  1. Regex. FinSMEs writes to a fixed template, so most of its entries parse
     locally for free. This matters because Groq's free tier is capped on
     tokens per minute, not requests.
  2. Groq. Everything the regex misses goes to the model in small batches.

Deal = {company, amount_raw, amount_usd, stage, location, description,
        investors, is_funding}
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger(__name__)

# "Obin AI, a NYC-based enterprise AI company building an agentic workforce for
#  financial institutions, raised $7M in Seed funding."
FINSMES_RE = re.compile(
    r"^(?P<company>.{2,80}?),\s+an?\s+(?P<location>[^,]{2,40}?)"
    r"(?:,\s*(?P<region>[A-Za-z .]{2,30}))?-based\s+"
    r"(?P<description>.{5,300}?),\s+"
    r"(?:raised|closed|secured|has raised|announced)\s+"
    r"(?P<amount>an undisclosed amount|over\s+[^\s]+|[^\s]+)\s+"
    r"in\s+(?P<stage>.{2,60}?)\s+(?:funding|financing|investment|round)\b",
    re.IGNORECASE | re.DOTALL,
)

LED_BY_RE = re.compile(
    r"(?:round|financing|investment)\s+was\s+(?:led|co-led)\s+by\s+(?P<investors>.{2,220})",
    re.IGNORECASE,
)

# Investor lists run on forever. Cut at the first clause that stops naming leads.
INV_TAIL_RE = re.compile(
    r",?\s+(?:with\s+participation|with\s+existing|and\s+existing|alongside|"
    r"plus\s|joined\s+by|with\s+support|and\s+included|with\s+additional)",
    re.IGNORECASE,
)


def clean_investors(raw: str) -> str:
    if not raw:
        return ""
    s = INV_TAIL_RE.split(raw)[0]
    s = re.split(r"(?<=[a-z])\.\s", s)[0]
    return s.strip(" .,;:").strip()[:160]

AMOUNT_RE = re.compile(
    r"(?P<cur>[$€£])\s?(?P<num>\d+(?:[.,]\d+)?)\s*(?P<mult>[KMB]|million|billion|thousand)?",
    re.IGNORECASE,
)

MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}
# Rough conversion so the amount column sorts sensibly across currencies.
FX = {"$": 1.0, "€": 1.08, "£": 1.27}

STAGE_CANON = [
    ("pre-seed", "Pre-Seed"), ("preseed", "Pre-Seed"), ("seed", "Seed"),
    ("series a", "Series A"), ("series b", "Series B"), ("series c", "Series C"),
    ("series d", "Series D"), ("series e", "Series E"), ("series f", "Series F"),
    ("growth", "Growth"), ("debt", "Debt"), ("venture debt", "Debt"),
    ("bridge", "Bridge"), ("strategic", "Strategic"), ("grant", "Grant"),
]

NON_FUNDING = re.compile(
    r"\b(acquire[sd]?|acquisition|merges?|appoints?|hires?|launches?|"
    r"partners? with|names? \w+ as|closes? (?:its )?fund|raises? \$?\d+[bm]? fund)\b",
    re.IGNORECASE,
)


def parse_amount(raw: str) -> float | None:
    """Return a USD-ish float, or None for undisclosed rounds."""
    if not raw or "undisclos" in raw.lower():
        return None
    m = AMOUNT_RE.search(raw)
    if not m:
        return None
    try:
        num = float(m.group("num").replace(",", ""))
    except ValueError:
        return None
    mult = MULT.get((m.group("mult") or "").lower(), 1.0)
    return round(num * mult * FX.get(m.group("cur"), 1.0), 2)


def canon_stage(raw: str) -> str:
    low = (raw or "").lower()
    for needle, label in STAGE_CANON:
        if needle in low:
            return label
    return (raw or "Undisclosed").strip().title()[:24]


# A headline like "Obin AI Raises $7M in Seed Funding" also matches the
# template, so a company name containing these is a sign we grabbed the title.
BAD_NAME_RE = re.compile(
    r"\b(raise[sd]?|raising|secure[sd]?|close[sd]?|funding|round|million|"
    r"seed|series)\b|[$€£]",
    re.IGNORECASE,
)


def _sentences(text: str) -> list[str]:
    """Split on sentence boundaries. Requires whitespace + capital after the
    period, so names like 'Transient.AI' survive intact."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\u00c0-\u017f])", text) if s.strip()]


def regex_extract(item: dict) -> dict | None:
    """Parse a FinSMEs-style entry locally. Returns None if it doesn't match."""
    if NON_FUNDING.search(item.get("title", "")):
        return None

    body = item.get("text", "") or ""
    match = None

    # Match the body's own sentences first. Folding the headline in is a last
    # resort, because the headline follows the same template and wins the
    # anchored match with a garbage company name.
    for blob in (body, f"{item.get('title', '')}. {body}"):
        for sent in _sentences(blob):
            m = FINSMES_RE.match(sent)
            if m and not BAD_NAME_RE.search(m.group("company")):
                match = m
                break
        if match:
            break

    if not match:
        return None

    loc = match.group("location").strip()
    if match.group("region"):
        loc = f"{loc}, {match.group('region').strip()}"

    inv = LED_BY_RE.search(body)

    return {
        "company": match.group("company").strip(" .,"),
        "amount_raw": match.group("amount").strip(),
        "amount_usd": parse_amount(match.group("amount")),
        "stage": canon_stage(match.group("stage")),
        "location": loc[:60],
        "description": re.sub(r"\s+", " ", match.group("description")).strip()[:280],
        "investors": clean_investors(inv.group("investors") if inv else ""),
        "is_funding": True,
        "extracted_by": "regex",
    }


SYSTEM_PROMPT = """You extract funding-round facts from news snippets. \
Reply with JSON only, no prose and no markdown fences.

Return: {"deals": [...]} — a JSON array with one object per input item, in the \
same order. Put the input "id" as a field inside each object. Do NOT use the id \
as an object key.

Each object:
  id           - the input id, unchanged
  is_funding   - true only if a company RAISED capital. False for acquisitions, \
M&A, hires, product launches, or a VC firm closing its own fund.
  company      - the company that raised. No suffixes like Inc or Ltd.
  amount_raw   - amount exactly as written, e.g. "$7M", "€33M", "undisclosed"
  stage        - one of: Pre-Seed, Seed, Series A, Series B, Series C, Series D, \
Series E, Series F, Growth, Debt, Bridge, Strategic, Grant, Undisclosed
  location     - headquarters, e.g. "New York, NY" or "London, UK"
  description  - what the company does, under 25 words
  investors    - lead investors, comma separated. "" if not stated.

Use "" for any string you cannot determine. Never invent an amount."""


class GroqExtractor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.api_key = os.environ.get("GROQ_API_KEY", "").strip()
        self.url = cfg["base_url"].rstrip("/") + "/chat/completions"
        self.model = cfg["model"]
        self.calls = 0

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _post(self, payload: dict) -> dict | None:
        for attempt in range(self.cfg["max_retries"]):
            try:
                resp = requests.post(
                    self.url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=90,
                )
            except requests.RequestException as exc:
                log.warning("groq: network error (%s), retrying", exc)
                time.sleep(2 ** attempt * 3)
                continue

            if resp.status_code == 429:
                # Groq tells you how long to wait. Believe it.
                wait = float(resp.headers.get("retry-after", 2 ** attempt * 5))
                log.info("groq: rate limited, sleeping %.1fs", wait)
                time.sleep(min(wait + 1, 90))
                continue

            if resp.status_code >= 500:
                time.sleep(2 ** attempt * 3)
                continue

            if not resp.ok:
                log.error("groq: %s %s", resp.status_code, resp.text[:300])
                return None

            return resp.json()

        log.error("groq: giving up after %d attempts", self.cfg["max_retries"])
        return None

    def extract_batch(self, items: list[dict]) -> dict[int, dict]:
        """items: [{"id": int, "text": str}]. Returns {id: deal}."""
        trunc = self.cfg["chars_per_item"]
        payload_items = [
            {"id": it["id"], "text": it["text"][:trunc]} for it in items
        ]

        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 2400,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload_items, ensure_ascii=False)},
            ],
        }

        data = self._post(body)
        self.calls += 1
        if not data:
            return {}

        try:
            content = data["choices"][0]["message"]["content"]
            content = re.sub(r"^```(?:json)?|```$", "", content.strip()).strip()
            parsed = json.loads(content)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            log.error("groq: unparseable response: %s", exc)
            return {}

        # Models are inconsistent about the JSON shape they return. Accept any:
        #   {"deals": [ {"id": 2, ...}, ... ]}   (documented shape)
        #   {"deals": { "2": {...}, ... }}        (id-keyed under "deals")
        #   { "2": {...}, ... }                    (id-keyed, no wrapper)
        payload = parsed.get("deals", parsed) if isinstance(parsed, dict) else parsed
        records: list[dict] = []
        if isinstance(payload, list):
            records = [d for d in payload if isinstance(d, dict)]
        elif isinstance(payload, dict):
            for key, val in payload.items():
                if isinstance(val, dict):
                    val = dict(val)
                    val.setdefault("id", key)
                    records.append(val)

        out: dict[int, dict] = {}
        for deal in records:
            try:
                did = int(deal["id"])
            except (KeyError, TypeError, ValueError):
                continue
            amount_raw = str(deal.get("amount_raw", "") or "")
            out[did] = {
                "company": str(deal.get("company", "") or "").strip()[:90],
                "amount_raw": amount_raw.strip()[:40],
                "amount_usd": parse_amount(amount_raw),
                "stage": canon_stage(str(deal.get("stage", "") or "")),
                "location": str(deal.get("location", "") or "").strip()[:60],
                "description": str(deal.get("description", "") or "").strip()[:280],
                "investors": clean_investors(str(deal.get("investors", "") or "")),
                "is_funding": bool(deal.get("is_funding", False)),
                "extracted_by": self.model,
            }
        return out


def extract_all(items: list[dict], cfg: dict) -> list[dict]:
    """Run every raw item through regex and/or Groq. Returns enriched items."""
    extractor = GroqExtractor(cfg)
    if not extractor.available:
        log.warning("GROQ_API_KEY not set. Falling back to regex only.")

    pending: list[dict] = []
    results: list[dict] = []

    for idx, item in enumerate(items):
        item = dict(item)
        item["_idx"] = idx

        if cfg.get("regex_fastpath", True) and item.get("template_hint") == "finsmes":
            deal = regex_extract(item)
            if deal:
                results.append({**item, **deal})
                continue

        if extractor.available:
            body = item["text"] or item["title"]
            pending.append({"id": idx, "text": f"{item['title']}. {body}", "_item": item})
        else:
            deal = regex_extract(item)
            if deal:
                results.append({**item, **deal})

    if pending:
        size = cfg["batch_size"]
        batches = [pending[i:i + size] for i in range(0, len(pending), size)]
        log.info("groq: %d items in %d batches", len(pending), len(batches))

        for n, batch in enumerate(batches, 1):
            got = extractor.extract_batch(batch)
            for entry in batch:
                deal = got.get(entry["id"])
                if deal and deal.get("company"):
                    results.append({**entry["_item"], **deal})
            log.info("groq: batch %d/%d -> %d parsed", n, len(batches), len(got))
            if n < len(batches):
                time.sleep(cfg["seconds_between_calls"])

    kept = [r for r in results if r.get("is_funding") and r.get("company")]
    log.info("extraction: %d raw -> %d funding rounds", len(items), len(kept))
    return kept
