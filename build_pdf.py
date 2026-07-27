#!/usr/bin/env python3
"""build_pdf.py — print-oriented PDF companion for the job-hunt ledger.

    python3 build_pdf.py [data.json] [tracker.html] [out.pdf]

Defaults: data.json, tracker.html, Job_Hunt_Ledger.pdf (all relative to CWD).

HOW IT WORKS
------------
Standard library only. No new dependencies, ever.

1. Read data.json.
2. Read the glossary out of tracker.html (the website is the single source of
   truth for definitions; see extract_glossary()).
3. Write a *print-optimised* HTML file with every value inlined — no fetch(),
   no XHR, no external assets — so it renders identically under file:// with
   no CORS problems.
4. Hand that file to headless Chrome, which does the pagination and writes the
   PDF.

The intermediate print HTML is deliberately LEFT ON DISK next to the PDF as
print.html so a human can open it in a browser, inspect the markup, and hit
Cmd-P to compare against the generated PDF. It is not a temp file; do not
"clean it up".

Chrome is invoked as:

    "$CHROME" --headless=new --no-pdf-header-footer --print-to-pdf=OUT IN

$CHROME defaults to the standard macOS install path and can be overridden with
the CHROME environment variable.

DESIGN POSTURE
--------------
This is a DOCUMENT, not a screenshot of the dark website. White paper, serif
type, real @page margins, page breaks that fall between rows instead of
through them, and a status vocabulary that survives a black-and-white laser
printer: every status carries its text label and its count, always. Colour is
a secondary cue only — the primary cues are the words, plus weight, border
style, and fill pattern. Red/green are indistinguishable to deuteranopic
readers and both are grey on a mono printer, so neither is ever load-bearing.
"""

from __future__ import annotations

import datetime
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

# --------------------------------------------------------------------------
# Contract constants — these MUST agree with tracker.html and the xlsx build.
# --------------------------------------------------------------------------

DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Hard ceiling on the Chrome run, and how long the PDF must sit at a constant
# size before we call it finished. See render_pdf() for why this is needed.
CHROME_TIMEOUT = 120.0
PDF_SETTLE_SECONDS = 1.5

# Screen palette from the shared contract. Kept here so the mapping is
# auditable, but these tints are too light for paper, so the print stylesheet
# derives darker inks from the same hues (see CSS, .p-* rules).
SCREEN_COLOURS = {
    "bounced": "#8b93a3",
    "rejected": "#f87171",
    "engaged": "#34d399",
    "cold": "#c084fc",
    "confirmed": "#60a5fa",
    "submitted": "#fbbf24",
}

# Status label + plain-English meaning. Verbatim from the contract.
STATUS_LABELS = {
    "rejected": ("Rejected", "They said no."),
    "submitted": ("Submitted, no reply", "Sent. Nobody has acknowledged it."),
    "confirmed": ("Confirmed received", "Their system said it arrived."),
    "engaged": ("A human engaged", "Someone actually replied and talked."),
    "bounced": ("Never arrived", "Bounced or withdrawn before it landed."),
    "cold": ("Cold outreach", "No posted job; introduced myself anyway."),
}

# Display order for the stacked bar and the legend: worst news first, best
# news last, so the eye reads the bar as a story left to right.
STATUS_ORDER = ["rejected", "submitted", "confirmed", "engaged", "bounced", "cold"]

ENGAGED_WORDS = (
    "conversation", "screen", "interview", "offer", "loop", "take-home", "onsite",
)

# Channel classification patterns. Order matters; first match wins.
#
# NOTE on the 4th pattern: the shared contract writes it as
#   /applied via|application submitted|application form|web form|portal|careers page/
# and separately states the expected counts (Web form 1, Not recorded 24).
# Against the real data those two statements disagree by exactly one row: the
# only row the "Web form 1" count can be referring to is Rivet ("Google Form
# recorded"), and the literal alternation above misses it. "google form" is
# therefore added as an explicit alternative so the counts come out as the
# contract specifies. It cannot steal rows from the earlier patterns (first
# match wins). See check_contract_counts() — the build prints a loud warning
# if any channel or status count drifts from the contract.
CHANNEL_PATTERNS = [
    ("Direct email", r"direct email|cold email|emailed"),
    ("YC board", r"work at a startup|\bwaas\b|ycombinator|\byc\b"),
    ("Company application system",
     r"greenhouse|ashby|lever|workday|icims|polymer|rippling|wellfound|paraform"),
    ("Web form",
     r"google form|applied via|application submitted|application form|web form|portal|careers page"),
]
CHANNEL_FALLBACK = "Not recorded"

# Expected counts from the contract, used as a self-check (warn, never fake).
# Resnapshotted after the 7/27 Sailor Health (Ashby) and Dench.com (YC) receipts.
EXPECTED_STATUS = {"submitted": 53, "confirmed": 37, "rejected": 3, "engaged": 1}
EXPECTED_CHANNEL = {
    "Direct email": 36,
    "Company application system": 29,
    "Not recorded": 24,
    "YC board": 4,
    "Web form": 1,
}

# The two applications whose ATS confirmation was superseded by a later
# outcome, which is why meta.scoreboard.ats_confirmations (cumulative) runs
# ahead of the number of rows currently showing "Submitted ✓" (current state).
SUPERSEDED_ATS = ("Rillet", "Tsenta")


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def status_key(status: str) -> str:
    """Map a raw status string to a status key. Order matters — replicate exactly."""
    s = (status or "").lower()
    if "bounced" in s or "withdrawn" in s:
        return "bounced"
    if "rejected" in s or "declined" in s:
        return "rejected"
    if any(w in s for w in ENGAGED_WORDS):
        return "engaged"
    if "cold" in s or "emailed" in s:
        return "cold"
    if "✓" in (status or ""):
        return "confirmed"
    if "submitted" in s:
        return "submitted"
    return "submitted"


def channel_of(note: str) -> str:
    """Classify how an application was sent, from the note text.

    Never infers *status* from note text: plenty of notes say "delivered; no
    bounce", which is the opposite of a bounce.
    """
    n = (note or "").lower()
    for name, pattern in CHANNEL_PATTERNS:
        if re.search(pattern, n):
            return name
    return CHANNEL_FALLBACK


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def load_data(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"build_pdf: data file not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"build_pdf: {path} is not valid JSON: {exc}")


def extract_glossary(path: str) -> dict:
    """Pull `const GLOSSARY = {...};` out of tracker.html.

    tracker.html is the source of truth for definitions. If the glossary
    cannot be found or parsed we FAIL LOUDLY rather than shipping a PDF with a
    silently empty appendix — a missing glossary is a broken document, not a
    cosmetic issue.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            markup = fh.read()
    except FileNotFoundError:
        raise SystemExit(
            f"build_pdf: tracker file not found: {path}\n"
            "  The glossary appendix is extracted from tracker.html. Pass the "
            "correct path as the second argument."
        )

    match = re.search(r"const GLOSSARY = (\{.*?\});", markup, re.S)
    if not match:
        raise SystemExit(
            f"build_pdf: could not find `const GLOSSARY = {{...}};` in {path}\n"
            "  The appendix is extracted from tracker.html and must not be empty. "
            "Either tracker.html has not been updated with the glossary yet, or the "
            "declaration no longer matches the agreed shape:\n"
            "    const GLOSSARY = { ...valid JSON object... };"
        )

    try:
        glossary = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"build_pdf: found `const GLOSSARY` in {path} but it is not valid JSON: {exc}\n"
            "  It must be a plain JSON object literal (double-quoted keys, no "
            "trailing commas, no JS expressions)."
        )

    if not isinstance(glossary, dict) or not glossary:
        raise SystemExit(
            f"build_pdf: the GLOSSARY object in {path} is empty. Refusing to ship a "
            "PDF with an empty appendix."
        )
    return glossary


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def all_rows(data: dict) -> list:
    return [row for section in data.get("sections", []) for row in section.get("rows", [])]


def day_label(section_title: str) -> str:
    """'JULY 26, 2026 — 10 VERIFIED' -> 'July 26, 2026'."""
    head = re.split(r"\s+[—–-]\s+", section_title)[0].strip()
    return head.title().replace(", 2026", ", 2026")


def check_contract_counts(rows: list) -> list:
    """Compare live counts against the contract. Warn loudly; never adjust."""
    warnings = []
    status_counts = {}
    channel_counts = {}
    for row in rows:
        k = status_key(row.get("status", ""))
        status_counts[k] = status_counts.get(k, 0) + 1
        c = channel_of(row.get("note", ""))
        channel_counts[c] = channel_counts.get(c, 0) + 1

    for key, expected in EXPECTED_STATUS.items():
        actual = status_counts.get(key, 0)
        if actual != expected:
            warnings.append(f"status '{key}': contract says {expected}, data has {actual}")
    for key, expected in EXPECTED_CHANNEL.items():
        actual = channel_counts.get(key, 0)
        if actual != expected:
            warnings.append(f"channel '{key}': contract says {expected}, data has {actual}")
    return warnings


# --------------------------------------------------------------------------
# HTML fragments
# --------------------------------------------------------------------------

def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", (url or "").strip(), re.I)
    if not m:
        return ""
    return m.group(1).lower().replace("www.", "")


CSS = """
:root {
  --ink: #16181d;
  --ink-soft: #4a4f5a;
  --ink-faint: #6d7280;
  --rule: #c7ccd4;
  --rule-strong: #16181d;
  --paper-tint: #f4f5f7;
}

/* Backgrounds and fill patterns are load-bearing here (they are the
   greyscale-safe status cue), so force Chrome to print them. */
* { -webkit-print-color-adjust: exact; print-color-adjust: exact; box-sizing: border-box; }

@page {
  size: Letter;
  margin: 0.62in 0.6in 0.7in 0.6in;
}

html { font-size: 11px; }

body {
  margin: 0;
  background: #fff;
  color: var(--ink);
  font-family: Georgia, "Times New Roman", Times, serif;
  font-size: 9.4pt;
  line-height: 1.38;
  -webkit-font-smoothing: antialiased;
}

h1, h2, h3 { font-family: Georgia, "Times New Roman", Times, serif; font-weight: 700; }

/* ---------- masthead ---------- */
/* Page 1 is a hard one-page budget: masthead + scoreboard + three charts +
   the reconciliation note + the counting rules must all land above the fold.
   The sizes below are tuned to that budget on US Letter — if you enlarge
   anything here, re-check that the ledger still starts on page 2. */
.masthead { border-bottom: 2.5px solid var(--rule-strong); padding-bottom: 6px; margin-bottom: 9px; }
.masthead h1 { font-size: 20pt; letter-spacing: -0.01em; margin: 0 0 2px; line-height: 1.1; }
.masthead .sub { font-size: 8.8pt; color: var(--ink-soft); font-style: italic; margin: 0 0 4px; }
.masthead .upd { font-size: 8.2pt; color: var(--ink-faint); margin: 0; }
.masthead .doc-kind {
  font-size: 7.2pt; letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--ink-faint); margin: 0 0 4px; font-family: Georgia, serif;
}

/* ---------- scoreboard ---------- */
.scoreboard {
  display: flex; gap: 0; margin: 0 0 11px;
  border: 1px solid var(--rule); border-left: none;
  page-break-inside: avoid;
}
.scoreboard .cell {
  flex: 1 1 0; padding: 5px 4px 4px; border-left: 1px solid var(--rule); text-align: center;
}
.scoreboard .cell .n { display: block; font-size: 15.5pt; font-weight: 700; line-height: 1; }
.scoreboard .cell .k {
  display: block; margin-top: 3px; font-size: 6.6pt; letter-spacing: 0.06em;
  text-transform: uppercase; color: var(--ink-soft); line-height: 1.25;
}
.scoreboard .cell.lead { background: var(--paper-tint); }

/* ---------- generic block ---------- */
.block { margin: 0 0 12px; page-break-inside: avoid; }
.block > h2 {
  font-size: 9.8pt; margin: 0 0 2px; padding-bottom: 2px;
  border-bottom: 1px solid var(--rule-strong);
}
.block > .note { font-size: 7.9pt; color: var(--ink-faint); margin: 3px 0 6px; font-style: italic; }

/* ---------- bar charts (divs, not SVG, not images) ---------- */
.bars { width: 100%; }
.barrow { display: flex; align-items: center; gap: 7px; margin-bottom: 3px; page-break-inside: avoid; }
/* Wide enough for the longest category name ("Company application system").
   A category label is never truncated: an ellipsis here would hide part of
   the very thing the chart is reporting. */
.barrow .lab {
  flex: 0 0 142px; text-align: right; font-size: 7.9pt; color: var(--ink-soft);
  white-space: nowrap;
}
.barrow .track { flex: 1 1 auto; height: 12px; background: var(--paper-tint); border: 1px solid var(--rule); position: relative; }
.barrow .fill { height: 100%; }
.barrow .num { flex: 0 0 62px; font-size: 8pt; font-variant-numeric: tabular-nums; color: var(--ink); }
.barrow .num .pct { color: var(--ink-faint); font-size: 7.2pt; }

/* ---------- status fill patterns (greyscale-safe) ----------
   Each status gets a distinct hatch geometry AND a distinct border style, so
   the segments stay tellable apart in pure black and white. The hue is a
   third, redundant cue only. */
.p-rejected {
  background-color: #f6e2e2;
  background-image: repeating-linear-gradient(45deg, #7d1616 0 1.6px, transparent 1.6px 4.2px);
  border: 1px solid #7d1616;
}
.p-submitted {
  background-color: #fbf1d8;
  background-image: repeating-linear-gradient(90deg, #6f5406 0 1px, transparent 1px 6px);
  border: 1px dashed #6f5406;
}
.p-confirmed {
  background-color: #e4edf9;
  background-image: repeating-linear-gradient(0deg, #123c72 0 1px, transparent 1px 4px);
  border: 1px solid #123c72;
}
.p-engaged {
  background-color: #14532d;
  background-image: none;
  border: 1px solid #0a2e19;
}
.p-bounced {
  background-color: #edeff2;
  background-image:
    repeating-linear-gradient(45deg, #4d5361 0 1px, transparent 1px 5px),
    repeating-linear-gradient(-45deg, #4d5361 0 1px, transparent 1px 5px);
  border: 1px dotted #4d5361;
}
.p-cold {
  background-color: #efe6fa;
  background-image: repeating-linear-gradient(135deg, #46257a 0 1px, transparent 1px 4.5px);
  border: 1px solid #46257a;
}
/* Plain quantity bars (days, channels). Mid-grey, not a pale tint: these have
   to read at arm's length on a mono printer. */
.p-neutral { background-color: #a9b0bb; border: 1px solid #5d6371; }
.p-unrecorded {
  background-color: #eceef1;
  background-image: repeating-linear-gradient(45deg, #767c88 0 1px, transparent 1px 5px);
  border: 1px dotted #767c88;
}

/* ---------- stacked status bar ---------- */
.stack {
  display: flex; width: 100%; height: 24px; margin: 5px 0 7px;
  border: 1.4px solid var(--rule-strong); page-break-inside: avoid;
}
.stack .seg {
  display: flex; align-items: center; justify-content: center;
  min-width: 11px; overflow: hidden;
  font-size: 8pt; font-weight: 700; font-variant-numeric: tabular-nums;
  border-top: none; border-bottom: none;
}
.stack .seg:first-child { border-left: none; }
.stack .seg:last-child { border-right: none; }
.stack .seg.on-dark { color: #fff; }
.stack .seg .cap { background: rgba(255,255,255,0.82); padding: 0 3px; border-radius: 1px; }
.stack .seg.on-dark .cap { background: transparent; }

/* ---------- status legend / key ---------- */
table.key { width: 100%; border-collapse: collapse; page-break-inside: avoid; }
table.key td { padding: 2.4px 4px; vertical-align: top; border-bottom: 1px solid #e6e8ec; font-size: 8.1pt; }
table.key tr:last-child td { border-bottom: none; }
table.key td.sw-cell { width: 22px; }
table.key td.lab-cell { width: 118px; font-weight: 700; white-space: nowrap; }
table.key td.mean-cell { color: var(--ink-soft); }
table.key td.n-cell { width: 74px; text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
table.key td.n-cell .pct { color: var(--ink-faint); font-size: 7.2pt; }
table.key tr.zero td { color: var(--ink-faint); font-weight: 400; }
table.key tr.zero td.lab-cell { font-weight: 400; }
.sw { display: inline-block; width: 14px; height: 10px; vertical-align: -1px; }

/* ---------- reconciliation + rules ---------- */
.recon {
  border: 1px solid var(--rule-strong); border-left: 4px solid var(--rule-strong);
  padding: 6px 9px; margin: 0 0 8px; font-size: 8.1pt; page-break-inside: avoid;
}
/* only the leading title is a block — inline <strong> numbers in the body
   must stay in the run of the sentence */
.recon > strong:first-child { display: block; margin-bottom: 1px; font-size: 8.2pt; }
.rules { font-size: 7.8pt; color: var(--ink-soft); page-break-inside: avoid; }
.rules p { margin: 0 0 3px; }
.rules .rk { font-weight: 700; color: var(--ink); }

/* ---------- section openers ---------- */
.newpage { page-break-before: always; }
.section-open { border-bottom: 2.5px solid var(--rule-strong); padding-bottom: 6px; margin-bottom: 10px; }
.section-open h2 { font-size: 15pt; margin: 0 0 2px; }
.section-open p { margin: 0; font-size: 8.4pt; color: var(--ink-faint); font-style: italic; }

/* ---------- ledger ---------- */
table.ledger { width: 100%; border-collapse: collapse; }
table.ledger thead { display: table-header-group; }   /* repeat header on every page */
table.ledger tfoot { display: table-footer-group; }
table.ledger thead th {
  font-size: 7pt; letter-spacing: 0.09em; text-transform: uppercase; text-align: left;
  padding: 4px 5px; border-bottom: 1.6px solid var(--rule-strong);
  border-top: 1.6px solid var(--rule-strong); background: #fff; color: var(--ink-soft);
  font-weight: 700;
}
table.ledger tbody { page-break-inside: avoid; }      /* keep an entry whole */
table.ledger td { padding: 3.5px 5px; vertical-align: top; font-size: 8.5pt; }
table.ledger tbody.entry { border-bottom: 1px solid #e6e8ec; }
table.ledger tbody.entry td { border-top: none; }
table.ledger tr.main td { border-bottom: none; }
table.ledger td.co { font-weight: 700; width: 15%; }
table.ledger td.role { width: 24%; }
table.ledger td.pay { width: 14%; font-variant-numeric: tabular-nums; }
table.ledger td.loc { width: 16%; color: var(--ink-soft); }
table.ledger td.st { width: 23%; }
table.ledger td.dt { width: 8%; text-align: right; font-variant-numeric: tabular-nums; color: var(--ink-soft); }
table.ledger tr.aux td {
  padding-top: 0; padding-bottom: 5px; font-size: 7.7pt; color: var(--ink-soft);
  border-bottom: 1px solid #e6e8ec;
}
table.ledger tr.aux .chan {
  display: inline-block; font-size: 6.6pt; letter-spacing: 0.07em; text-transform: uppercase;
  border: 1px solid var(--rule); padding: 0 3px; margin-right: 5px; color: var(--ink);
  white-space: nowrap;
}
table.ledger tr.aux .chan.unrecorded { color: var(--ink-faint); border-style: dotted; font-style: italic; }
table.ledger tr.aux .src { color: var(--ink-faint); white-space: nowrap; }

tbody.dayhead td {
  padding: 9px 0 3px; border-bottom: 1px solid var(--rule-strong);
  font-size: 8.4pt; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase;
}
tbody.dayhead { page-break-after: avoid; page-break-inside: avoid; }

/* status tag inside the ledger: label first, pattern swatch second, hue last */
.tag { display: inline-block; white-space: nowrap; font-size: 7.8pt; }
.tag .sw { margin-right: 4px; }
.tag .txt { font-weight: 700; }
.tag.k-submitted .txt { font-weight: 400; }
.tag.k-engaged .txt { text-decoration: underline; text-underline-offset: 2px; }

/* ---------- glossary ---------- */
/* A whole group is allowed to span a page break — some run long, and forcing
   them to stay together strands half a page of white. What must never break
   is a single term away from its definition, or a heading away from its first
   term. */
.gloss-group { margin: 0 0 11px; page-break-inside: auto; }
.gloss-group > h3 {
  font-size: 9pt; letter-spacing: 0.1em; text-transform: uppercase; color: var(--ink-soft);
  margin: 0 0 5px; padding-bottom: 2px; border-bottom: 1px solid var(--rule);
  page-break-after: avoid;
}
.term { margin: 0 0 6px; page-break-inside: avoid; }
.term .t { font-weight: 700; font-size: 8.8pt; }
.term .al { font-style: italic; color: var(--ink-faint); font-size: 7.8pt; }
.term .d { display: block; font-size: 8.3pt; color: var(--ink-soft); margin-top: 1px; }

.colophon {
  margin-top: 14px; padding-top: 6px; border-top: 1px solid var(--rule);
  font-size: 7.4pt; color: var(--ink-faint); page-break-inside: avoid;
}
"""


def render_bar_rows(items, total_max, palette_class, show_pct_of=None) -> str:
    """items: list of (label, count, optional css class override)."""
    out = []
    for item in items:
        label, count = item[0], item[1]
        cls = item[2] if len(item) > 2 else palette_class
        width = 0 if not total_max else round(100.0 * count / total_max, 2)
        pct = ""
        if show_pct_of:
            pct = f' <span class="pct">({round(100.0 * count / show_pct_of):.0f}%)</span>'
        out.append(
            '<div class="barrow">'
            f'<div class="lab">{esc(label)}</div>'
            '<div class="track">'
            f'<div class="fill {cls}" style="width:{width}%"></div>'
            "</div>"
            f'<div class="num">{count}{pct}</div>'
            "</div>"
        )
    return "\n".join(out)


def build_summary_page(data: dict, rows: list) -> str:
    meta = data.get("meta", {})
    board = meta.get("scoreboard", {})
    total = len(rows)

    # --- per-day counts, in the order the sections are listed (newest first)
    days = []
    for section in data.get("sections", []):
        days.append((day_label(section.get("title", "")), len(section.get("rows", []))))
    day_max = max([c for _, c in days] or [1])

    # --- status counts
    status_counts = {k: 0 for k in STATUS_ORDER}
    for row in rows:
        status_counts[status_key(row.get("status", ""))] += 1

    # --- channel counts, ranked, "Not recorded" always visible
    channel_counts = {}
    for row in rows:
        c = channel_of(row.get("note", ""))
        channel_counts[c] = channel_counts.get(c, 0) + 1
    ranked = sorted(channel_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    chan_max = max([c for _, c in ranked] or [1])

    parts = []

    # masthead
    parts.append(
        '<header class="masthead">'
        '<p class="doc-kind">Printed ledger &middot; companion to the live tracker</p>'
        f'<h1>{esc(meta.get("name", "Job Hunt"))}</h1>'
        f'<p class="sub">{esc(meta.get("subtitle", ""))}</p>'
        f'<p class="upd">Updated {esc(meta.get("updated", ""))}</p>'
        "</header>"
    )

    # scoreboard
    tiles = [
        ("applications_in", "Applications in", True),
        ("ats_confirmations", "ATS confirmations", False),
        ("human_replies", "Human replies", False),
        ("live_conversations", "Live conversations", False),
        ("rejections", "Rejections", False),
        ("in_pipeline", "In pipeline", False),
        ("backlog", "Backlog", False),
    ]
    cells = []
    for key, caption, lead in tiles:
        if key not in board:
            continue
        cells.append(
            f'<div class="cell{" lead" if lead else ""}">'
            f'<span class="n">{esc(board[key])}</span>'
            f'<span class="k">{esc(caption)}</span>'
            "</div>"
        )
    parts.append(f'<div class="scoreboard">{"".join(cells)}</div>')

    # --- chart 1: applications per day
    parts.append(
        '<div class="block">'
        "<h2>How many went out, by day</h2>"
        f'<p class="note">{total} verified applications across {len(days)} active days. '
        "Bars are to scale against the busiest day.</p>"
        f'<div class="bars">{render_bar_rows(days, day_max, "p-neutral", show_pct_of=total)}</div>'
        "</div>"
    )

    # --- chart 2: where all 92 stand today
    segs = []
    key_rows = []
    for key in STATUS_ORDER:
        count = status_counts.get(key, 0)
        label, meaning = STATUS_LABELS[key]
        share = 100.0 * count / total if total else 0
        if count:
            cap = f'<span class="cap">{count}</span>' if share >= 7 else ""
            dark = " on-dark" if key == "engaged" else ""
            segs.append(
                f'<div class="seg p-{key}{dark}" style="flex:{count} 1 0">{cap}</div>'
            )
        key_rows.append(
            f'<tr class="{"" if count else "zero"}">'
            f'<td class="sw-cell"><span class="sw p-{key}"></span></td>'
            f'<td class="lab-cell">{esc(label)}</td>'
            f'<td class="mean-cell">{esc(meaning)}</td>'
            f'<td class="n-cell">{count if count else "none"}'
            + (f' <span class="pct">({round(share):.0f}%)</span>' if count else "")
            + "</td></tr>"
        )
    parts.append(
        '<div class="block">'
        f"<h2>Where all {total} stand today</h2>"
        '<p class="note">Every status carries its own label, count and fill pattern &mdash; '
        "the chart is readable in black and white, and colour is never the only cue.</p>"
        f'<div class="stack">{"".join(segs)}</div>'
        f'<table class="key">{"".join(key_rows)}</table>'
        "</div>"
    )

    # --- chart 3: how applications were sent
    chan_items = []
    for name, count in ranked:
        cls = "p-unrecorded" if name == CHANNEL_FALLBACK else "p-neutral"
        chan_items.append((name, count, cls))
    parts.append(
        '<div class="block">'
        "<h2>How the applications were sent</h2>"
        '<p class="note">Read from the notes column. &ldquo;Not recorded&rdquo; means the note '
        "does not say how it went out &mdash; it is shown, not hidden, because it is a real "
        "share of the ledger.</p>"
        f'<div class="bars">{render_bar_rows(chan_items, chan_max, "p-neutral", show_pct_of=total)}</div>'
        "</div>"
    )

    # --- honest ATS reconciliation
    cumulative = board.get("ats_confirmations")
    current = status_counts.get("confirmed", 0)
    if cumulative is not None and cumulative != current:
        delta = cumulative - current
        names = " and ".join(SUPERSEDED_ATS)
        parts.append(
            '<div class="recon">'
            "<strong>Why the confirmation numbers differ</strong>"
            f"The scoreboard says <strong>{cumulative}</strong> ATS confirmations, but only "
            f"<strong>{current}</strong> rows below currently read &ldquo;Confirmed received.&rdquo; "
            f"Both are correct. {cumulative} is <em>cumulative</em> &mdash; every confirmation ever "
            f"received. The ledger shows <em>current state</em>, and {delta} of those "
            f"confirmations ({esc(names)}) were superseded by a later outcome, so those rows now "
            "show that outcome instead. Nothing was lost and nothing was double-counted."
            "</div>"
        )

    # --- counting rules
    rule_bits = []
    if meta.get("counting_rule"):
        rule_bits.append(
            f'<p><span class="rk">What counts:</span> {esc(meta["counting_rule"])}</p>'
        )
    if data.get("standing_rules"):
        rule_bits.append(
            f'<p><span class="rk">Standing rules:</span> {esc(data["standing_rules"])}</p>'
        )
    if rule_bits:
        parts.append(f'<div class="rules">{"".join(rule_bits)}</div>')

    return "\n".join(parts)


def build_ledger(data: dict, rows: list) -> str:
    total = len(rows)
    body = []
    body.append(
        '<section class="newpage">'
        '<div class="section-open">'
        "<h2>The full ledger</h2>"
        f"<p>All {total} verified applications, newest day first. "
        "Each entry carries its status in words, plus the channel it went out through.</p>"
        "</div>"
        '<table class="ledger">'
        "<thead><tr>"
        "<th>Company</th><th>Role</th><th>Compensation</th><th>Location</th>"
        "<th>Status</th><th>Date</th>"
        "</tr></thead>"
    )

    for section in data.get("sections", []):
        title = section.get("title", "")
        body.append(f'<tbody class="dayhead"><tr><td colspan="6">{esc(title)}</td></tr></tbody>')
        for row in section.get("rows", []):
            key = status_key(row.get("status", ""))
            label, _meaning = STATUS_LABELS[key]
            chan = channel_of(row.get("note", ""))
            chan_cls = " unrecorded" if chan == CHANNEL_FALLBACK else ""
            host = host_of(row.get("url", ""))
            src = f' <span class="src">&middot; {esc(host)}</span>' if host else ""
            note = esc(row.get("note", ""))
            body.append(
                '<tbody class="entry">'
                '<tr class="main">'
                f'<td class="co">{esc(row.get("company", ""))}</td>'
                f'<td class="role">{esc(row.get("role", ""))}</td>'
                f'<td class="pay">{esc(row.get("salary", ""))}</td>'
                f'<td class="loc">{esc(row.get("location", ""))}</td>'
                f'<td class="st"><span class="tag k-{key}">'
                f'<span class="sw p-{key}"></span><span class="txt">{esc(label)}</span>'
                "</span></td>"
                f'<td class="dt">{esc(row.get("date", ""))}</td>'
                "</tr>"
                '<tr class="aux">'
                f'<td colspan="6"><span class="chan{chan_cls}">{esc(chan)}</span>{note}{src}</td>'
                "</tr>"
                "</tbody>"
            )

    body.append("</table></section>")
    return "\n".join(body)


def build_glossary(glossary: dict) -> str:
    out = [
        '<section class="newpage">'
        '<div class="section-open">'
        "<h2>Appendix &mdash; plain-language glossary</h2>"
        "<p>Extracted from the live tracker so the paper and the website always say the "
        "same thing.</p>"
        "</div>"
    ]
    for group, terms in glossary.items():
        out.append(f'<div class="gloss-group"><h3>{esc(group)}</h3>')
        for entry in terms or []:
            aliases = entry.get("aliases") or []
            alias_txt = ""
            if aliases:
                alias_txt = f' <span class="al">also: {esc(", ".join(str(a) for a in aliases))}</span>'
            out.append(
                '<div class="term">'
                f'<span class="t">{esc(entry.get("term", ""))}</span>{alias_txt}'
                f'<span class="d">{esc(entry.get("definition", ""))}</span>'
                "</div>"
            )
        out.append("</div>")
    out.append("</section>")
    return "\n".join(out)


def build_print_html(data: dict, glossary: dict, source_note: str) -> str:
    rows = all_rows(data)
    meta = data.get("meta", {})
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    colophon = (
        '<div class="colophon">'
        f"Generated {esc(stamp)} by build_pdf.py from {esc(source_note)}. "
        "Status labels, channel classification and the glossary are shared with the live "
        "tracker. Printed status cues do not rely on colour."
        "</div>"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>{esc(meta.get('name', 'Job Hunt Ledger'))}</title>"
        f"<style>{CSS}</style></head><body>\n"
        + build_summary_page(data, rows)
        + "\n"
        + build_ledger(data, rows)
        + "\n"
        + build_glossary(glossary)
        + colophon
        + "\n</body></html>\n"
    )


# --------------------------------------------------------------------------
# Chrome
# --------------------------------------------------------------------------

def render_pdf(html_path: str, pdf_path: str) -> None:
    chrome = os.environ.get("CHROME", DEFAULT_CHROME)
    if not os.path.exists(chrome):
        found = shutil.which(chrome) or shutil.which("google-chrome") or shutil.which("chromium")
        if not found:
            raise SystemExit(
                f"build_pdf: Chrome not found at {chrome}\n"
                "  Set the CHROME environment variable to the Chrome/Chromium binary, e.g.\n"
                '  CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" '
                "python3 build_pdf.py"
            )
        chrome = found

    if os.path.exists(pdf_path):
        os.remove(pdf_path)

    # A throwaway profile keeps this from colliding with the user's running Chrome.
    with tempfile.TemporaryDirectory(prefix="build_pdf_chrome_") as profile:
        cmd = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--virtual-time-budget=5000",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            html_path,
        ]
        # Chrome 150's --headless=new writes the PDF and then, on this machine,
        # routinely fails to exit: the browser process sits there indefinitely
        # with a finished file already on disk. So don't just wait on it. Poll
        # for the PDF to appear and stop growing, then shut Chrome down
        # ourselves. The file on disk is the real success signal; the exit code
        # is only used to explain a failure.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stderr = ""
        deadline = time.monotonic() + CHROME_TIMEOUT
        last_size, stable_since = -1, None
        while time.monotonic() < deadline:
            if proc.poll() is not None:            # Chrome exited on its own
                break
            size = os.path.getsize(pdf_path) if os.path.exists(pdf_path) else 0
            if size and size == last_size:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= PDF_SETTLE_SECONDS:
                    proc.terminate()               # finished output, hung process
                    break
            else:
                stable_since = None
            last_size = size
            time.sleep(0.25)

        try:
            _out, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                _out, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stderr = "(Chrome ignored SIGKILL)"

    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
        raise SystemExit(
            "build_pdf: Chrome did not produce a PDF.\n"
            f"  exit code: {proc.returncode}\n"
            f"  stderr: {(stderr or '').strip()[:2000]}"
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list) -> int:
    data_path = argv[1] if len(argv) > 1 else "data.json"
    tracker_path = argv[2] if len(argv) > 2 else "tracker.html"
    pdf_path = argv[3] if len(argv) > 3 else "Job_Hunt_Ledger.pdf"

    data = load_data(data_path)
    glossary = extract_glossary(tracker_path)
    rows = all_rows(data)

    # Print-HTML lives next to the PDF, permanently, for inspection.
    out_dir = os.path.dirname(os.path.abspath(pdf_path))
    html_path = os.path.join(out_dir, "print.html")

    markup = build_print_html(
        data, glossary, f"{os.path.basename(data_path)} + {os.path.basename(tracker_path)}"
    )
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(markup)

    render_pdf(html_path, os.path.abspath(pdf_path))

    # ---- report, including any drift from the shared contract
    terms = sum(len(v or []) for v in glossary.values())
    status_counts = {}
    channel_counts = {}
    for row in rows:
        k = status_key(row.get("status", ""))
        status_counts[k] = status_counts.get(k, 0) + 1
        c = channel_of(row.get("note", ""))
        channel_counts[c] = channel_counts.get(c, 0) + 1

    print(f"rows            {len(rows)} across {len(data.get('sections', []))} sections")
    print("status          " + ", ".join(
        f"{STATUS_LABELS[k][0]} {status_counts.get(k, 0)}"
        for k in STATUS_ORDER if status_counts.get(k)
    ))
    print("channel         " + ", ".join(
        f"{k} {v}" for k, v in sorted(channel_counts.items(), key=lambda kv: -kv[1])
    ))
    print(f"glossary        {terms} terms in {len(glossary)} groups (from {tracker_path})")
    print(f"print html      {html_path}")
    print(f"pdf             {os.path.abspath(pdf_path)} ({os.path.getsize(pdf_path):,} bytes)")

    for warning in check_contract_counts(rows):
        print(f"WARNING: count drift — {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
