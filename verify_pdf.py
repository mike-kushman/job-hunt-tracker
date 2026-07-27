#!/usr/bin/env python3
"""Check a freshly built Job_Hunt_Ledger.pdf before it gets published.

Run after build_pdf.py. Exits non-zero if the PDF does not faithfully contain
the ledger, so a broken document never reaches the public URL.

    python3 verify_pdf.py [data.json] [tracker.html] [Job_Hunt_Ledger.pdf]

Needs pypdf (CI installs it). Used by .github/workflows/rebuild-pdf.yml.
"""
import json
import re
import sys

from pypdf import PdfReader

_a = sys.argv[1:]
data_path = _a[0] if len(_a) > 0 else "data.json"
html_path = _a[1] if len(_a) > 1 else "tracker.html"
pdf_path = _a[2] if len(_a) > 2 else "Job_Hunt_Ledger.pdf"

data = json.load(open(data_path, encoding="utf-8"))
rows = [r for s in data["sections"] for r in s["rows"]]

try:
    reader = PdfReader(pdf_path)
    pages = len(reader.pages)
    text = "\n".join(p.extract_text() for p in reader.pages)
except Exception as exc:                      # truncated, empty, or not a PDF
    print(f"FAIL: {pdf_path} is not a readable PDF — {type(exc).__name__}: {exc}")
    sys.exit(1)

problems = []

# 1. every company must appear. Names line-wrap during extraction, so match on
#    the longest word in the name rather than the whole string.
missing = []
for r in rows:
    probe = max(r["company"].split(), key=len)
    if probe not in text:
        missing.append(r["company"])
if missing:
    problems.append(f"{len(missing)} companies absent from the PDF text: {missing[:8]}")

# 2. every glossary term must reach the appendix
m = re.search(r"const GLOSSARY = (\{.*?\});", open(html_path, encoding="utf-8").read(), re.S)
if not m:
    problems.append(f"no `const GLOSSARY = {{...}};` block in {html_path}")
else:
    terms = [t["term"] for v in json.loads(m.group(1)).values() for t in v]
    gone = [t for t in terms if t not in text]
    if gone:
        problems.append(f"{len(gone)} glossary terms absent from the appendix: {gone[:8]}")

# 3. it should be a real multi-page document, not a stub
if pages < 2:
    problems.append(f"only {pages} page(s) — the ledger cannot fit on one")

print(f"{pdf_path}: {pages} pages, {len(rows)} ledger rows checked")
if problems:
    for p in problems:
        print("FAIL:", p)
    sys.exit(1)
print("OK — every company and glossary term is present.")
