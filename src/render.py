"""Render the deal set as a single self-contained HTML page.

Design: a trade blotter. Amounts in tabular monospace figures so the column
scans vertically, a left gutter rail that fills in red ink for rounds matching
both your sector and location filters, and "n/d" rather than a blank cell for
undisclosed rounds so they stay visible instead of disappearing.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

CSS = """
:root {
  --ledger:  #ECEEE8;
  --card:    #F6F7F3;
  --ink:     #171A16;
  --rule:    #CBCFC5;
  --muted:   #6B7267;
  --credit:  #0C5A3A;
  --flag:    #A8380F;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--ledger); color: var(--ink);
  font-family: Newsreader, Georgia, serif;
  font-size: 16px; line-height: 1.45;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 0 24px 96px; }

/* Masthead */
.mast { padding: 40px 0 0; }
.mast h1 {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 13px; font-weight: 600; letter-spacing: 0.22em;
  text-transform: uppercase; margin: 0 0 6px;
}
.mast .sub {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px; color: var(--muted); letter-spacing: 0.06em;
}
.mast .sub b { color: var(--ink); font-weight: 500; }
.rule { height: 1px; background: var(--ink); margin: 18px 0 0; }
.rule.thin { background: var(--rule); }

/* Controls */
.controls {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  padding: 14px 0; border-bottom: 1px solid var(--rule);
  position: sticky; top: 0; background: var(--ledger); z-index: 5;
}
.chip {
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  letter-spacing: 0.08em; text-transform: uppercase;
  padding: 5px 11px; border: 1px solid var(--rule); border-radius: 0;
  background: transparent; color: var(--muted); cursor: pointer;
}
.chip:hover { border-color: var(--ink); color: var(--ink); }
.chip[aria-pressed="true"] {
  background: var(--ink); border-color: var(--ink); color: var(--ledger);
}
.chip:focus-visible, #q:focus-visible, .fetch-btn:focus-visible, .token-btn:focus-visible {
  outline: 2px solid var(--flag); outline-offset: 2px;
}
.fetch-btn {
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  letter-spacing: 0.08em; text-transform: uppercase;
  padding: 5px 11px; border: 1px solid var(--ink); border-radius: 0;
  background: var(--ink); color: var(--ledger); cursor: pointer;
  margin-left: auto;
}
.fetch-btn:hover { opacity: 0.88; }
.fetch-btn:disabled { opacity: 0.45; cursor: wait; }
.token-btn {
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  letter-spacing: 0.06em; text-transform: uppercase;
  padding: 5px 9px; border: 1px solid var(--rule); background: transparent;
  color: var(--muted); cursor: pointer;
}
.token-btn:hover { border-color: var(--ink); color: var(--ink); }
#fetch-status {
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  color: var(--muted); width: 100%; margin-top: 2px; min-height: 1.2em;
}
#fetch-status.ok { color: var(--credit); }
#fetch-status.err { color: var(--flag); }
#q {
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  padding: 5px 9px; border: 1px solid var(--rule); background: var(--card);
  color: var(--ink); min-width: 190px;
}
#q::placeholder { color: var(--muted); }

/* Date groups */
.daybar {
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  letter-spacing: 0.16em; text-transform: uppercase; color: var(--muted);
  padding: 26px 0 8px; display: flex; justify-content: space-between;
}

/* Deal row: the gutter rail is the signature element */
.deal {
  display: grid;
  grid-template-columns: 3px 1fr 108px 92px;
  gap: 0 18px; align-items: start;
  padding: 13px 0; border-bottom: 1px solid var(--rule);
}
.gutter { background: var(--rule); align-self: stretch; }
.deal.pri .gutter { background: var(--flag); }
.deal.dim { opacity: 0.42; }
.deal:hover { background: var(--card); }

.name { font-size: 19px; font-weight: 600; line-height: 1.2; letter-spacing: -0.01em; }
.name a { color: inherit; text-decoration: none; border-bottom: 1px solid var(--rule); }
.name a:hover { border-bottom-color: var(--ink); }
.desc { color: var(--muted); font-size: 14.5px; margin-top: 3px; }
.meta {
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  color: var(--muted); margin-top: 6px; letter-spacing: 0.03em;
}
.meta .inv { color: var(--ink); }
.meta .sector {
  color: var(--credit); border: 1px solid var(--rule); border-radius: 2px;
  padding: 1px 6px; letter-spacing: 0.05em; white-space: nowrap;
}
.meta .src {
  color: var(--muted); letter-spacing: 0.06em;
  text-decoration: none; border-bottom: 1px solid var(--rule);
}
a.src:hover { color: var(--ink); border-bottom-color: var(--ink); }
a.src:focus-visible { outline: 2px solid var(--flag); outline-offset: 2px; }

.amt {
  font-family: 'IBM Plex Mono', monospace; font-variant-numeric: tabular-nums;
  font-size: 16px; font-weight: 500; text-align: right; color: var(--credit);
  padding-top: 2px;
}
.amt.nd { color: var(--muted); font-weight: 400; }
.stage {
  font-family: 'IBM Plex Mono', monospace; font-size: 10.5px;
  letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted);
  text-align: right; padding-top: 6px;
}
.deal.pri .stage { color: var(--flag); }

.empty {
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  color: var(--muted); padding: 56px 0; text-align: center;
}
.foot {
  font-family: 'IBM Plex Mono', monospace; font-size: 10.5px;
  color: var(--muted); padding-top: 28px; letter-spacing: 0.05em;
}

@media (max-width: 720px) {
  .wrap { padding: 0 16px 64px; }
  .deal { grid-template-columns: 3px 1fr; gap: 0 14px; }
  .amt, .stage { text-align: left; padding: 6px 0 0; }
  .amt { font-size: 15px; }
  .stage { grid-column: 2; }
  #q, .fetch-btn { margin-left: 0; width: 100%; }
}

@media (prefers-reduced-motion: no-preference) {
  .deal { animation: in 0.32s ease both; }
  @keyframes in { from { opacity: 0; transform: translateY(4px); } }
}
"""

JS = """
const DATA = __DATA__;
const state = { mode: 'all', q: '' };
const feed = document.getElementById('feed');
const count = document.getElementById('count');

const trim = s => s.replace(/(\\.\\d*?)0+$/, '$1').replace(/\\.$/, '');
const fmt = (usd, raw) => {
  if (usd == null) return raw && /undisclos/i.test(raw) ? 'n/d' : (raw || 'n/d');
  if (usd >= 1e9) return '$' + trim((usd / 1e9).toFixed(2)) + 'B';
  if (usd >= 1e6) return '$' + trim((usd / 1e6).toFixed(1)) + 'M';
  return '$' + Math.round(usd / 1e3) + 'K';
};

const dayLabel = iso => {
  const d = new Date(iso + 'T12:00:00Z');
  return d.toLocaleDateString('en-US',
    { day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' }).toUpperCase();
};

const esc = s => String(s ?? '').replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const SRC = { finsmes: 'FinSMEs', fintech_global: 'fintech.global', aifunding_me: 'aifunding.me' };
const srcLabel = s => SRC[s] || s;

const SECTOR = {
  ai_fintech: 'AI Fintech',
  fintech: 'Fintech',
  financial_services: 'Financial Services',
  enabler: 'Fintech enabler',
};
const sectorLabel = s => SECTOR[s] || '';

function visible() {
  const q = state.q.toLowerCase();
  return DATA.filter(d => {
    if (state.mode === 'priority' && !d.priority) return false;
    if (state.mode === 'disclosed' && d.amount_usd == null) return false;
    if (state.mode === 'early' && !/seed|series a/i.test(d.stage || '')) return false;
    if (!q) return true;
    return [d.company, d.description, d.investors, d.location, d.stage]
      .join(' ').toLowerCase().includes(q);
  });
}

function render() {
  const rows = visible();
  count.textContent = rows.length + (rows.length === 1 ? ' round' : ' rounds');

  if (!rows.length) {
    feed.innerHTML = '<div class="empty">No rounds match. Widen the filter or clear the search.</div>';
    return;
  }

  const groups = {};
  rows.forEach(d => (groups[d.published_at] = groups[d.published_at] || []).push(d));

  feed.innerHTML = Object.keys(groups).sort().reverse().map(day => {
    const items = groups[day].map((d, i) => {
      const cls = ['deal', d.priority ? 'pri' : '', d.score < 0 ? 'dim' : ''].join(' ');
      const name = d.url
        ? `<a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.company)}</a>`
        : esc(d.company);
      const bits = [];
      if (d.location) bits.push(esc(d.location));
      const sec = sectorLabel(d.sector_label);
      if (sec) bits.push(`<span class="sector" title="${esc(d.sector_reason || '')}">${esc(sec)}</span>`);
      if (d.investors) bits.push(`<span class="inv">${esc(d.investors)}</span>`);
      const srcTxt = 'via ' + esc(srcLabel(d.source));
      bits.push(d.url
        ? `<a class="src" href="${esc(d.url)}" target="_blank" rel="noopener">${srcTxt}</a>`
        : `<span class="src">${srcTxt}</span>`);
      const amt = fmt(d.amount_usd, d.amount_raw);
      return `<div class="${cls}" style="animation-delay:${Math.min(i * 14, 260)}ms">
        <div class="gutter"></div>
        <div>
          <div class="name">${name}</div>
          ${d.description ? `<div class="desc">${esc(d.description)}</div>` : ''}
          <div class="meta">${bits.join(' &nbsp;·&nbsp; ')}</div>
        </div>
        <div class="amt${d.amount_usd == null ? ' nd' : ''}">${esc(amt)}</div>
        <div class="stage">${esc(d.stage || '')}</div>
      </div>`;
    }).join('');

    const flagged = groups[day].filter(d => d.priority).length;
    return `<div class="daybar"><span>${dayLabel(day)}</span>
      <span>${groups[day].length} · ${flagged} flagged</span></div>${items}`;
  }).join('');
}

document.querySelectorAll('.chip').forEach(chip => {
  chip.addEventListener('click', () => {
    state.mode = chip.dataset.mode;
    document.querySelectorAll('.chip').forEach(c =>
      c.setAttribute('aria-pressed', String(c === chip)));
    render();
  });
});

let t;
document.getElementById('q').addEventListener('input', e => {
  clearTimeout(t);
  t = setTimeout(() => { state.q = e.target.value; render(); }, 120);
});

/* --- Fetch today's deals (triggers GitHub Actions on your Mac runner) --- */
const GH = __GITHUB__;
const TOKEN_KEY = 'funding_blotter_gh_pat';
const actionsUrl = `https://github.com/${GH.owner}/${GH.repo}/actions/workflows/${GH.workflow}`;
const statusEl = document.getElementById('fetch-status');
const fetchBtn = document.getElementById('fetch-today');
const tokenBtn = document.getElementById('set-token');

function setStatus(msg, cls) {
  statusEl.textContent = msg || '';
  statusEl.className = cls || '';
}

tokenBtn.addEventListener('click', () => {
  const next = prompt(
    'Paste a GitHub Personal Access Token (classic: workflow scope, or fine-grained: Actions write).\\n' +
    'Stored only in this browser — never in the repo. Cancel to abort. Empty + OK clears the token.'
  );
  if (next === null) return;
  if (!next.trim()) {
    localStorage.removeItem(TOKEN_KEY);
    setStatus('Token cleared.', '');
    return;
  }
  localStorage.setItem(TOKEN_KEY, next.trim());
  setStatus('Token saved in this browser. Click Fetch today\\'s deals.', 'ok');
});

fetchBtn.addEventListener('click', async () => {
  const token = localStorage.getItem(TOKEN_KEY);
  if (!token) {
    setStatus(
      'First click “Set token”, or open Actions and click Run workflow (Mac runner must be online).',
      'err'
    );
    window.open(actionsUrl, '_blank', 'noopener');
    return;
  }
  fetchBtn.disabled = true;
  setStatus('Starting fetch on your Mac runner…', '');
  try {
    const resp = await fetch(
      `https://api.github.com/repos/${GH.owner}/${GH.repo}/actions/workflows/${GH.workflow}/dispatches`,
      {
        method: 'POST',
        headers: {
          'Accept': 'application/vnd.github+json',
          'Authorization': `Bearer ${token}`,
          'X-GitHub-Api-Version': '2022-11-28',
        },
        body: JSON.stringify({
          ref: 'main',
          inputs: { reason: 'blotter-page-button' },
        }),
      }
    );
    if (resp.status === 204) {
      setStatus(
        'Fetch started. Keep your Mac awake. Refresh this page in 2–3 minutes.',
        'ok'
      );
    } else {
      const body = await resp.text();
      setStatus(`GitHub error ${resp.status}: ${body.slice(0, 180) || resp.statusText}`, 'err');
      if (resp.status === 401 || resp.status === 403) {
        localStorage.removeItem(TOKEN_KEY);
        setStatus('Token rejected — click Set token and paste a fresh PAT.', 'err');
      }
    }
  } catch (err) {
    setStatus(`Could not reach GitHub: ${err.message}`, 'err');
  } finally {
    fetchBtn.disabled = false;
  }
});

render();
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Funding Blotter</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Newsreader:opsz,wght@6..72,400;6..72,600&display=swap" rel="stylesheet">
<style>__CSS__</style>
</head>
<body>
<div class="wrap">
  <header class="mast">
    <h1>Funding Blotter</h1>
    <div class="sub">
      finsmes &nbsp;·&nbsp; aifunding.me
      &nbsp;&nbsp;|&nbsp;&nbsp; built <b>__BUILT__</b>
      &nbsp;&nbsp;|&nbsp;&nbsp; <b id="count"></b>
    </div>
    <div class="rule"></div>
  </header>

  <div class="controls">
    <button class="chip" data-mode="all" aria-pressed="true">All</button>
    <button class="chip" data-mode="priority" aria-pressed="false">Flagged</button>
    <button class="chip" data-mode="early" aria-pressed="false">Seed &amp; A</button>
    <button class="chip" data-mode="disclosed" aria-pressed="false">Disclosed</button>
    <input id="q" type="search" placeholder="company, investor, city" aria-label="Search rounds">
    <button type="button" class="fetch-btn" id="fetch-today" title="Run the daily scrape on your Mac self-hosted runner">Fetch today's deals</button>
    <button type="button" class="token-btn" id="set-token" title="Store a GitHub PAT in this browser only">Set token</button>
    <div id="fetch-status" aria-live="polite"></div>
  </div>

  <main id="feed"></main>

  <div class="rule thin"></div>
  <p class="foot">
    Red rail = matches your sector and location filters. n/d = amount not disclosed;
    check the SEC Form D filing for the real number.
    &nbsp;·&nbsp; <b>Fetch today's deals</b> runs the pipeline on your Mac runner
    (FinSMEs blocks GitHub cloud IPs). Mac must be on and the runner listening.
  </p>
</div>
<script>__JS__</script>
</body>
</html>
"""


def write_html(deals: list[dict], path: str, github: dict | None = None) -> None:
    fields = (
        "company", "description", "amount_usd", "amount_raw", "stage",
        "location", "investors", "source", "url", "published_at",
        "priority", "score", "sector_label", "sector_reason",
    )
    slim = [{k: d.get(k) for k in fields} for d in deals]
    built = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC").upper()
    gh = github or {
        "owner": "Garry-j-code",
        "repo": "funding-blotter",
        "workflow": "daily.yml",
    }

    html = (
        TEMPLATE.replace("__CSS__", CSS)
        .replace("__BUILT__", built)
        .replace(
            "__JS__",
            JS.replace("__DATA__", json.dumps(slim, ensure_ascii=False))
            .replace("__GITHUB__", json.dumps(gh, ensure_ascii=False)),
        )
    )

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log.info("render: %s (%d deals, %.1f KB)", path, len(deals), len(html) / 1024)


def write_csv(deals: list[dict], path: str) -> None:
    cols = (
        "published_at", "company", "amount_usd", "amount_raw", "stage",
        "location", "description", "investors", "source", "url",
        "priority", "score",
    )
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(deals)
    log.info("render: %s (%d rows)", path, len(deals))
