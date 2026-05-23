"""
qa_engine.py
============
All DB queries are filtered by company_name — only data for the
company the user is currently viewing is ever fetched or shown.

Functions:
  get_companies()                          → sidebar list
  get_company_docs(company_name)           → processed documents list
  get_company_summary(company_name)        → metrics cards
  answer_question(company_name, question)  → Q&A via Gemini
"""

import os
import logging
from google import genai
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

client   = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


# ── Get all unique companies from analysis_results ────────────
def get_companies():
    """
    Returns list of companies that have at least one processed document.
    Used to populate the sidebar.
    """
    try:
        res = supabase.table("analysis_results") \
            .select("company_name, bse_code, nse_symbol") \
            .order("company_name") \
            .execute()

        seen      = set()
        companies = []
        for row in res.data:
            name = (row.get("company_name") or "").strip().title()
            key  = name.lower()
            if name and key not in seen:
                seen.add(key)
                companies.append({
                    "company_name": name,
                    "bse_code":     row.get("bse_code"),
                    "nse_symbol":   row.get("nse_symbol"),
                })
        return companies

    except Exception as e:
        log.error(f"get_companies: {e}")
        return []


# ── Get processed documents for ONE company ───────────────────
def get_company_docs(company_name):
    """
    Returns all document records for this specific company only.
    """
    if not company_name:
        return []
    try:
        res = supabase.table("analysis_results") \
            .select("id, doc_type, year, quarter, source_filename") \
            .eq("company_name", company_name) \
            .order("year", desc=True) \
            .execute()
        return res.data
    except Exception as e:
        log.error(f"get_company_docs({company_name}): {e}")
        return []


# ── Fetch all financial rows for ONE company ──────────────────
def fetch_company_data(company_name, year=None, doc_type=None):
    """
    Fetches up to 20 financial records strictly for company_name.
    No other company's data is ever included.
    """
    if not company_name:
        return []
    try:
        query = supabase.table("analysis_results") \
            .select("*") \
            .eq("company_name", company_name)   # ← strict company filter

        if year:
            query = query.eq("year", year)
        if doc_type:
            query = query.eq("doc_type", doc_type)

        res = query.order("year", desc=True).limit(20).execute()
        return res.data

    except Exception as e:
        log.error(f"fetch_company_data({company_name}): {e}")
        return []


# ── Pick the row with the most financial fields filled ────────
def pick_best_row(rows):
    """
    The latest row is often a quarterly result with sparse balance sheet data.
    Pick the row with the most non-null financial fields for the summary card.
    """
    FIELDS = [
        "revenue", "net_profit", "gross_profit", "ebitda",
        "total_debt", "total_equity", "roe", "roce",
        "debt_to_equity", "net_margin", "eps",
        "operating_cash_flow", "cash_and_equivalents",
    ]
    return max(rows, key=lambda r: sum(1 for f in FIELDS if r.get(f) is not None))


# ── Build the Q&A prompt with all available periods ───────────
def build_qa_prompt(rows, question, company_name):
    def sort_key(r):
        yr  = str(r.get("year") or "0000")
        qtr = str(r.get("quarter") or "Q0")
        return (yr, qtr)

    sorted_rows = sorted(rows, key=sort_key)

    periods_list = ", ".join(
        f"FY{r.get('year', '')} {r.get('quarter') or ''}".strip()
        for r in sorted_rows
    )

    data_block = ""
    for row in sorted_rows:
        yr     = row.get("year", "")
        qtr    = row.get("quarter", "")
        period = f"FY{yr}" + (f" {qtr}" if qtr else "")

        data_block += f"""
━━━ {row.get('doc_type', 'Document')} | {period} ━━━
Revenue              : {row.get('revenue')} Cr
Net Profit           : {row.get('net_profit')} Cr
Gross Profit         : {row.get('gross_profit')} Cr
EBITDA               : {row.get('ebitda')} Cr
Total Expenses       : {row.get('total_expenses')} Cr
Expense %            : {row.get('expense_percentage')}%
Total Debt           : {row.get('total_debt')} Cr
Total Equity         : {row.get('total_equity')} Cr
Cash                 : {row.get('cash_and_equivalents')} Cr
Operating Cash Flow  : {row.get('operating_cash_flow')} Cr
EPS                  : {row.get('eps')}
ROE                  : {row.get('roe')}%
ROCE                 : {row.get('roce')}%
Debt to Equity       : {row.get('debt_to_equity')}
Net Margin           : {row.get('net_margin')}%
Revenue Growth YoY   : {row.get('revenue_growth_yoy')}%
Profit Growth YoY    : {row.get('profit_growth_yoy')}%
Key Highlights       : {row.get('key_highlights')}
Management Outlook   : {row.get('management_outlook')}
Key Risks            : {row.get('key_risks')}
Verdict              : {row.get('verdict')}
"""

    return f"""
You are a financial analyst expert in Indian stock markets.
You explain financial data clearly to retail investors in plain English.

IMPORTANT: You are answering questions ONLY about {company_name}.
Do NOT reference or compare with any other company unless explicitly asked.

Company : {company_name}
Data available for {len(rows)} period(s): {periods_list}

FINANCIAL DATA FROM OFFICIAL DOCUMENTS:
{data_block}

USER QUESTION: {question}

Instructions:
- Answer ONLY using the data above — do not fabricate numbers
- Use actual numbers and specific periods (e.g. Q3 FY26, FY2024)
- If a multi-year trend is asked, compare the available periods clearly
- Write in plain English for a retail investor — no jargon
- Keep answer to 5-7 sentences
- If data is missing for the question, clearly state what is missing
- End with one practical takeaway for the investor
"""


# ── Answer a user question about a specific company ───────────
def answer_question(company_name, question, year=None, doc_type=None):
    """
    Fetches data for company_name only, then asks Gemini to answer
    the user's question based on that data.
    """
    if not company_name or not question:
        return {"error": "company_name and question are required"}

    # Fetch ONLY this company's data
    rows = fetch_company_data(company_name, year=year, doc_type=doc_type)

    if not rows:
        return {
            "answer":  (
                f"No financial data found for {company_name}. "
                f"Documents may still be processing — "
                f"click 'Scan New PDFs' and wait a moment."
            ),
            "sources": []
        }

    try:
        prompt   = build_qa_prompt(rows, question, company_name)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        answer = response.text.strip()

    except Exception as e:
        log.error(f"Gemini Q&A error for {company_name}: {e}")
        return {"error": f"Could not generate answer: {str(e)}"}

    sources = [
        {
            "doc_type": r.get("doc_type"),
            "year":     r.get("year"),
            "quarter":  r.get("quarter"),
        }
        for r in rows
    ]

    return {
        "company":  company_name,
        "question": question,
        "answer":   answer,
        "sources":  sources,
    }


# ── Company summary for the metrics cards ─────────────────────
def get_company_summary(company_name):
    """
    Returns key metrics for the dashboard cards.
    Uses best-filled row for financial metrics, latest row for metadata.
    """
    if not company_name:
        return None

    rows = fetch_company_data(company_name)
    if not rows:
        return None

    latest = rows[0]
    best   = pick_best_row(rows)

    return {
        "company_name":   (latest.get("company_name") or "").strip().title(),
        "bse_code":       latest.get("bse_code"),
        "nse_symbol":     latest.get("nse_symbol"),
        "latest_year":    latest.get("year"),
        # Financial metrics from best-filled row
        "revenue":        best.get("revenue"),
        "net_profit":     best.get("net_profit"),
        "net_margin":     best.get("net_margin"),
        "debt_to_equity": best.get("debt_to_equity"),
        "roe":            best.get("roe"),
        "roce":           best.get("roce"),
        "eps":            best.get("eps"),
        "verdict":        best.get("verdict"),
        "key_risks":      best.get("key_risks"),
        "total_docs":     len(rows),
    }