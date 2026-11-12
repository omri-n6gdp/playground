# Playground: parenting-workshop leads

`build_parenting_workshop_leads.py` builds a spreadsheet of London employers that could host parenting workshops. It pulls names from employer award lists (Working Families Top 30 family-friendly employers, Glassdoor Best Places to Work UK), then looks for contact emails and phone numbers on their sites.

## Files

- `build_parenting_workshop_leads.py`: the script
- `parenting_workshop_leads_london.xlsx`: the output spreadsheet (not in this public repo: it holds scraped contact details)
- `sources_used.txt`: the pages it pulled from

## Running it

```bash
pip install pandas openpyxl
python3 build_parenting_workshop_leads.py
```
