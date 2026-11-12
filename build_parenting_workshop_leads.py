import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from io import StringIO
from typing import Dict, List, Optional, Tuple

import pandas as pd


USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
CONTACT_SCRAPE_LIMIT = int(os.getenv("CONTACT_SCRAPE_LIMIT", "200"))
WEBSITE_ENRICH_LIMIT = int(os.getenv("WEBSITE_ENRICH_LIMIT", str(CONTACT_SCRAPE_LIMIT)))


def fetch_text(url: str, retries: int = 3, backoff: float = 1.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_exc = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            last_exc = exc
            time.sleep(backoff * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError("fetch_text failed")


def fetch_json(url: str, retries: int = 3, backoff: float = 1.0) -> dict:
    return json.loads(fetch_text(url, retries=retries, backoff=backoff))


def normalize_name(name: str) -> str:
    s = name.lower()
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    stop = {
        "plc",
        "ltd",
        "limited",
        "group",
        "holdings",
        "holding",
        "company",
        "co",
        "corp",
        "corporation",
        "inc",
        "the",
        "and",
        "international",
        "uk",
        "united",
        "kingdom",
    }
    tokens = [t for t in s.split() if t and t not in stop]
    return " ".join(tokens).strip()


def extract_emails(text: str) -> List[str]:
    emails = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}", text, flags=re.I)
    cleaned = []
    for email in emails:
        email = email.lower()
        if email.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg")):
            continue
        if email.startswith("example@") or email.endswith("@example.com"):
            continue
        cleaned.append(email)
    return list(dict.fromkeys(cleaned))


def extract_phones(text: str) -> List[str]:
    # Basic UK/international phone patterns.
    patterns = [
        r"(\\+44\\s?\\d{2,4}[\\s\\-]?\\d{3,4}[\\s\\-]?\\d{3,4})",
        r"(\\b0\\d{2,4}[\\s\\-]?\\d{3,4}[\\s\\-]?\\d{3,4}\\b)",
    ]
    phones = []
    for pattern in patterns:
        phones.extend(re.findall(pattern, text))
    cleaned = []
    for phone in phones:
        cleaned.append(re.sub(r"\\s+", " ", phone).strip())
    return list(dict.fromkeys(cleaned))


def pick_best_email(emails: List[str]) -> Optional[str]:
    if not emails:
        return None
    role_keywords = [
        "info",
        "contact",
        "hello",
        "support",
        "sales",
        "business",
        "enquiries",
        "inquiries",
        "hr",
        "people",
        "careers",
        "talent",
        "jobs",
        "recruit",
        "recruitment",
        "press",
        "media",
    ]
    for kw in role_keywords:
        for email in emails:
            local = email.split("@")[0]
            if kw in local:
                return email
    return emails[0]


def find_contact_links(html: str, base_url: str) -> List[str]:
    links = re.findall(r'href="([^"]+)"', html, flags=re.I)
    keywords = ["contact", "contact-us", "get-in-touch", "support", "help", "about", "customer"]
    parsed_base = urllib.parse.urlparse(base_url)
    base_host = parsed_base.netloc
    candidates = []
    for href in links:
        if href.startswith("mailto:") or href.startswith("tel:"):
            continue
        if any(kw in href.lower() for kw in keywords):
            absolute = urllib.parse.urljoin(base_url, href)
            parsed = urllib.parse.urlparse(absolute)
            if parsed.scheme in ("http", "https") and parsed.netloc == base_host:
                candidates.append(absolute)
    # Preserve order, unique
    seen = set()
    ordered = []
    for link in candidates:
        if link in seen:
            continue
        seen.add(link)
        ordered.append(link)
    return ordered[:2]


def scrape_contact_info(website: str) -> Tuple[Optional[str], Optional[str]]:
    if not website or not website.startswith(("http://", "https://")):
        return None, None
    try:
        html = fetch_text(website, retries=2, backoff=1.0)
    except Exception:
        return None, None
    emails = extract_emails(html)
    phones = extract_phones(html)
    # Extract from mailto links if present.
    mailtos = re.findall(r"mailto:([^\"'?]+)", html, flags=re.I)
    emails.extend([m.lower() for m in mailtos])
    emails = list(dict.fromkeys(emails))
    contact_links = find_contact_links(html, website)
    for link in contact_links:
        time.sleep(0.4)
        try:
            sub_html = fetch_text(link, retries=2, backoff=1.0)
        except Exception:
            continue
        emails.extend(extract_emails(sub_html))
        phones.extend(extract_phones(sub_html))
        emails = list(dict.fromkeys([e.lower() for e in emails]))
        phones = list(dict.fromkeys(phones))
    return pick_best_email(emails), (phones[0] if phones else None)


def wikipedia_page_extract(title: str) -> Optional[dict]:
    base = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "format": "json",
        "titles": title,
        "prop": "pageprops|pageimages",
        "ppprop": "wikibase_item",
        "redirects": "1",
    }
    url = f"{base}?{urllib.parse.urlencode(params)}"
    data = fetch_json(url)
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        return page
    return None


def wikipedia_official_website(title: str) -> Optional[str]:
    base = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "parse",
        "format": "json",
        "page": title,
        "prop": "text",
        "redirects": "1",
    }
    url = f"{base}?{urllib.parse.urlencode(params)}"
    data = fetch_json(url, retries=3, backoff=2.0)
    html = data.get("parse", {}).get("text", {}).get("*", "")
    if not html:
        return None
    m = re.search(
        r"<th[^>]*>\\s*(Website|Official website)\\s*</th>\\s*<td[^>]*>.*?href=\"([^\"]+)\"",
        html,
        flags=re.I | re.S,
    )
    if m:
        link = m.group(2)
        if link.startswith(("http://", "https://")):
            return link
    # Fallback: first external link in the infobox row if present.
    m = re.search(r'class=\"external text\" href=\"([^\"]+)\"', html, flags=re.I)
    if m:
        link = m.group(1)
        if link.startswith(("http://", "https://")):
            return link
    return None


def wikidata_official_websites_from_qids(qids: List[str]) -> Dict[str, str]:
    if not qids:
        return {}
    base = "https://www.wikidata.org/w/api.php"
    results: Dict[str, str] = {}
    # Wikidata allows up to 50 ids per call.
    for i in range(0, len(qids), 25):
        chunk = qids[i : i + 25]
        params = {
            "action": "wbgetentities",
            "format": "json",
            "ids": "|".join(chunk),
            "props": "claims",
        }
        url = f"{base}?{urllib.parse.urlencode(params)}"
        data = fetch_json(url, retries=6, backoff=2.0)
        entities = data.get("entities", {})
        for qid, entity in entities.items():
            claims = entity.get("claims", {})
            if "P856" not in claims:
                continue
            claim = claims["P856"][0]
            mainsnak = claim.get("mainsnak", {})
            datavalue = mainsnak.get("datavalue", {})
            value = datavalue.get("value")
            if isinstance(value, str):
                results[qid] = value
        time.sleep(0.6)
    return results


def wikipedia_category_members(category_title: str, max_pages: int = 10) -> List[str]:
    members: List[str] = []
    base = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": f"Category:{category_title}",
        "cmlimit": "500",
        "format": "json",
    }
    cont = None
    pages = 0
    while True:
        if cont:
            params["cmcontinue"] = cont
        url = f"{base}?{urllib.parse.urlencode(params)}"
        data = fetch_json(url)
        for item in data.get("query", {}).get("categorymembers", []):
            title = item.get("title", "")
            if not title or title.startswith("Category:"):
                continue
            members.append(title)
        cont = data.get("continue", {}).get("cmcontinue")
        pages += 1
        time.sleep(0.2)
        if not cont or pages >= max_pages:
            break
    return members


def read_html_table(url: str, required_cols: List[str]) -> Optional[pd.DataFrame]:
    html = fetch_text(url)
    tables = pd.read_html(StringIO(html))
    for table in tables:
        if isinstance(table.columns, pd.MultiIndex):
            table.columns = [
                " ".join([str(c) for c in col if str(c) != "nan"]).strip()
                for col in table.columns.values
            ]
        cols = [str(c).strip() for c in table.columns]
        normalized = [re.sub(r"\\s+", " ", c.lower()) for c in cols]
        if all(req in normalized for req in required_cols):
            return table
    return None


def parse_glassdoor_bptw_uk_2026() -> List[Tuple[int, str]]:
    url = "https://www.glassdoor.co.uk/Award/Best-Places-to-Work-UK-LST_KQ0%2C22.htm"
    html = fetch_text(url)
    # Extract ranks and corresponding company overview links using indexed data-test attributes.
    rank_matches = re.findall(r"bptw-company-rank-(\d+)\"[^>]*>#(\d+)", html)
    link_matches = re.findall(r"bptw-infosite-overview-link-logo-(\d+)\"[^>]*href=\"([^\"]+)\"", html)
    rank_by_idx = {int(i): int(r) for i, r in rank_matches}
    link_by_idx = {int(i): url for i, url in link_matches}
    results = []
    for idx, rank in sorted(rank_by_idx.items()):
        if rank > 50:
            continue
        link = link_by_idx.get(idx)
        if not link:
            continue
        m = re.search(r"Working-at-([^/-]+)", link)
        if not m:
            continue
        name = m.group(1).replace("-", " ").strip()
        if name:
            results.append((rank, name))
    return results


def parse_working_families_top_employers_2025() -> List[Tuple[int, str]]:
    url = "https://workingfamilies.org.uk/wp-json/wp/v2/pages?slug=top-30-employers"
    data = fetch_json(url)
    if not data:
        return []
    content = data[0].get("content", {}).get("rendered", "")
    # Extract member profile links, preserve order.
    links = re.findall(r'href="(https://workingfamilies\.org\.uk/member-profile/[^"]+)"', content)
    seen = set()
    names = []
    for link in links:
        slug = link.rstrip("/").split("/")[-1]
        if not slug or slug in seen:
            continue
        seen.add(slug)
        name = slug.replace("-", " ").title()
        names.append(name)
    results = []
    rank = 1
    for name in names:
        results.append((rank, name))
        rank += 1
        if rank > 30:
            break
    return results


def score_from_rank(rank: int, top: int) -> int:
    if rank <= max(10, top // 5):
        return 5
    if rank <= max(25, top // 2):
        return 4
    return 3


def main() -> int:
    rows: Dict[str, Dict[str, object]] = {}
    columns = [
        "Company",
        "HQ_Location",
        "Sector",
        "Website",
        "General_Contact_Email",
        "General_Contact_Phone",
        "HR_LD_Contact_Name",
        "HR_LD_Contact_Title",
        "HR_LD_Contact_Email",
        "HR_LD_Contact_Phone",
        "LinkedIn_URL",
        "Large_Budget_Score",
        "Parent_Employees_Score",
        "HR_LD_Activity_Score",
        "Employee_Satisfaction_Score",
        "Overall_Score",
        "Large_Budget_Evidence",
        "Parent_Employees_Evidence",
        "HR_LD_Evidence",
        "Employee_Satisfaction_Evidence",
        "Data_Source_Notes",
        "Date_Contacted",
        "Follow_Up_Date_1",
        "Follow_Up_Date_2",
        "Follow_Up_Date_3",
    ]

    def get_row(name: str) -> Dict[str, object]:
        key = normalize_name(name)
        if not key:
            key = name.lower().strip()
        if key not in rows:
            rows[key] = {col: "" for col in columns}
            rows[key]["Company"] = name.strip()
        return rows[key]

    sources_used: List[str] = []
    output_path = "parenting_workshop_leads_london.xlsx"
    use_existing = os.path.exists(output_path) and os.getenv("REFRESH_LISTS", "0") != "1"

    if use_existing:
        try:
            existing = pd.read_excel(output_path, sheet_name="Leads")
            for col in columns:
                if col not in existing.columns:
                    existing[col] = ""
            for _, r in existing.iterrows():
                name = str(r.get("Company", "")).strip()
                if not name:
                    continue
                row = get_row(name)
                for col in columns:
                    val = r.get(col, "")
                    if pd.isna(val):
                        val = ""
                    row[col] = val
        except Exception as exc:
            print(f"Warning: Failed to load existing lead sheet, rebuilding: {exc}", file=sys.stderr)
            use_existing = False

    if not use_existing:
        # 1) London-based companies (large candidate list)
        try:
            london_members = wikipedia_category_members("Companies_based_in_London", max_pages=10)
            sources_used.append("https://en.wikipedia.org/wiki/Category:Companies_based_in_London")
            for name in london_members:
                row = get_row(name)
                if not row["HQ_Location"]:
                    row["HQ_Location"] = "London, UK"
                if "Wikipedia: London companies" not in row["Data_Source_Notes"]:
                    row["Data_Source_Notes"] = (row["Data_Source_Notes"] + "; " if row["Data_Source_Notes"] else "") + "Wikipedia: London companies"
        except Exception as exc:
            print(f"Warning: Failed to load London companies: {exc}", file=sys.stderr)

        # 2) FTSE 100 constituents (large budget + sector)
        try:
            ftse100_url = "https://en.wikipedia.org/wiki/FTSE_100_Index"
            ftse100 = read_html_table(ftse100_url, ["company"])
            sources_used.append(ftse100_url)
            if ftse100 is not None:
                sector_col = None
                for col in ftse100.columns:
                    if "sector" in str(col).lower():
                        sector_col = col
                        break
                for _, r in ftse100.iterrows():
                    name = str(r["Company"]).strip()
                    row = get_row(name)
                    if sector_col and not row["Sector"]:
                        row["Sector"] = str(r[sector_col]).strip()
                    row["Large_Budget_Score"] = 5
                    row["Large_Budget_Evidence"] = "FTSE 100 constituent"
                    row["Data_Source_Notes"] = (row["Data_Source_Notes"] + "; " if row["Data_Source_Notes"] else "") + "FTSE 100 list"
        except Exception as exc:
            print(f"Warning: Failed to load FTSE 100: {exc}", file=sys.stderr)

        # 3) FTSE 250 constituents
        try:
            ftse250_url = "https://en.wikipedia.org/wiki/FTSE_250_Index"
            ftse250 = read_html_table(ftse250_url, ["company"])
            sources_used.append(ftse250_url)
            if ftse250 is not None:
                sector_col = None
                for col in ftse250.columns:
                    if "sector" in str(col).lower():
                        sector_col = col
                        break
                for _, r in ftse250.iterrows():
                    name = str(r["Company"]).strip()
                    row = get_row(name)
                    if sector_col and not row["Sector"]:
                        row["Sector"] = str(r[sector_col]).strip()
                    if row["Large_Budget_Score"] == "":
                        row["Large_Budget_Score"] = 4
                        row["Large_Budget_Evidence"] = "FTSE 250 constituent"
                    row["Data_Source_Notes"] = (row["Data_Source_Notes"] + "; " if row["Data_Source_Notes"] else "") + "FTSE 250 list"
        except Exception as exc:
            print(f"Warning: Failed to load FTSE 250: {exc}", file=sys.stderr)

        # 4) Largest UK companies by revenue
        try:
            largest_url = "https://en.wikipedia.org/wiki/List_of_largest_companies_in_the_United_Kingdom"
            largest = read_html_table(largest_url, ["company", "headquarters"])
            sources_used.append(largest_url)
            if largest is not None:
                for _, r in largest.iterrows():
                    name = str(r["Company"]).strip()
                    row = get_row(name)
                    hq = str(r.get("Headquarters", "")).strip()
                    if hq and not row["HQ_Location"]:
                        row["HQ_Location"] = hq
                    row["Large_Budget_Score"] = 5
                    row["Large_Budget_Evidence"] = "UK largest companies by revenue"
                    row["Data_Source_Notes"] = (row["Data_Source_Notes"] + "; " if row["Data_Source_Notes"] else "") + "UK largest companies list"
        except Exception as exc:
            print(f"Warning: Failed to load largest UK companies list: {exc}", file=sys.stderr)

    def score_value(v):
        try:
            return int(v)
        except Exception:
            return 0

    # 4b) Fetch official websites for companies we already have.
    try:
        rows_for_websites = list(rows.values())
        if WEBSITE_ENRICH_LIMIT > 0 and len(rows_for_websites) > WEBSITE_ENRICH_LIMIT:
            rows_for_websites = sorted(
                rows_for_websites,
                key=lambda r: -(
                    score_value(r.get("Large_Budget_Score"))
                    + score_value(r.get("Employee_Satisfaction_Score"))
                    + score_value(r.get("Parent_Employees_Score"))
                ),
            )[:WEBSITE_ENRICH_LIMIT]
        titles = [row["Company"] for row in rows_for_websites if not row["Website"]]
        websites: Dict[str, str] = {}
        try:
            qid_by_name: Dict[str, str] = {}
            base = "https://en.wikipedia.org/w/api.php"
            for i in range(0, len(titles), 50):
                chunk = titles[i : i + 50]
                params = {
                    "action": "query",
                    "format": "json",
                    "titles": "|".join(chunk),
                    "prop": "pageprops",
                    "ppprop": "wikibase_item",
                    "redirects": "1",
                }
                url = f"{base}?{urllib.parse.urlencode(params)}"
                data = fetch_json(url, retries=3, backoff=2.0)
                pages = data.get("query", {}).get("pages", {})
                for page in pages.values():
                    if "missing" in page:
                        continue
                    title = page.get("title", "")
                    qid = page.get("pageprops", {}).get("wikibase_item")
                    if title and qid:
                        qid_by_name[normalize_name(title)] = qid
                time.sleep(0.2)
            qids = list(set(qid_by_name.values()))
            websites = wikidata_official_websites_from_qids(qids)
            for row in rows_for_websites:
                if row["Website"]:
                    continue
                qid = qid_by_name.get(normalize_name(row["Company"]))
                if qid:
                    site = websites.get(qid)
                    if site:
                        row["Website"] = site
        except Exception as exc:
            print(f"Warning: Wikidata website lookup failed, falling back to Wikipedia HTML: {exc}", file=sys.stderr)

        # Fallback for any remaining rows without websites.
        for row in rows_for_websites:
            if row["Website"]:
                continue
            try:
                site = wikipedia_official_website(row["Company"])
            except Exception:
                time.sleep(2.0)
                continue
            if site:
                row["Website"] = site
            time.sleep(1.0)
    except Exception as exc:
        print(f"Warning: Website enrichment failed: {exc}", file=sys.stderr)

        # 5) Glassdoor Best Places to Work UK 2026 (employee satisfaction proxy)
        try:
            glassdoor_list = parse_glassdoor_bptw_uk_2026()
            if glassdoor_list:
                sources_used.append("https://www.glassdoor.co.uk/Award/Best-Places-to-Work-UK-LST_KQ0%2C22.htm")
            for rank, name in glassdoor_list:
                row = get_row(name)
                row["Employee_Satisfaction_Score"] = score_from_rank(rank, 50)
                row["Employee_Satisfaction_Evidence"] = f"Glassdoor Best Places to Work UK 2026 (Rank #{rank})"
                row["Data_Source_Notes"] = (row["Data_Source_Notes"] + "; " if row["Data_Source_Notes"] else "") + "Glassdoor BPTW UK 2026"
        except Exception as exc:
            print(f"Warning: Failed to load Glassdoor list: {exc}", file=sys.stderr)

        # 6) Working Families Top Employers 2025 (parenting-friendly proxy)
        try:
            wf_list = parse_working_families_top_employers_2025()
            if wf_list:
                sources_used.append("https://workingfamilies.org.uk/wp-json/wp/v2/pages?slug=top-30-employers")
                sources_used.append("https://workingfamilies.org.uk/employers/benchmark/top-30-employers/")
            for rank, name in wf_list:
                row = get_row(name)
                row["Parent_Employees_Score"] = score_from_rank(rank, 30)
                row["Parent_Employees_Evidence"] = f"Working Families Top Employers 2025 (Rank #{rank})"
                row["Data_Source_Notes"] = (row["Data_Source_Notes"] + "; " if row["Data_Source_Notes"] else "") + "Working Families Top Employers 2025"
        except Exception as exc:
            print(f"Warning: Failed to load Working Families list: {exc}", file=sys.stderr)

    # Build DataFrame
    data = list(rows.values())
    df = pd.DataFrame(data, columns=columns)

    # If a company has no large-budget signal and is London-based, set a conservative baseline.
    def set_baseline_budget(row):
        if row["Large_Budget_Score"] == "" and row["HQ_Location"].startswith("London"):
            return 2
        return row["Large_Budget_Score"]

    df["Large_Budget_Score"] = df.apply(set_baseline_budget, axis=1)

    # Sort by signals then name.
    df["_score_sort"] = (
        df["Large_Budget_Score"].apply(score_value)
        + df["Employee_Satisfaction_Score"].apply(score_value)
        + df["Parent_Employees_Score"].apply(score_value)
    )
    df = df.sort_values(by=["_score_sort", "Company"], ascending=[False, True]).drop(columns=["_score_sort"])

    # 5) Scrape general contact email/phone for the top-ranked subset to keep runtime reasonable.
    if CONTACT_SCRAPE_LIMIT > 0:
        top_rows = df.head(CONTACT_SCRAPE_LIMIT)
        for idx, row in top_rows.iterrows():
            if row["General_Contact_Email"] or row["General_Contact_Phone"]:
                continue
            website = row["Website"]
            if website and str(website).startswith(("http://", "https://")):
                email, phone = scrape_contact_info(website)
                if email:
                    df.at[idx, "General_Contact_Email"] = email
                if phone:
                    df.at[idx, "General_Contact_Phone"] = phone
                time.sleep(0.4)

    # Write Excel with an Overall_Score formula.
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Leads")
        rubric = pd.DataFrame(
            [
                {
                    "Category": "Large_Budget_Score",
                    "Logic": "5 if FTSE 100 or top revenue list; 4 if FTSE 250; 2 baseline if London-based with no signal.",
                },
                {
                    "Category": "Parent_Employees_Score",
                    "Logic": "Based on Working Families Top Employers 2025 rank when available; blank otherwise.",
                },
                {
                    "Category": "HR_LD_Activity_Score",
                    "Logic": "Blank; fill manually when you confirm active HR/L&D programs or roles.",
                },
                {
                    "Category": "Employee_Satisfaction_Score",
                    "Logic": "Based on Glassdoor Best Places to Work UK 2026 rank when available; blank otherwise.",
                },
                {
                    "Category": "Overall_Score",
                    "Logic": "Average of available scores; updates automatically when you fill missing scores.",
                },
            ]
        )
        rubric.to_excel(writer, index=False, sheet_name="Scoring_Rubric")

    # Add formulas for Overall_Score after writing.
    from openpyxl import load_workbook
    from openpyxl.worksheet.table import Table, TableStyleInfo

    wb = load_workbook(output_path)
    ws = wb["Leads"]
    # Find columns by header.
    headers = {cell.value: idx for idx, cell in enumerate(ws[1], start=1)}
    score_cols = [
        headers.get("Large_Budget_Score"),
        headers.get("Parent_Employees_Score"),
        headers.get("HR_LD_Activity_Score"),
        headers.get("Employee_Satisfaction_Score"),
    ]
    overall_col = headers.get("Overall_Score")
    if all(score_cols) and overall_col:
        for row in range(2, ws.max_row + 1):
            # Build range like L2:O2
            col_letters = [ws.cell(row=1, column=c).column_letter for c in score_cols]
            score_range = f"{col_letters[0]}{row}:{col_letters[-1]}{row}"
            ws.cell(row=row, column=overall_col).value = f"=IF(COUNT({score_range})=0,\"\",ROUND(AVERAGE({score_range}),2))"

    # Make the sheet sortable/filterable as an Excel table and freeze headers.
    last_col_letter = ws.cell(row=1, column=ws.max_column).column_letter
    table_ref = f"A1:{last_col_letter}{ws.max_row}"
    table = Table(displayName="LeadsTable", ref=table_ref)
    style = TableStyleInfo(
        name="TableStyleMedium9",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    table.tableStyleInfo = style
    ws.add_table(table)
    ws.auto_filter.ref = table_ref
    ws.freeze_panes = "A2"
    wb.save(output_path)

    # Save sources used for transparency.
    with open("sources_used.txt", "w", encoding="utf-8") as f:
        for url in sorted(set(sources_used)):
            f.write(url + "\n")

    print(f"Wrote {output_path} with {len(df)} companies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
