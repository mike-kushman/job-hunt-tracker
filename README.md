# Job Hunt Tracker

Public, searchable view of Michael Kushman’s verified job-application ledger.

- Live site: https://mike-kushman.github.io/job-hunt-tracker/
- Strategy dashboard: https://mike-kushman.github.io/job-hunt-tracker/compass/
- Simplified Google Sheet: https://docs.google.com/spreadsheets/d/1iNAL9T_iAbafWFMRKCvHP2SmOcyJGqoVvBVwnBhaGVg/edit
- Canonical audit ledger: https://docs.google.com/spreadsheets/d/1zwuGx3hu1YXHsEX6zr-wJJU507WFZHMkp-dONqTb0rs/edit

Current counts and reconciliation dates live in `data.json`. Submitted
applications and delivered live-role emails count; drafts, bounces, opened
forms, research, speculative outreach, and duplicate resends do not.

## How it fits together

`data.json` is the source of truth. Everything else is derived from it.

| file | what it is |
|---|---|
| `data.json` | the ledger — every row, the scoreboard, the standing rules |
| `tracker.html` | the site: one self-contained file, no build step, no frameworks. Fetches `data.json` and renders client-side |
| `index.html` | a byte-identical twin of `tracker.html` (GitHub Pages serves this one) — keep them in sync |
| `compass/index.html` | the morning strategy dashboard. Fetches `../data.json`; personal projects, review history, focus targets, classifications and activity dates stay in that browser |
| `build_xlsx_from_json.py` | generates the workbook (needs `openpyxl`) |
| `build_pdf.py` | generates the print PDF (standard library + headless Chrome, no new deps) |

The **glossary lives inside `tracker.html`** as `const GLOSSARY = {...}` — valid
JSON so both Python scripts lift the same definitions out of it. Edit it in one
place and everything follows.

```bash
python3 build_xlsx_from_json.py            # → Job_Application_Tracker.xlsx
python3 build_pdf.py                       # → Job_Hunt_Ledger.pdf (+ print.html to inspect)
cp tracker.html index.html                 # after ANY edit to tracker.html
```

Generated files are gitignored — rebuild them, don't commit them.

### Status colours are semantic and fixed

red = rejected · amber = submitted, never acknowledged · blue = confirmed
received · green = a human engaged · grey = never arrived · purple = cold
outreach. Red and green are hard to tell apart for deuteranopic readers, so
**every status always carries its text label and count** — colour is never the
only signal, on the page, in the workbook, or in the PDF.
