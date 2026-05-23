

import os
import sys
import io
import time
import logging
import requests
import datetime
import re
import json
import pandas as pd
from collections import defaultdict
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()


SUPABASE_URL    = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY    = os.getenv("SUPABASE_KEY", "")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "company-documents")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise EnvironmentError(
        "Missing SUPABASE_URL or SUPABASE_KEY in .env\n"
        "Create a .env file:\n"
        "  SUPABASE_URL=https://your-project.supabase.co\n"
        "  SUPABASE_KEY=your-service-role-key\n"
        "  SUPABASE_BUCKET=company-documents\n"
    )

SUPABASE = create_client(SUPABASE_URL, SUPABASE_KEY)


try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

log = logging.getLogger(__name__)



COMPANY_CATALOGUE = {
    "1": {"name": "Steel Authority of India Ltd",     "bse_code": "500113", "nse_symbol": "SAIL",        "yfinance_symbol": "SAIL.NS",       "ipo_date": datetime.date(1995, 7, 1)},
    "2": {"name": "Wipro Ltd",                        "bse_code": "507685", "nse_symbol": "WIPRO",       "yfinance_symbol": "WIPRO.NS",       "ipo_date": datetime.date(1946, 1, 1)},
    "3": {"name": "Oil and Natural Gas Corporation",  "bse_code": "500312", "nse_symbol": "ONGC",        "yfinance_symbol": "ONGC.NS",        "ipo_date": datetime.date(1993, 8, 1)},
    "4": {"name": "NIBE Ltd",                         "bse_code": "535136", "nse_symbol": "NIBE",        "yfinance_symbol": "NIBE.NS",        "ipo_date": datetime.date(2025, 2, 7)},
    "5": {"name": "Adani Power Ltd",                  "bse_code": "533096", "nse_symbol": "ADANIPOWER",  "yfinance_symbol": "ADANIPOWER.NS",  "ipo_date": datetime.date(2009, 8, 20)},
}


BSE_CODE_MAP = {v["bse_code"]: v for v in COMPANY_CATALOGUE.values()}

TODAY          = datetime.date.today()
FIVE_YEARS_AGO = TODAY - datetime.timedelta(days=5 * 365)
MIN_PDF_BYTES  = 50_000



BSE_SUBCATEGORY_MAP = {
    "Quarterly Results":             "Financial Results",
    "Annual Report":                 "Annual Report",
    "Press Releases":                "Press Release / Media Release",
    "Board Meeting Outcomes":        "Outcome of Board Meeting",
    "Shareholding Pattern":          "Shareholding Pattern",
    "Corporate Governance Report":   "Corporate Governance Report",
    "Stock Exchange Filings Reg30":  "Outcome of Board Meeting",
    "Insider Trading Disclosures":   "Insider Trading / SAST",
    "Related Party Transactions":    "Related Party",
    "Compliance Certificates":       "Compliance Report",
    "AGM Notices":                   "AGM/EGM/Court Convened Meeting",
    "Postal Ballot Notices":         "Postal Ballot",
    "Scheme of Arrangement Mergers": "Scheme of Arrangement",
    "Investor Presentations":        "Analyst / Investor Meet",
    "Earnings Call Transcripts":     "Analyst / Investor Meet",
    "Shareholding Changes":          "Shareholding Pattern",
}

KEYWORD_FILTER = {
    "Quarterly Results":            ["financial result", "audited", "unaudited", "standalone", "consolidated", "march 31", "june 30", "september 30", "december 31"],
    "Annual Report":                ["annual report"],
    "Board Meeting Outcomes":       ["board meeting", "board of directors"],
    "Press Releases":               ["press release", "media release"],
    "Shareholding Pattern":         ["shareholding pattern"],
    "Investor Presentations":       ["investor", "analyst", "presentation"],
    "Earnings Call Transcripts":    ["earnings call", "analyst call", "transcript", "concall", "con. call"],
    "AGM Notices":                  ["agm", "annual general meeting"],
    "Postal Ballot Notices":        ["postal ballot"],
    "Compliance Certificates":      ["compliance"],
    "Insider Trading Disclosures":  ["insider trading", "sast"],
    "Related Party Transactions":   ["related party"],
    "Stock Exchange Filings Reg30": ["regulation 30", "reg. 30", "outcome of board"],
    "Corporate Governance Report":  ["corporate governance"],
}

ALL_DOCUMENTS = {
    "1":  {"name": "Quarterly Results",             "category": "FinancialStatements"},
    "2":  {"name": "Annual Report",                 "category": "FinancialStatements"},
    "7":  {"name": "Investor Presentations",        "category": "CorporateFilings"},
    "8":  {"name": "Earnings Call Transcripts",     "category": "CorporateFilings"},
    "9":  {"name": "Press Releases",                "category": "CorporateFilings"},
    "10": {"name": "Board Meeting Outcomes",        "category": "CorporateFilings"},
    "11": {"name": "Shareholding Pattern",          "category": "CorporateFilings"},
    "12": {"name": "Corporate Governance Report",   "category": "CorporateFilings"},
    "13": {"name": "Stock Exchange Filings Reg30",  "category": "RegulatoryFilings"},
    "14": {"name": "Insider Trading Disclosures",   "category": "RegulatoryFilings"},
    "15": {"name": "Related Party Transactions",    "category": "RegulatoryFilings"},
    "16": {"name": "Compliance Certificates",       "category": "RegulatoryFilings"},
    "17": {"name": "AGM Notices",                   "category": "StrategicDocuments"},
    "18": {"name": "Postal Ballot Notices",         "category": "StrategicDocuments"},
    "19": {"name": "Scheme of Arrangement Mergers", "category": "StrategicDocuments"},
    "21": {"name": "Historical Prices",             "category": "StockData"},
    "22": {"name": "Bulk and Block Deals",          "category": "StockData"},
    "23": {"name": "Shareholding Changes",          "category": "CorporateFilings"},
}

DEFAULT_DOC_IDS = list(ALL_DOCUMENTS.keys())



def ensure_bucket():
    """Create the Supabase bucket if it doesn't already exist."""
    try:
        buckets  = SUPABASE.storage.list_buckets()
        existing = [b.name for b in buckets]
        if SUPABASE_BUCKET in existing:
            return
        log.info(f"Creating Supabase bucket '{SUPABASE_BUCKET}'...")
        SUPABASE.storage.create_bucket(
            SUPABASE_BUCKET,
            options={
                "public":             False,
                "file_size_limit":    104857600,
                "allowed_mime_types": ["application/pdf", "text/csv"],
            }
        )
        log.info(f"Bucket '{SUPABASE_BUCKET}' created.")
    except Exception as e:
        if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
            return
        log.error(f"Bucket error: {e}")
        raise


def upload_to_supabase(content: bytes, storage_path: str,
                       content_type: str = "application/pdf",
                       counters: dict = None) -> bool:
    """Upload bytes directly to Supabase. Returns True on success/skip."""
    try:
        
        SUPABASE.storage.from_(SUPABASE_BUCKET).upload(
            path=storage_path,
            file=content,               # ← just pass bytes
            file_options={"content-type": content_type, "x-upsert": "false"},
        )
        kb = len(content) // 1024
        log.info(f"Uploaded: {storage_path} ({kb} KB)")
        if counters is not None:
            counters["uploaded"] = counters.get("uploaded", 0) + 1
        return True

    except Exception as e:
        err = str(e).lower()
        if "already exists" in err or "duplicate" in err or "409" in err:
            log.debug(f"Skip (exists): {storage_path}")
            if counters is not None:
                counters["skipped"] = counters.get("skipped", 0) + 1
            return True
        log.error(f"Upload failed: {storage_path} | {e}")
        if counters is not None:
            counters["failed"] = counters.get("failed", 0) + 1
        return False



def analyse_filing_dates(bse_code: str, all_filings: list):
    """
    Analyses historical filing metadata to find which months a company
    typically releases documents.  Stores result in `filing_calendar`.

    Table schema (create once in Supabase):
        CREATE TABLE filing_calendar (
            id            BIGSERIAL PRIMARY KEY,
            bse_code      TEXT NOT NULL,
            company_name  TEXT,
            active_months JSONB,        -- e.g. [1, 4, 7, 10]
            next_expected DATE,
            last_analysed TIMESTAMPTZ DEFAULT now()
        );
        CREATE UNIQUE INDEX ON filing_calendar (bse_code);
    """
    if not all_filings:
        return

    month_counts = defaultdict(int)
    for f in all_filings:
        raw_dt = f.get("NEWS_DT", "")
        parsed = _parse_bse_date(raw_dt)
        if parsed:
            try:
                month = datetime.datetime.strptime(parsed, "%Y%m%d").month
                month_counts[month] += 1
            except Exception:
                pass

    if not month_counts:
        return

    
    total = sum(month_counts.values())
    active_months = sorted(
        m for m, cnt in month_counts.items()
        if (cnt / total) >= 0.10
    )

    # Next expected date = nearest upcoming active month
    next_expected = _next_active_date(active_months)

    catalogue_entry = BSE_CODE_MAP.get(bse_code, {})
    company_name    = catalogue_entry.get("name", bse_code)

    try:
        SUPABASE.table("filing_calendar").upsert(
            {
                "bse_code":      bse_code,
                "company_name":  company_name,
                "active_months": active_months,
                "next_expected": next_expected.isoformat() if next_expected else None,
                "last_analysed": datetime.datetime.utcnow().isoformat(),
            },
            on_conflict="bse_code"
        ).execute()
        log.info(f"Filing calendar saved for {bse_code}: active months={active_months}, next={next_expected}")
    except Exception as e:
        log.warning(f"Could not save filing calendar for {bse_code}: {e}")


def _next_active_date(active_months: list) -> datetime.date | None:
    """Return the 1st day of the nearest upcoming active month."""
    if not active_months:
        return None
    today = datetime.date.today()
    for offset in range(13):
        candidate = today + datetime.timedelta(days=offset * 28)
        if candidate.month in active_months and candidate > today:
            return candidate.replace(day=1)
    return None


def get_filing_calendar(bse_code: str) -> dict | None:
    """Fetch filing calendar row for a company. Used by server.py scheduler."""
    try:
        res = SUPABASE.table("filing_calendar") \
            .select("*") \
            .eq("bse_code", bse_code) \
            .execute()
        return res.data[0] if res.data else None
    except Exception as e:
        log.warning(f"get_filing_calendar({bse_code}): {e}")
        return None


def get_all_filing_calendars() -> list:
    """Returns all filing calendar rows. Used by server.py smart scheduler."""
    try:
        res = SUPABASE.table("filing_calendar").select("*").execute()
        return res.data or []
    except Exception as e:
        log.warning(f"get_all_filing_calendars: {e}")
        return []



def _make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.bseindia.com/",
        "Origin":          "https://www.bseindia.com",
    })
    return s

SESSION_API = _make_session()
SESSION_PDF = _make_session()
SESSION_PDF.headers.update({"Accept": "application/pdf,application/octet-stream,*/*"})


def _warm_up_bse():
    try:
        SESSION_API.get("https://www.bseindia.com", timeout=15)
        time.sleep(2)
    except Exception:
        pass


def _safe_get(session, url, params=None, retries=3, delay=4, timeout=30):
    for attempt in range(retries):
        try:
            resp = session.get(url, params=params, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200:
                return resp
        except Exception as e:
            log.debug(f"HTTP attempt {attempt+1} failed: {e}")
        time.sleep(delay)
    return None


def _is_valid_pdf(content):
    if len(content) < MIN_PDF_BYTES:
        return False, f"Too small ({len(content)} bytes)"
    if not content[:5].startswith(b"%PDF"):
        return False, f"Not a PDF"
    return True, "OK"


def _safe_filename(text, max_len=60):
    text = re.sub(r'[\\/*?:"<>|]', "", str(text))
    text = re.sub(r'\s+', "_", text.strip())
    return text[:max_len]


def _parse_bse_date(raw):
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%Y%m%d", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(str(raw)[:10], fmt).strftime("%Y%m%d")
        except Exception:
            continue
    return ""


def _pdf_urls(att_name):
    return [
        f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{att_name}",
        f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{att_name}",
    ]

# ============================================================
# BSE API — fetch filings metadata
# ============================================================

def _fetch_bse_filings(bse_code: str, doc_name: str, from_dt: str,
                       to_dt: str, max_records=2000) -> list:
    subcategory = BSE_SUBCATEGORY_MAP.get(doc_name, "")
    url         = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    all_filings = []
    page        = 1

    while True:
        params = {
            "strCat":      "",
            "strPrevDate": from_dt,
            "strScrip":    bse_code,
            "strSearch":   "P",
            "strToDate":   to_dt,
            "strType":     "C",
            "subcategory": subcategory,
            "PageNo":      page,
            "NoOfRec":     50,
        }
        resp = _safe_get(SESSION_API, url, params=params)
        if not resp:
            break

        try:
            data    = resp.json()
            filings = data.get("Table", data.get("Table1", []))
            if not filings:
                break

            for f in filings:
                news_dt = _parse_bse_date(f.get("NEWS_DT", ""))
                if news_dt and news_dt < from_dt:
                    return all_filings
                all_filings.append(f)

            if len(all_filings) >= max_records:
                break

            page += 1
            time.sleep(1.5)

        except Exception as e:
            log.warning(f"BSE API page {page} error: {e}")
            break

    # Apply keyword filter
    kws = KEYWORD_FILTER.get(doc_name, [])
    if kws:
        return [f for f in all_filings if any(k in str(f.get("NEWSSUB","")).lower() for k in kws)]

    return all_filings

# ============================================================
# DOWNLOADERS  (all return collected filings for calendar analysis)
# ============================================================

def _download_bse_api(company: dict, doc_name: str, category: str,
                      from_dt: str, to_dt: str, company_prefix: str,
                      counters: dict, max_files=2000) -> list:
    """Download via BSE API, upload to Supabase. Returns raw filings for calendar."""
    filings = _fetch_bse_filings(company["bse_code"], doc_name, from_dt, to_dt)
    if not filings:
        log.info(f"  {doc_name}: 0 results from BSE API")
        return []

    log.info(f"  {doc_name}: {len(filings)} filings found")

    for filing in filings[:max_files]:
        att_name = filing.get("ATTACHMENTNAME", "").strip()
        news_dt  = str(filing.get("NEWS_DT", ""))[:10].replace("-","").replace("/","")
        news_id  = str(filing.get("NEWSID",""))[:8]
        headline = _safe_filename(filing.get("NEWSSUB", doc_name), 50)

        if not att_name:
            continue

        fname        = f"{news_dt}_{news_id}_{headline}.pdf"
        storage_path = f"{company_prefix}/{category}/{fname}"

        for pdf_url in _pdf_urls(att_name):
            resp = _safe_get(SESSION_PDF, pdf_url, timeout=90)
            if not resp:
                continue
            valid, _ = _is_valid_pdf(resp.content)
            if not valid:
                continue
            upload_to_supabase(resp.content, storage_path, "application/pdf", counters)
            break
        else:
            counters["failed"] = counters.get("failed", 0) + 1

        time.sleep(1.5)

    return filings   # return raw filings for calendar analysis


def _download_annual_report(company: dict, category: str,
                             from_dt: str, to_dt: str, company_prefix: str,
                             counters: dict) -> list:
    params = {
        "strCat": "", "strPrevDate": from_dt, "strScrip": company["bse_code"],
        "strSearch": "P", "strToDate": to_dt, "strType": "C",
        "subcategory": "Annual Report", "PageNo": 1, "NoOfRec": 20,
    }
    resp = _safe_get(SESSION_API,
                     "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w",
                     params=params)
    if not resp:
        return []

    filings = resp.json().get("Table", [])
    for filing in filings:
        att_name = filing.get("ATTACHMENTNAME", "").strip()
        news_dt  = _parse_bse_date(filing.get("NEWS_DT", ""))
        year     = news_dt[:4] if news_dt else "Unknown"
        headline = _safe_filename(filing.get("NEWSSUB", "AnnualReport"), 40)
        if not att_name:
            continue
        fname        = f"AnnualReport_{year}_{headline}.pdf"
        storage_path = f"{company_prefix}/{category}/{fname}"
        for pdf_url in [
            f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{att_name}",
            f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{att_name}",
        ]:
            resp2 = _safe_get(SESSION_PDF, pdf_url, timeout=120)
            if not resp2:
                continue
            valid, _ = _is_valid_pdf(resp2.content)
            if not valid:
                continue
            upload_to_supabase(resp2.content, storage_path, "application/pdf", counters)
            break
        time.sleep(2)

    return filings


def _download_historical_prices(company: dict, category: str,
                                 company_prefix: str, counters: dict,
                                 from_date: datetime.date):
    if not YFINANCE_AVAILABLE:
        log.warning("yfinance not installed — skipping historical prices")
        return
    try:
        df = yf.Ticker(company["yfinance_symbol"]).history(
            start=from_date.strftime("%Y-%m-%d"),
            end=TODAY.strftime("%Y-%m-%d")
        )
        if df.empty:
            return
        csv_bytes    = df.to_csv().encode("utf-8")
        fname        = f"HistoricalPrices_{company['nse_symbol']}_5yr.csv"
        storage_path = f"{company_prefix}/{category}/{fname}"
        upload_to_supabase(csv_bytes, storage_path, "text/csv", counters)
    except Exception as e:
        log.warning(f"yfinance error: {e}")


def _download_bulk_deals(company: dict, category: str,
                          from_dt: str, to_dt: str,
                          company_prefix: str, counters: dict):
    resp = _safe_get(SESSION_API,
        "https://api.bseindia.com/BseIndiaAPI/api/BulkDealData/w",
        params={"strScrip": company["bse_code"], "strPrevDate": from_dt, "strToDate": to_dt}
    )
    if not resp:
        return
    try:
        rows = resp.json().get("Table", [])
        if rows:
            csv_bytes    = pd.DataFrame(rows).to_csv(index=False).encode("utf-8")
            fname        = f"BulkBlockDeals_{company['nse_symbol']}_5yr.csv"
            storage_path = f"{company_prefix}/{category}/{fname}"
            upload_to_supabase(csv_bytes, storage_path, "text/csv", counters)
    except Exception as e:
        log.warning(f"Bulk deals error: {e}")


def _download_selenium(company: dict, doc_name: str, category: str,
                        company_prefix: str, counters: dict):
    """Selenium fallback — used when BSE API returns 0 results."""
    if not SELENIUM_AVAILABLE:
        log.warning(f"Selenium not available for {doc_name}")
        return

    anntype_map = {
        "Quarterly Results":         "9",
        "Investor Presentations":    "30",
        "Earnings Call Transcripts": "30",
        "Board Meeting Outcomes":    "7",
        "Press Releases":            "8",
        "AGM Notices":               "3",
    }
    ann_url  = (f"https://www.bseindia.com/corporates/ann.html"
                f"?scrip={company['bse_code']}&dur=A"
                f"&anntype={anntype_map.get(doc_name,'')}")

    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = None
    try:
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options
        )
        driver.get("https://www.bseindia.com")
        time.sleep(5)
        driver.get(ann_url)
        try:
            WebDriverWait(driver, 25).until(
                EC.presence_of_element_located((
                    By.XPATH,
                    "//a[contains(@href,'AttachLive') or contains(@href,'AttachHis')]"
                ))
            )
        except Exception:
            time.sleep(5)

        all_links = driver.find_elements(
            By.XPATH,
            "//a[contains(@href,'AttachLive') or contains(@href,'AttachHis')]"
        )
        cookies   = {c["name"]: c["value"] for c in driver.get_cookies()}
        today_str = TODAY.strftime("%Y%m%d")

        for i, link in enumerate(all_links[:50]):
            href = link.get_attribute("href") or ""
            if not href or "javascript" in href:
                continue
            try:
                row      = link.find_element(By.XPATH, "./ancestor::tr")
                row_text = row.text.replace("\n"," ").strip()
                dm       = re.search(r'(\d{2}[/-]\d{2}[/-]\d{4})', row_text)
                fdate    = dm.group(1).replace("/","").replace("-","") if dm else today_str
                headline = _safe_filename(row_text[:55])
            except Exception:
                fdate    = today_str
                headline = _safe_filename(doc_name)

            fname        = f"{fdate}_{i+1:02d}_{headline}.pdf"
            storage_path = f"{company_prefix}/{category}/{fname}"

            resp = requests.get(
                href, cookies=cookies,
                headers={"User-Agent":"Mozilla/5.0","Referer":"https://www.bseindia.com/"},
                timeout=90, allow_redirects=True
            )
            valid, _ = _is_valid_pdf(resp.content)
            if not valid:
                continue
            upload_to_supabase(resp.content, storage_path, "application/pdf", counters)
            time.sleep(1.5)

    except Exception as e:
        log.error(f"Selenium error for {doc_name}: {e}")
    finally:
        if driver:
            driver.quit()

# ============================================================
# PUBLIC API  ←  called by server.py
# ============================================================

def run_scraping(
    company_name: str,
    doc_ids: list | None = None,
) -> dict:
    """
    Main entry point called by server.py.

    Parameters
    ----------
    company_name : str
        Must match a key in COMPANY_CATALOGUE by name, OR be a BSE code.
        Example: "Reliance Industries Ltd" or "500325"
    doc_ids : list | None
        Subset of ALL_DOCUMENTS keys to scrape.
        None → scrape everything.

    Returns
    -------
    dict with keys:
        company, uploaded, skipped, failed, calendar_months, next_expected
    """
    # ── Resolve company ──────────────────────────────────────
    company = _resolve_company(company_name)
    if not company:
        log.error(f"run_scraping: unknown company '{company_name}'")
        return {"error": f"Unknown company: {company_name}"}

    # ── Date range ───────────────────────────────────────────
    ipo_date   = company["ipo_date"]
    start_date = max(FIVE_YEARS_AGO, ipo_date)
    from_dt    = start_date.strftime("%Y%m%d")
    to_dt      = TODAY.strftime("%Y%m%d")

    # ── Supabase prefix ──────────────────────────────────────
    company_prefix = re.sub(r'[^A-Za-z0-9]', '', company["name"].split()[0])

    # ── Ensure bucket exists ─────────────────────────────────
    ensure_bucket()

    # ── Warm up BSE ──────────────────────────────────────────
    _warm_up_bse()

    counters   = {"uploaded": 0, "skipped": 0, "failed": 0}
    all_filings_pool = []   # collect all filing metadata for calendar analysis
    selected   = doc_ids if doc_ids else DEFAULT_DOC_IDS

    log.info(f"[SCRAPE] {company['name']} | {from_dt}→{to_dt} | {len(selected)} doc types")

    for doc_id in selected:
        doc      = ALL_DOCUMENTS.get(doc_id)
        if not doc:
            continue
        name = doc["name"]
        cat  = doc["category"]

        log.info(f"  [{doc_id}] {name}")

        if name == "Historical Prices":
            _download_historical_prices(company, cat, company_prefix, counters, start_date)

        elif name == "Bulk and Block Deals":
            _download_bulk_deals(company, cat, from_dt, to_dt, company_prefix, counters)

        elif name == "Annual Report":
            filings = _download_annual_report(company, cat, from_dt, to_dt, company_prefix, counters)
            all_filings_pool.extend(filings)

        else:
            filings = _download_bse_api(company, name, cat, from_dt, to_dt, company_prefix, counters)
            if not filings:
                # Fallback to Selenium
                _download_selenium(company, name, cat, company_prefix, counters)
            else:
                all_filings_pool.extend(filings)

        time.sleep(2)

    # ── Analyse filing calendar from collected metadata ──────
    analyse_filing_dates(company["bse_code"], all_filings_pool)
    calendar = get_filing_calendar(company["bse_code"]) or {}

    result = {
        "company":         company["name"],
        "bse_code":        company["bse_code"],
        "uploaded":        counters["uploaded"],
        "skipped":         counters["skipped"],
        "failed":          counters["failed"],
        "calendar_months": calendar.get("active_months", []),
        "next_expected":   calendar.get("next_expected"),
    }
    log.info(f"[SCRAPE DONE] {result}")
    return result


def run_scraping_all(doc_ids: list | None = None) -> list:
    """
    Scrape ALL companies in the catalogue.
    Returns list of per-company result dicts.
    Used by server.py on startup.
    """
    results = []
    for key, company in COMPANY_CATALOGUE.items():
        log.info(f"=== Scraping {company['name']} ({key}/{len(COMPANY_CATALOGUE)}) ===")
        result = run_scraping(company["name"], doc_ids=doc_ids)
        results.append(result)
        time.sleep(5)   # brief pause between companies
    return results


def _resolve_company(identifier: str) -> dict | None:
    """
    Accepts company name (partial or full) or BSE code.
    Returns the matched catalogue dict or None.
    """
    identifier = identifier.strip()

    # Exact BSE code match
    if identifier in BSE_CODE_MAP:
        return BSE_CODE_MAP[identifier]

    # Full or partial name match (case-insensitive)
    ident_lower = identifier.lower()
    for entry in COMPANY_CATALOGUE.values():
        if ident_lower in entry["name"].lower() or entry["name"].lower() in ident_lower:
            return entry

    # Try first-word match (e.g. "Reliance" → "Reliance Industries Ltd")
    for entry in COMPANY_CATALOGUE.values():
        if entry["name"].split()[0].lower() == ident_lower.split()[0].lower():
            return entry

    return None

# ============================================================
# CLI ENTRYPOINT  (kept for standalone use)
# ============================================================

def _cli_select_company():
    print(f"\n{'='*65}")
    print(f"  SELECT COMPANY")
    print(f"{'='*65}")
    for key, co in COMPANY_CATALOGUE.items():
        data_yrs = min(5, TODAY.year - co["ipo_date"].year)
        note     = f"~{data_yrs} yr" if data_yrs < 5 else "5 yr"
        print(f"  {key:>2}. {co['name']:<40}  BSE:{co['bse_code']}  ({note})")
    
    raw = input("\nEnter number (1-5) or company name: ").strip()
    if raw in COMPANY_CATALOGUE:
        return COMPANY_CATALOGUE[raw]["name"]
    return raw   # pass as-is to _resolve_company


def _cli_select_docs():
    print(f"""
{'='*60}
 1. Financial Statements  (Quarterly Results, Annual Report)
 2. Corporate Filings
 3. Regulatory Filings
 4. Strategic Documents
 5. Stock Data
 6. ALL DOCUMENTS
 7. Custom
{'='*60}""")
    choice  = input("Choice (1-7): ").strip()
    cat_map = {
        "1": ["1","2"],
        "2": ["7","8","9","10","11","12","23"],
        "3": ["13","14","15","16"],
        "4": ["17","18","19"],
        "5": ["21","22"],
        "6": DEFAULT_DOC_IDS,
    }
    if choice in cat_map:
        return cat_map[choice]
    elif choice == "7":
        for k, v in ALL_DOCUMENTS.items():
            print(f"  {k:>2}. {v['name']}")
        raw = input("Enter doc IDs e.g. 1,2,7: ").strip()
        return [x.strip() for x in raw.split(",") if x.strip() in ALL_DOCUMENTS]
    return DEFAULT_DOC_IDS


def main():
    """Standalone CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    print("""
+------------------------------------------------------------+
|  MULTI-COMPANY DOCUMENT DOWNLOADER  [v13]                 |
|  • Uploads directly to Supabase (no local saving)        |
|  • Analyses filing calendar for smart re-scraping        |
+------------------------------------------------------------+
""")
    company_name = _cli_select_company()
    doc_ids      = _cli_select_docs()
    result       = run_scraping(company_name, doc_ids=doc_ids)

    print(f"""
{'='*55}
DONE
  Company       : {result.get('company')}
  Uploaded      : {result.get('uploaded')}
  Skipped       : {result.get('skipped')}
  Failed        : {result.get('failed')}
  Active months : {result.get('calendar_months')}
  Next expected : {result.get('next_expected')}
{'='*55}
""")


if __name__ == "__main__":
    main()