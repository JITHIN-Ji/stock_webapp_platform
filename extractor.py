"""
extractor.py
============
PDF Extraction using Gemini AI.

KEY CHANGE: Now accepts a company_name parameter.
  - Only scans that company's folder in the bucket
  - Storage path: <CompanyName>/<Category>/<filename>.pdf
  - Skips files already processed for that company

Called by server.py as:
    extractor.run_extraction(company_name="Reliance")   # specific company
    extractor.run_extraction()                           # all companies
"""

import os
import json
import time
import tempfile
import logging
from google import genai
from google.genai import types
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

client   = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
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


# ── Build Gemini extraction prompt ────────────────────────────
def build_prompt(storage_path):
    filename = storage_path.split("/")[-1]

    if   "Balance_Sheet"     in filename: doc_hint = "Balance Sheet"
    elif "Profit_and_Loss"   in filename: doc_hint = "Profit & Loss Statement"
    elif "Cash_Flow"         in filename: doc_hint = "Cash Flow Statement"
    elif "Annual_Report"     in filename: doc_hint = "Annual Report"
    elif "Quarterly_Results" in filename: doc_hint = "Quarterly Results"
    elif "Notes_to_Accounts" in filename: doc_hint = "Notes to Accounts"
    else:                                 doc_hint = None

    doc_type_rule = (
        f'- doc_type MUST be set to "{doc_hint}" based on the filename'
        if doc_hint else
        '- doc_type: identify from document content'
    )

    return f"""
You are a financial data extraction expert for Indian listed companies.

File path: {storage_path}
Filename hint: {filename}

Read the entire PDF carefully — every page, every table, every number.

Identify from cover page:
- Exact company name in Title Case (e.g. "Reliance Industries Limited" not "RELIANCE INDUSTRIES")
- BSE / NSE code
- Financial year
- Document type

CRITICAL RULES:
{doc_type_rule}
- company_name MUST be Title Case, never ALL CAPS

Return ONLY this JSON. No markdown. No explanation. No extra text:

{{
    "company_name": "",
    "bse_code": "",
    "nse_symbol": "",
    "year": "",
    "quarter": null,
    "doc_type": "",
    "revenue": null,
    "net_profit": null,
    "gross_profit": null,
    "ebitda": null,
    "total_expenses": null,
    "expense_percentage": null,
    "total_debt": null,
    "total_equity": null,
    "total_assets": null,
    "total_liabilities": null,
    "cash_and_equivalents": null,
    "operating_cash_flow": null,
    "capex": null,
    "eps": null,
    "pe_ratio": null,
    "roe": null,
    "roce": null,
    "debt_to_equity": null,
    "net_margin": null,
    "gross_margin": null,
    "current_ratio": null,
    "interest_coverage": null,
    "revenue_growth_yoy": null,
    "profit_growth_yoy": null,
    "key_highlights": "",
    "management_outlook": "",
    "key_risks": "",
    "verdict": ""
}}

Rules:
- All money in Indian Rupees Crores (Cr)
- Negative numbers for losses
- null if value not found
- expense_percentage = total expenses as percentage of revenue
- verdict = 2-3 sentence plain English financial health summary
- Return ONLY the JSON, nothing else
"""


# ── Check if a PDF is already processed ──────────────────────
def is_processed(storage_path):
    try:
        res = supabase.table("analysis_results") \
            .select("id") \
            .eq("source_filename", storage_path) \
            .execute()
        return len(res.data) > 0
    except Exception:
        return False


# ── List all companies in the bucket ─────────────────────────
def get_all_companies():
    """Returns list of company folder names from the bucket root."""
    try:
        items = supabase.storage.from_(BUCKET).list("")
        # Folders have no extension and metadata shows them as directories
        companies = [
            item["name"] for item in items
            if not item["name"].endswith(".pdf")
            and not item["name"].endswith(".csv")
        ]
        return companies
    except Exception as e:
        log.error(f"get_all_companies: {e}")
        return []


# ── List PDFs for a specific company ─────────────────────────
def get_company_pdfs(company_name):
    """
    Lists all PDFs under <company_name>/<Category>/ in the bucket.
    Returns list of full storage paths like:
        Reliance/FinancialStatements/Balance_Sheet_2024.pdf
    """
    all_pdfs = []

    for category in CATEGORIES:
        folder_path = f"{company_name}/{category}"
        try:
            files = supabase.storage.from_(BUCKET).list(
                folder_path,
                options={"limit": 1000, "offset": 0}
            )
            for f in files:
                name = f.get("name", "")
                if name.endswith(".pdf") or name.endswith(".csv"):
                    all_pdfs.append(f"{folder_path}/{name}")
        except Exception as e:
            log.warning(f"Could not list {folder_path}: {e}")

    return all_pdfs


# ── Download a PDF from Supabase to a temp file ──────────────
def download_pdf(storage_path):
    data = supabase.storage.from_(BUCKET).download(storage_path)
    tmp  = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.write(data)
    tmp.close()
    return tmp.name


# ── Send PDF to Gemini and parse JSON response ────────────────
def extract_with_gemini(tmp_path, storage_path):
    with open(tmp_path, "rb") as f:
        uploaded = client.files.upload(
            file=f,
            config=types.UploadFileConfig(mime_type="application/pdf")
        )
    time.sleep(2)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_uri(
                file_uri=uploaded.uri,
                mime_type="application/pdf"
            ),
            build_prompt(storage_path)
        ]
    )

    raw = response.text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


# ── Save extracted data to Supabase analysis_results table ───
def save_result(data, storage_path):
    row = {
        "source_filename":      storage_path,
        "company_name":         data.get("company_name"),
        "bse_code":             data.get("bse_code"),
        "nse_symbol":           data.get("nse_symbol"),
        "doc_type":             data.get("doc_type"),
        "year":                 data.get("year"),
        "quarter":              data.get("quarter"),
        "revenue":              data.get("revenue"),
        "net_profit":           data.get("net_profit"),
        "gross_profit":         data.get("gross_profit"),
        "ebitda":               data.get("ebitda"),
        "total_expenses":       data.get("total_expenses"),
        "expense_percentage":   data.get("expense_percentage"),
        "total_debt":           data.get("total_debt"),
        "total_equity":         data.get("total_equity"),
        "total_assets":         data.get("total_assets"),
        "total_liabilities":    data.get("total_liabilities"),
        "cash_and_equivalents": data.get("cash_and_equivalents"),
        "operating_cash_flow":  data.get("operating_cash_flow"),
        "capex":                data.get("capex"),
        "eps":                  data.get("eps"),
        "pe_ratio":             data.get("pe_ratio"),
        "roe":                  data.get("roe"),
        "roce":                 data.get("roce"),
        "debt_to_equity":       data.get("debt_to_equity"),
        "net_margin":           data.get("net_margin"),
        "gross_margin":         data.get("gross_margin"),
        "current_ratio":        data.get("current_ratio"),
        "interest_coverage":    data.get("interest_coverage"),
        "revenue_growth_yoy":   data.get("revenue_growth_yoy"),
        "profit_growth_yoy":    data.get("profit_growth_yoy"),
        "key_highlights":       data.get("key_highlights"),
        "management_outlook":   data.get("management_outlook"),
        "key_risks":            data.get("key_risks"),
        "verdict":              data.get("verdict"),
        "full_json":            json.dumps(data),
        "processed":            True,
    }
    supabase.table("analysis_results").insert(row).execute()


# ── Process one PDF file ──────────────────────────────────────
def process_one(storage_path):
    tmp_path = None
    try:
        log.info(f"  Downloading: {storage_path}")
        tmp_path = download_pdf(storage_path)

        log.info(f"  Sending to Gemini...")
        data = extract_with_gemini(tmp_path, storage_path)

        log.info(f"  Saving to Supabase DB...")
        save_result(data, storage_path)

        log.info(f"  Done: {data.get('company_name')} | {data.get('doc_type')} | {data.get('year')}")
        return {"status": "success", "data": data}

    except Exception as e:
        log.error(f"  Failed: {storage_path} | {e}")
        return {"status": "failed", "error": str(e)}

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


# ── Run extraction for a specific company (or all companies) ──
def run_extraction(company_name=None):
    """
    company_name: if provided, only process PDFs for that company.
                  if None, process ALL companies found in the bucket.
    """
    results = {"total": 0, "success": 0, "skipped": 0, "failed": 0}

    if company_name:
        # Only process this one company's folder
        companies = [company_name]
        log.info(f"Scanning bucket for company: {company_name}")
    else:
        # Process all company folders in the bucket
        companies = get_all_companies()
        log.info(f"Scanning bucket — found {len(companies)} companies: {companies}")

    for company in companies:
        log.info(f"Processing company: {company}")
        pdfs = get_company_pdfs(company)
        log.info(f"  Found {len(pdfs)} PDF(s) for {company}")

        for path in pdfs:
            results["total"] += 1

            if is_processed(path):
                log.info(f"  Skip (already done): {path}")
                results["skipped"] += 1
                continue

            result = process_one(path)
            if result["status"] == "success":
                results["success"] += 1
            else:
                results["failed"] += 1

            time.sleep(5)   # be kind to Gemini rate limits

    log.info(f"Extraction complete: {results}")
    return results