#!/usr/bin/env python3
"""Generate the tracker spreadsheet from data.json (the source of truth).
Usage: python3 build_xlsx_from_json.py data.json out.xlsx"""
import json, sys
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

src, out = (sys.argv + ['data.json', 'Job_Application_Tracker.xlsx'])[1:3]
d = json.load(open(src, encoding='utf-8'))

wb = openpyxl.Workbook(); ws = wb.active; ws.title = 'Tracker'
F = lambda **k: Font(name='Arial', **k)
TINT = {'c-sub':'D8F3E4','c-msg':'DCEAFB','c-watch':'FDF3D5','c-cold':'ECDFF8','c-skip':'FADDDD'}
def chip_key(status):
    s = (status or '').lower()
    if 'rejected' in s or 'bounced' in s or 'skip' in s: return 'c-skip'
    if 'submitted' in s or '✓' in (status or ''): return 'c-sub'
    if 'waas' in s: return 'c-msg'
    if 'emailed' in s: return 'c-cold'
    return 'c-watch'

headers = ['Section','Company','Role','Salary','Location','Status','Date','Notes / Next']
ws.append(headers)
for c in range(1,9):
    cell = ws.cell(1,c); cell.font = F(bold=True, size=10)
    cell.fill = PatternFill('solid', fgColor='1F2430'); cell.font = F(bold=True, size=10, color='FFFFFF')
ws.freeze_panes = 'A2'

sb = d['meta']['scoreboard']
ws.append(['SCOREBOARD', f"{sb['applications_in']} applications in", f"{sb['ats_confirmations']} ATS confirmations",
           f"{sb['live_conversations']} live conversation (Tsenta)",
           f"{sb['human_replies']} human replies · {sb['rejections']} rejection (GitHub)",
           f"{sb['in_pipeline']} in pipeline · {sb['backlog']} backlog", '7/24', d['meta']['counting_rule']])
for c in range(1,9): ws.cell(2,c).font = F(bold=True, size=10)

r = 3
for sec in d['sections']:
    ws.append([]); r += 1
    ws.cell(r,1).value = sec['title']; ws.cell(r,1).font = F(bold=True, size=11)
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8); r += 1
    for row in sec['rows']:
        ws.append([None, row['company'], row['role'], row['salary'], row['location'],
                   row['status'], row['date'], row['note']])
        tint = TINT[chip_key(row['status'])]
        for c in range(1,9):
            cell = ws.cell(r,c); cell.font = F(size=10)
            cell.fill = PatternFill('solid', fgColor=tint)
            cell.alignment = Alignment(wrap_text=True, vertical='top')
        if row.get('url'):
            ws.cell(r,2).hyperlink = row['url']; ws.cell(r,2).font = F(size=10, bold=True, color='2563EB')
        r += 1

ws.append([]); r += 1
ws.append(['STANDING RULES', d['standing_rules']]); r += 1
ws.cell(r,1).font = F(bold=True, size=10); ws.cell(r,2).font = F(size=10)
ws.cell(r,2).alignment = Alignment(wrap_text=True, vertical='top')
ws.append(['UPDATED', d['meta']['updated']]); r += 1
ws.cell(r,1).font = F(bold=True, size=10); ws.cell(r,2).font = F(size=10)

widths = [14, 22, 30, 20, 24, 22, 8, 60]
for i, w in enumerate(widths, 1): ws.column_dimensions[get_column_letter(i)].width = w
wb.save(out)
print('saved', out)
