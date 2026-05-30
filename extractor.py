

import os
import json
import time
import tempfile
import logging
import threading
from google import genai
from google.genai import types
from supabase import create_client
from dotenv import load_dotenv

import notifier

load_dotenv()

log    = logging.getLogger(__name__)
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
BUCKET   = os.getenv("SUPABASE_BUCKET", "company-documents")

CATEGORIES = [
    "FinancialStatements",
    "CorporateFilings",
    "RegulatoryFilings",
    "StrategicDocuments",
    "StockData",
    "MCA_Documents",
]

# Max PDFs to send to Gemini per batch (to stay within context limits)
MAX_PDFS_PER_BATCH = 3


# ── Prompts per category ──────────────────────────────────────

FINANCIAL_PROMPT = """
You are a financial data extraction expert for Indian listed companies.
You are given multiple financial PDFs (quarterly results, annual reports, P&L, balance sheets, cash flow statements).

Extract and synthesize ALL years and quarters of data you can find across ALL the provided PDFs.

Return ONLY this JSON structure. No markdown. No explanation:

{
  "company_name": "",
  "bse_code": "",
  "nse_symbol": "",
  "face_value": null,
  "book_value": null,
  "quarterly_data": [
    {
      "quarter": "Q3FY26",
      "revenue": null,
      "expenses": null,
      "operating_profit": null,
      "opm_percent": null,
      "other_income": null,
      "interest": null,
      "depreciation": null,
      "profit_before_tax": null,
      "tax": null,
      "net_profit": null,
      "eps": null
    }
  ],
  "annual_pl": [
    {
      "year": "Mar 2025",
      "revenue": null,
      "expenses": null,
      "operating_profit": null,
      "opm_percent": null,
      "other_income": null,
      "interest": null,
      "depreciation": null,
      "profit_before_tax": null,
      "tax": null,
      "net_profit": null,
      "eps": null,
      "dividend_payout": null
    }
  ],
  "balance_sheet": [
    {
      "year": "Mar 2025",
      "share_capital": null,
      "reserves": null,
      "borrowings": null,
      "other_liabilities": null,
      "total_liabilities": null,
      "fixed_assets": null,
      "cwip": null,
      "investments": null,
      "other_assets": null,
      "total_assets": null
    }
  ],
  "cash_flow": [
    {
      "year": "Mar 2025",
      "operating": null,
      "investing": null,
      "financing": null,
      "net_cash_flow": null,
      "capex": null,
      "free_cash_flow": null
    }
  ],
  "ratios": [
    {
      "year": "Mar 2025",
      "debtor_days": null,
      "inventory_days": null,
      "days_payable": null,
      "cash_conversion_cycle": null,
      "working_capital_days": null,
      "roce": null,
      "roe": null,
      "debt_to_equity": null,
      "current_ratio": null,
      "interest_coverage": null,
      "net_margin": null,
      "gross_margin": null,
      "asset_turnover": null,
      "pe_ratio": null,
      "pb_ratio": null,
      "ev_ebitda": null,
      "dividend_yield": null
    }
  ]
}

Rules:
- All money in Indian Rupees Crores (Cr) unless stated otherwise
- Negative numbers for losses/outflows
- null if value not found anywhere in the documents
- Sort quarterly_data newest first
- Sort annual data newest first
- Include as many years/quarters as the PDFs contain
- Return ONLY the JSON, nothing else
"""

CORPORATE_PROMPT = """
You are a financial analyst extracting corporate information from Indian company filings.
You are given multiple documents: press releases, board meeting outcomes, investor presentations,
earnings call transcripts, shareholding patterns, annual report narratives.

Extract and return ONLY this JSON. No markdown. No explanation:

{
  "about": "",
  "business_description": "",
  "key_points": "",
  "management_outlook": "",
  "key_risks": "",
  "opportunities": "",
  "segments": [
    {
      "name": "",
      "percentage": null,
      "type": "revenue"
    }
  ],
  "geographic_segments": [
    {
      "region": "",
      "percentage": null
    }
  ],
  "shareholding": [
    {
      "quarter": "Dec 2024",
      "promoter": null,
      "fii": null,
      "dii": null,
      "public": null,
      "no_of_shareholders": null
    }
  ],
  "recent_announcements": [
    {
      "date": "",
      "title": "",
      "summary": ""
    }
  ],
  "products_and_platforms": "",
  "expansion_plans": "",
  "acquisitions": "",
  "partnerships": "",
  "employee_data": {
    "total_employees": null,
    "women_percent": null,
    "attrition_rate": null
  },
  "client_data": "",
  "awards_recognition": "",
  "esg_highlights": "",
  "ai_digital_initiatives": ""
}

Rules:
- about: 2-3 sentence company description
- key_points: detailed bullet points about business model, revenue breakup, competitive advantages
- management_outlook: direct quotes or paraphrase from management commentary
- key_risks: list the main risks mentioned
- segments: business segment revenue breakdown with percentages
- geographic_segments: geographic revenue breakdown
- Return ONLY the JSON, nothing else
"""

MERGE_PROMPT = """
You are merging two JSON objects about the same company into one complete company profile.
Combine all data from both objects. If both have a field, prefer the more detailed/complete value.
Do not lose any data from either object.

Object 1 (Financial Data):
{financial_json}

Object 2 (Corporate Data):
{corporate_json}

Return ONLY the merged JSON with ALL fields from both objects combined. No markdown. No explanation.
Also add these computed summary fields at the top level:
- "latest_revenue": most recent annual revenue
- "latest_net_profit": most recent annual net profit  
- "latest_net_margin": most recent net margin %
- "latest_roe": most recent ROE
- "latest_roce": most recent ROCE
- "latest_debt_to_equity": most recent D/E ratio
- "latest_year": most recent financial year (e.g. "Mar 2025")
- "latest_quarter": most recent quarter (e.g. "Q3FY26")
- "verdict": 3-4 sentence plain English financial health and business summary for retail investors
"""


# ── Storage helpers ───────────────────────────────────────────

def get_all_companies():
    """Returns list of company folder names from the bucket root."""
    try:
        items = supabase.storage.from_(BUCKET).list("")
        return [
            item["name"] for item in items
            if not item["name"].endswith(".pdf")
            and not item["name"].endswith(".csv")
        ]
    except Exception as e:
        log.error(f"get_all_companies: {e}")
        return []


CATEGORY_SIZE_THRESHOLD = {
    "FinancialStatements": 500_000,
    "CorporateFilings":    100_000,
    "RegulatoryFilings":    50_000,
    "StrategicDocuments":   50_000,
    "StockData":                 0,
    "MCA_Documents":        50_000,
}

def get_company_pdfs(company_name):
    by_category = {}
    for category in CATEGORIES:
        folder_path = f"{company_name}/{category}"
        try:
            files = supabase.storage.from_(BUCKET).list(
                folder_path,
                options={"limit": 1000, "offset": 0}
            )
            min_size = CATEGORY_SIZE_THRESHOLD.get(category, 50_000)
            paths = []
            for f in files:
                name = f.get("name", "")
                if not name.endswith(".pdf"):
                    continue
                size = f.get("metadata", {}).get("size", 0)
                if size < min_size:
                    log.debug(f"  Skip small: {name} ({size} bytes)")
                    continue
                paths.append(f"{folder_path}/{name}")

            # newest first
            paths = sorted(paths, reverse=True)
            if paths:
                by_category[category] = paths
                log.info(f"  {category}: {len(paths)} PDFs after filter")

        except Exception as e:
            log.warning(f"Could not list {folder_path}: {e}")
    return by_category

def download_pdf_to_tmp(storage_path):
    """Download a PDF from Supabase to a temp file. Returns tmp path."""
    data = supabase.storage.from_(BUCKET).download(storage_path)
    tmp  = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.write(data)
    tmp.close()
    return tmp.name


# ── Gemini helpers ────────────────────────────────────────────

def upload_pdf_to_gemini(tmp_path):
    """Upload a local PDF to Gemini Files API. Returns file object."""
    with open(tmp_path, "rb") as f:
        uploaded = client.files.upload(
            file=f,
            config=types.UploadFileConfig(mime_type="application/pdf")
        )
    time.sleep(1)
    return uploaded


def call_gemini(parts, prompt_text):
    contents = parts + [prompt_text]
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents,
        config=types.GenerateContentConfig(
            http_options=types.HttpOptions(timeout=120000)  # 120 sec
        )
    )
    raw = response.text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def safe_call_gemini(parts, prompt_text, label=""):
    for attempt in range(3):
        try:
            return call_gemini(parts, prompt_text)
        except Exception as e:
            log.warning(f"Gemini attempt {attempt+1}/3 failed [{label}]: {e}")
            if attempt < 2:
                time.sleep(15 * (attempt + 1))  # 15s, 30s
    log.error(f"All retries failed [{label}]")
    return {}


# ── Core extraction logic ─────────────────────────────────────

def process_financial_pdfs(pdf_paths, company_name):
    """
    Send up to MAX_PDFS_PER_BATCH financial PDFs to Gemini.
    Returns merged financial JSON.
    """
    log.info(f"  [Financial] Processing {len(pdf_paths)} PDFs for {company_name}")
    merged = {}

    # Process in batches
    for i in range(0, len(pdf_paths), MAX_PDFS_PER_BATCH):
        batch = pdf_paths[i:i + MAX_PDFS_PER_BATCH]
        tmp_paths = []
        gemini_files = []

        try:
            # Download and upload to Gemini
            for path in batch:
                log.info(f"    Downloading: {path}")
                tmp = download_pdf_to_tmp(path)
                tmp_paths.append(tmp)
                gf = upload_pdf_to_gemini(tmp)
                gemini_files.append(gf)

            # Build parts list
            parts = [
                types.Part.from_uri(file_uri=gf.uri, mime_type="application/pdf")
                for gf in gemini_files
            ]

            # Call Gemini
            result = safe_call_gemini(parts, FINANCIAL_PROMPT, label=f"financial-batch-{i}")
            if result:
                merged = _deep_merge_financial(merged, result)

            time.sleep(3)

        finally:
            for tmp in tmp_paths:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            # Clean up Gemini Files API uploads
            for gf in gemini_files:
                try:
                    gf_name = getattr(gf, "name", None) or getattr(gf, "filename", None)
                    if gf_name:
                        try:
                            client.files.delete(name=gf_name)
                            log.debug(f"Deleted Gemini file: {gf_name}")
                        except Exception:
                            try:
                                client.files.delete(gf_name)
                                log.debug(f"Deleted Gemini file (alt): {gf_name}")
                            except Exception as e:
                                log.warning(f"Could not delete Gemini file {gf_name}: {e}")
                except Exception as e:
                    log.warning(f"Could not delete Gemini file object: {e}")

    return merged


def process_corporate_pdfs(pdf_paths, company_name):
    """
    Send up to MAX_PDFS_PER_BATCH corporate PDFs to Gemini.
    Returns merged corporate JSON.
    """
    log.info(f"  [Corporate] Processing {len(pdf_paths)} PDFs for {company_name}")
    merged = {}

    for i in range(0, len(pdf_paths), MAX_PDFS_PER_BATCH):
        batch = pdf_paths[i:i + MAX_PDFS_PER_BATCH]
        tmp_paths = []
        gemini_files = []

        try:
            for path in batch:
                log.info(f"    Downloading: {path}")
                tmp = download_pdf_to_tmp(path)
                tmp_paths.append(tmp)
                gf = upload_pdf_to_gemini(tmp)
                gemini_files.append(gf)

            parts = [
                types.Part.from_uri(file_uri=gf.uri, mime_type="application/pdf")
                for gf in gemini_files
            ]

            result = safe_call_gemini(parts, CORPORATE_PROMPT, label=f"corporate-batch-{i}")
            if result:
                merged = _deep_merge_corporate(merged, result)

            time.sleep(3)

        finally:
            for tmp in tmp_paths:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            # Clean up Gemini Files API uploads
            for gf in gemini_files:
                try:
                    gf_name = getattr(gf, "name", None) or getattr(gf, "filename", None)
                    if gf_name:
                        try:
                            client.files.delete(name=gf_name)
                            log.debug(f"Deleted Gemini file: {gf_name}")
                        except Exception:
                            try:
                                client.files.delete(gf_name)
                                log.debug(f"Deleted Gemini file (alt): {gf_name}")
                            except Exception as e:
                                log.warning(f"Could not delete Gemini file {gf_name}: {e}")
                except Exception as e:
                    log.warning(f"Could not delete Gemini file object: {e}")

    return merged


def merge_all_results(financial_data, corporate_data):
    """
    Use Gemini to intelligently merge financial + corporate data
    into one complete profile, computing summary fields.
    """
    if not financial_data and not corporate_data:
        return {}

    if not financial_data:
        return corporate_data
    if not corporate_data:
        return financial_data

    prompt = MERGE_PROMPT.format(
        financial_json=json.dumps(financial_data, indent=2),
        corporate_json=json.dumps(corporate_data, indent=2)
    )

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt]
        )
        raw = response.text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        merged = json.loads(raw)

        # Also compute summary fields locally as fallback
        merged = _compute_summary_fields(merged, financial_data)
        return merged

    except Exception as e:
        log.error(f"Merge error: {e}")
        # Fallback: simple dict merge
        result = {**financial_data, **corporate_data}
        return _compute_summary_fields(result, financial_data)


def _compute_summary_fields(profile, financial_data):
    """Compute latest_* summary fields from annual_pl and ratios arrays."""
    annual_pl = financial_data.get("annual_pl") or profile.get("annual_pl") or []
    ratios    = financial_data.get("ratios")    or profile.get("ratios")    or []

    if annual_pl:
        latest = annual_pl[0]  # newest first
        profile.setdefault("latest_revenue",    latest.get("revenue"))
        profile.setdefault("latest_net_profit", latest.get("net_profit"))
        profile.setdefault("latest_net_margin", latest.get("net_profit") / latest.get("revenue") * 100
                           if latest.get("revenue") and latest.get("net_profit") else None)
        profile.setdefault("latest_year",       latest.get("year"))

    if ratios:
        latest_r = ratios[0]
        profile.setdefault("latest_roe",            latest_r.get("roe"))
        profile.setdefault("latest_roce",           latest_r.get("roce"))
        profile.setdefault("latest_debt_to_equity", latest_r.get("debt_to_equity"))
        profile.setdefault("latest_pe",             latest_r.get("pe_ratio"))

    quarterly = financial_data.get("quarterly_data") or profile.get("quarterly_data") or []
    if quarterly:
        profile.setdefault("latest_quarter", quarterly[0].get("quarter"))

    return profile


# ── Deep merge helpers ────────────────────────────────────────

def _deep_merge_financial(base, new):
    """Merge two financial dicts, combining array fields."""
    if not base:
        return new
    result = dict(base)

    # For array fields, combine and deduplicate by period key
    for arr_field, key in [
        ("quarterly_data", "quarter"),
        ("annual_pl",      "year"),
        ("balance_sheet",  "year"),
        ("cash_flow",      "year"),
        ("ratios",         "year"),
    ]:
        base_arr = base.get(arr_field) or []
        new_arr  = new.get(arr_field)  or []
        result[arr_field] = _merge_arrays(base_arr, new_arr, key)

    # Scalar fields: prefer non-null values
    for field in ["company_name", "bse_code", "nse_symbol", "face_value", "book_value"]:
        if not result.get(field) and new.get(field):
            result[field] = new[field]

    return result


def _deep_merge_corporate(base, new):
    """Merge two corporate dicts."""
    if not base:
        return new
    result = dict(base)

    # Text fields: prefer longer/more detailed
    for field in ["about", "business_description", "key_points", "management_outlook",
                  "key_risks", "opportunities", "products_and_platforms",
                  "expansion_plans", "acquisitions", "partnerships",
                  "client_data", "awards_recognition", "esg_highlights",
                  "ai_digital_initiatives"]:
        base_val = base.get(field) or ""
        new_val  = new.get(field)  or ""
        result[field] = base_val if len(base_val) >= len(new_val) else new_val

    # Array fields
    for arr_field, key in [
        ("segments",              "name"),
        ("geographic_segments",   "region"),
        ("shareholding",          "quarter"),
        ("recent_announcements",  "date"),
    ]:
        base_arr = base.get(arr_field) or []
        new_arr  = new.get(arr_field)  or []
        result[arr_field] = _merge_arrays(base_arr, new_arr, key)

    # Employee data: merge dict
    base_emp = base.get("employee_data") or {}
    new_emp  = new.get("employee_data")  or {}
    result["employee_data"] = {**new_emp, **{k: v for k, v in base_emp.items() if v is not None}}

    return result


def _merge_arrays(base_arr, new_arr, key):
    """Merge two arrays of dicts by a key field, combining fields."""
    index = {}
    for item in base_arr:
        k = item.get(key)
        if k:
            index[k] = dict(item)

    for item in new_arr:
        k = item.get(key)
        if not k:
            continue
        if k in index:
            # Merge: fill in nulls from new
            for field, val in item.items():
                if (index[k].get(field) is None) and val is not None:
                    index[k][field] = val
        else:
            index[k] = dict(item)

    # Sort by key descending (newest first)
    result = list(index.values())
    try:
        result.sort(key=lambda x: str(x.get(key, "")), reverse=True)
    except Exception:
        pass
    return result


# ── Supabase save ─────────────────────────────────────────────

def save_company_profile(profile, rejection_count: int = 0):
    """Upsert the full company profile into company_profiles table."""
    if not profile:
        return

    company_name = profile.get("company_name", "")
    bse_code     = profile.get("bse_code", "")

    if not company_name and not bse_code:
        log.warning("No company_name or bse_code in profile — skipping save")
        return
    # Always reset to pending on every new extraction
    # regardless of previous status
    new_status = "pending"

    row = {
        "company_name":         company_name,
        "bse_code":             bse_code,
        "nse_symbol":           profile.get("nse_symbol"),
        "face_value":           profile.get("face_value"),
        "book_value":           profile.get("book_value"),
        "latest_revenue":       profile.get("latest_revenue"),
        "latest_net_profit":    profile.get("latest_net_profit"),
        "latest_net_margin":    profile.get("latest_net_margin"),
        "latest_roe":           profile.get("latest_roe"),
        "latest_roce":          profile.get("latest_roce"),
        "latest_debt_to_equity":profile.get("latest_debt_to_equity"),
        "latest_pe":            profile.get("latest_pe"),
        "latest_year":          profile.get("latest_year"),
        "latest_quarter":       profile.get("latest_quarter"),
        "about":                profile.get("about"),
        "business_description": profile.get("business_description"),
        "key_points":           profile.get("key_points"),
        "management_outlook":   profile.get("management_outlook"),
        "key_risks":            profile.get("key_risks"),
        "opportunities":        profile.get("opportunities"),
        "esg_highlights":    profile.get("esg_highlights"),
        "awards_recognition":profile.get("awards_recognition"),
        "products_and_platforms":profile.get("products_and_platforms"),
        "expansion_plans":      profile.get("expansion_plans"),
        "acquisitions":         profile.get("acquisitions"),
        "partnerships":         profile.get("partnerships"),
        "client_data":          profile.get("client_data"),
        "ai_digital_initiatives":profile.get("ai_digital_initiatives"),
        "verdict":              profile.get("verdict"),
        # JSONB arrays
        "quarterly_data":       profile.get("quarterly_data") or [],
        "annual_pl":            profile.get("annual_pl") or [],
        "balance_sheet":        profile.get("balance_sheet") or [],
        "cash_flow":            profile.get("cash_flow") or [],
        "ratios":               profile.get("ratios") or [],
        "segments":             profile.get("segments") or [],
        "geographic_segments":  profile.get("geographic_segments") or [],
        "shareholding":         profile.get("shareholding") or [],
        "recent_announcements": profile.get("recent_announcements") or [],
        "employee_data":        profile.get("employee_data") or {},
        "full_data":            profile,
        "status":              new_status,
    }

    try:
        supabase.table("company_profiles").upsert(
            row, on_conflict="bse_code"
        ).execute()
        log.info(f"Saved profile for {company_name} (BSE: {bse_code}) — status: pending")
    except Exception as e:
        log.error(f"Failed to save profile for {company_name}: {e}")
        return

    # Email master — always a fresh review request
    threading.Thread(
        target=notifier.notify_stock_master,
        args=(profile,),
        kwargs={"send_to_holder": rejection_count >= 1},
        daemon=True
    ).start()
    log.info(f"Approval email sent for {company_name}")


# ── Profile status check ──────────────────────────────────────

def is_profile_fresh(identifier, max_age_hours=24):
    """
    Returns True if company_profiles has a recent row.
    Accepts company_name or bse_code.
    """
    try:
        from datetime import datetime, timezone, timedelta

        # Try bse_code first
        res = supabase.table("company_profiles") \
            .select("last_updated") \
            .eq("bse_code", identifier) \
            .execute()

        # Fallback to name match
        if not res.data:
            res = supabase.table("company_profiles") \
                .select("last_updated") \
                .ilike("company_name", f"%{identifier}%") \
                .execute()

        if not res.data:
            return False

        last_updated = res.data[0].get("last_updated", "")
        if not last_updated:
            return False

        updated_at = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - updated_at
        return age < timedelta(hours=max_age_hours)

    except Exception:
        return False


# ── Public API ────────────────────────────────────────────────

def run_extraction(company_name=None, force=False):
    """
    Main entry point called by server.py.

    company_name: if provided, only process that company.
                  if None, process ALL companies in the bucket.
    force:        if True, re-process even if profile is fresh.

    Returns dict: { total, success, skipped, failed }
    """
    results = {"total": 0, "success": 0, "skipped": 0, "failed": 0}

    companies = [company_name] if company_name else get_all_companies()
    log.info(f"Extraction starting — {len(companies)} company/companies")

    for company in companies:
        results["total"] += 1
        log.info(f"=== Extracting: {company} ===")

        try:
            # Check if already fresh (skip unless forced)
            # We don't have bse_code here easily, so skip freshness check
            # and let server.py decide via the force flag

            # Get all PDFs grouped by category
            pdfs_by_category = get_company_pdfs(company)
            total_pdfs = sum(len(v) for v in pdfs_by_category.values())

            if total_pdfs == 0:
                log.info(f"  No PDFs found for {company} — skipping")
                results["skipped"] += 1
                continue

            log.info(f"  Found {total_pdfs} PDFs across {len(pdfs_by_category)} categories")

            # ── Step 1: Process financial PDFs ────────────────
            financial_categories = ["FinancialStatements"]
            financial_pdfs = []
            for cat in financial_categories:
                financial_pdfs.extend(pdfs_by_category.get(cat, []))

            financial_data = {}
            if financial_pdfs:
                financial_data = process_financial_pdfs(financial_pdfs, company)
            
            # ── Step 2: Process corporate PDFs ────────────────
            corporate_categories = [
                "CorporateFilings", "RegulatoryFilings",
                "StrategicDocuments", "MCA_Documents"
            ]
            corporate_pdfs = []
            for cat in corporate_categories:
                corporate_pdfs.extend(pdfs_by_category.get(cat, []))

            corporate_data = {}
            if corporate_pdfs:
                corporate_data = process_corporate_pdfs(corporate_pdfs, company)

            # ── Step 3: Merge into one profile ────────────────
            log.info(f"  Merging financial + corporate data...")
            profile = merge_all_results(financial_data, corporate_data)

            # Fallback: set company name from folder name if Gemini missed it
            if not profile.get("company_name"):
                profile["company_name"] = company

            # ── Step 4: Save to Supabase ──────────────────────
            save_company_profile(profile)
            results["success"] += 1

        except Exception as e:
            log.error(f"Failed extraction for {company}: {e}")
            results["failed"] += 1

        time.sleep(5)  # be kind to Gemini rate limits between companies

    log.info(f"Extraction complete: {results}")
    return results


# ── Read profile back (used by server.py routes) ──────────────

def get_company_profile(company_name):
    """Fetch a company profile from Supabase by name or BSE code."""
    try:
        # Try exact name match first
        res = supabase.table("company_profiles") \
            .select("*") \
            .ilike("company_name", f"%{company_name}%") \
            .eq("status", "approved") \
            .limit(1) \
            .execute()
        if res.data:
            return res.data[0]

        # Try BSE code
        res = supabase.table("company_profiles") \
            .select("*") \
            .eq("bse_code", company_name) \
            .eq("status", "approved") \
            .limit(1) \
            .execute()
        return res.data[0] if res.data else None

    except Exception as e:
        log.error(f"get_company_profile({company_name}): {e}")
        return None


def get_all_profiles_summary(status=None):
    """
    Returns lightweight list of all companies for the /api/companies endpoint.
    Only fetches scalar columns, not the heavy JSONB arrays.
    """
    try:
        query = supabase.table("company_profiles") \
            .select(
                "company_name, bse_code, nse_symbol, latest_revenue, "
                "latest_net_profit, latest_net_margin, latest_roe, "
                "latest_roce, latest_debt_to_equity, latest_pe, "
                "latest_year, latest_quarter, about, verdict"
            )

        if status:
            res = query.eq("status", status) \
                .order("company_name") \
                .execute()
        else:
            res = query.order("company_name") \
                .execute()
        return res.data or []
    except Exception as e:
        log.error(f"get_all_profiles_summary: {e}")
        return []


REGENERATE_PROMPT = """
You are improving a company financial profile JSON based on reviewer feedback.

Original Profile JSON:
{original_json}

Reviewer Feedback:
{feedback}

Fix ALL issues mentioned in the feedback.
Return complete improved JSON only.
No markdown. No explanation.
"""


def regenerate_with_feedback(bse_code: str, feedback: str,
                              rejection_count: int = 1):
    """
    Called after master rejects + replies with feedback.
    Fetches existing profile → Gemini improves it →
    saves with status=pending → sends new review email.
    """
    log.info(f"Regenerating profile for BSE:{bse_code}")

    # fetch existing profile
    try:
        res = supabase.table("company_profiles") \
            .select("*") \
            .eq("bse_code", bse_code) \
            .limit(1) \
            .execute()

        if not res.data:
            log.error(f"No profile for BSE:{bse_code}")
            return

        profile = res.data[0]

    except Exception as e:
        log.error(f"Fetch error BSE:{bse_code}: {e}")
        return

    # build prompt
    prompt = REGENERATE_PROMPT.format(
        original_json=json.dumps(profile, indent=2),
        feedback=feedback.strip()
    )

    # call Gemini
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt]
        )
        raw = response.text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        improved = json.loads(raw)
    except Exception as e:
        log.error(f"Gemini regenerate error: {e}")
        return

    # force correct fields
    improved["bse_code"]  = bse_code
    improved["status"]    = "pending"
    if not improved.get("company_name"):
        improved["company_name"] = profile.get("company_name", "")

    # save → triggers new review email automatically
    save_company_profile(improved, rejection_count=rejection_count)
    log.info(f"Regenerated BSE:{bse_code} → status: pending → email sent")