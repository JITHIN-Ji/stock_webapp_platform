"""
server.py  —  v2
=================
Changes from v1:
  1. INTEGRATED SCRAPING: On startup, triggers both scraping (downloader.py)
     AND extraction (extractor.py) as background processes per company
  2. SMART SCHEDULER: Reads filing_calendar table to know WHEN each company
     is expected to release new documents — only scrapes then, not blindly
  3. SCRAPE ENDPOINTS: New /api/scrape and /api/scrape/<company> endpoints
  4. UNIFIED STATE: Single `state` dict tracks both scraping and extraction

API endpoints:
  GET  /api/status                      → server, scraping & extraction status
  GET  /api/companies                   → list of companies with data
  GET  /api/company/<name>              → metrics summary for one company
  GET  /api/company/<name>/docs         → documents list for one company
  POST /api/ask                         → Q&A for a specific company
  POST /api/scan                        → trigger manual extraction scan (all)
  POST /api/scan/<company>              → trigger extraction for one company
  POST /api/scrape                      → trigger manual scrape (all companies)
  POST /api/scrape/<company>            → trigger scrape for one company
"""

import os
import logging
import threading
import time
import datetime
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

import extractor
import qa_engine
import downloader   # ← NEW: integrated scraper

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")
CORS(app)

# ── Global state ──────────────────────────────────────────────
state = {
    # Scraping state
    "scraping":          False,
    "scraping_company":  None,
    "scraping_last_run": None,
    "scraping_message":  "Not started yet",
    "scraping_results":  [],      # list of per-company result dicts

    # Extraction state
    "extracting":          False,
    "extraction_company":  None,
    "extraction_last_run": None,
    "extraction_message":  "Not started yet",
    "extraction_total":    0,
    "extraction_success":  0,
    "extraction_skipped":  0,
    "extraction_failed":   0,
}

# ── Lock to prevent overlapping runs ─────────────────────────
_scrape_lock    = threading.Lock()
_extract_lock   = threading.Lock()

# =============================================================
# SCRAPING BACKGROUND TASK
# =============================================================

def _run_scraping_background(company_name: str | None = None):
    """
    company_name=None  → scrape all companies in catalogue
    company_name="X"   → scrape only that company
    """
    with _scrape_lock:
        if state["scraping"]:
            log.info("Scraping already running — skipping")
            return

        state["scraping"]         = True
        state["scraping_company"] = company_name or "ALL"
        state["scraping_message"] = (
            f"Scraping {company_name}..." if company_name else "Scraping all companies..."
        )

    try:
        if company_name:
            result  = downloader.run_scraping(company_name)
            results = [result]
        else:
            results = downloader.run_scraping_all()

        state.update({
            "scraping":          False,
            "scraping_company":  None,
            "scraping_last_run": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "scraping_results":  results,
            "scraping_message":  (
                f"Done — "
                f"{sum(r.get('uploaded',0) for r in results)} uploaded, "
                f"{sum(r.get('skipped',0) for r in results)} skipped, "
                f"{sum(r.get('failed',0) for r in results)} failed"
            ),
        })
        log.info(f"Scraping complete: {state['scraping_message']}")

        # After scraping finishes → trigger extraction for the same company
        log.info("Auto-triggering extraction after scraping...")
        start_extraction(company_name=company_name)

    except Exception as e:
        state.update({
            "scraping":         False,
            "scraping_company": None,
            "scraping_message": f"Error: {e}",
        })
        log.error(f"Scraping error: {e}")


def start_scraping(company_name: str | None = None):
    t = threading.Thread(
        target=_run_scraping_background,
        args=(company_name,),
        daemon=True
    )
    t.start()


# =============================================================
# EXTRACTION BACKGROUND TASK  (unchanged logic, updated state keys)
# =============================================================

def _run_extraction_background(company_name: str | None = None):
    with _extract_lock:
        if state["extracting"]:
            log.info("Extraction already running — skipping")
            return

        state["extracting"]         = True
        state["extraction_company"] = company_name
        state["extraction_message"] = (
            f"Processing PDFs for {company_name}..." if company_name
            else "Scanning all companies..."
        )

    log.info(f"Extraction started — company: {company_name or 'ALL'}")

    try:
        results = extractor.run_extraction(company_name=company_name)
        state.update({
            "extracting":          False,
            "extraction_company":  None,
            "extraction_last_run": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "extraction_total":    results["total"],
            "extraction_success":  results["success"],
            "extraction_skipped":  results["skipped"],
            "extraction_failed":   results["failed"],
            "extraction_message":  (
                f"Done — {results['success']} new, "
                f"{results['skipped']} skipped, "
                f"{results['failed']} failed"
            ),
        })
        log.info(f"Extraction complete: {results}")

    except Exception as e:
        state.update({
            "extracting":         False,
            "extraction_company": None,
            "extraction_message": f"Error: {e}",
        })
        log.error(f"Extraction error: {e}")


def start_extraction(company_name: str | None = None):
    t = threading.Thread(
        target=_run_extraction_background,
        args=(company_name,),
        daemon=True
    )
    t.start()


# =============================================================
# SMART FILING CALENDAR SCHEDULER
# =============================================================

def _smart_schedule_loop():
    """
    Runs in background. Every hour checks the filing_calendar table.
    If any company has `next_expected` ≤ today, triggers a scrape for it.
    Falls back to a 24-hour full scan if no calendar data exists.
    """
    FALLBACK_HOURS = 24
    last_fallback  = datetime.datetime.now()

    while True:
        time.sleep(60 * 60)   # check every hour

        try:
            calendars = downloader.get_all_filing_calendars()
            today     = datetime.date.today()
            triggered = []

            for cal in calendars:
                next_exp = cal.get("next_expected")
                if not next_exp:
                    continue
                try:
                    next_date = datetime.date.fromisoformat(str(next_exp)[:10])
                except Exception:
                    continue

                if next_date <= today:
                    company_name = cal.get("company_name") or cal.get("bse_code")
                    log.info(f"[SMART SCHEDULE] {company_name} filing expected today ({next_date}) — triggering scrape")
                    start_scraping(company_name=company_name)
                    triggered.append(company_name)

            if triggered:
                log.info(f"Smart schedule triggered scraping for: {triggered}")
            else:
                # Fallback: if no calendar triggers fired and it's been >24h, do a full scan
                hours_since = (datetime.datetime.now() - last_fallback).total_seconds() / 3600
                if hours_since >= FALLBACK_HOURS:
                    log.info(f"[SMART SCHEDULE] {FALLBACK_HOURS}h fallback scan — no calendar triggers today")
                    start_scraping(company_name=None)
                    last_fallback = datetime.datetime.now()

        except Exception as e:
            log.error(f"Smart schedule error: {e}")


# =============================================================
# API ROUTES
# =============================================================

@app.route("/api/status")
def api_status():
    return jsonify({
        "server": "running",
        "scraping": {
            "running":    state["scraping"],
            "company":    state["scraping_company"],
            "last_run":   state["scraping_last_run"],
            "message":    state["scraping_message"],
            "results":    state["scraping_results"],
        },
        "extraction": {
            "running":    state["extracting"],
            "company":    state["extraction_company"],
            "last_run":   state["extraction_last_run"],
            "message":    state["extraction_message"],
            "total":      state["extraction_total"],
            "success":    state["extraction_success"],
            "skipped":    state["extraction_skipped"],
            "failed":     state["extraction_failed"],
        },
    })


@app.route("/api/companies")
def api_companies():
    data = qa_engine.get_companies()
    return jsonify({"companies": data, "count": len(data)})


@app.route("/api/company/<path:name>")
def api_company(name):
    summary = qa_engine.get_company_summary(name)
    if not summary:
        return jsonify({"error": f"No data found for '{name}'"}), 404
    return jsonify(summary)


@app.route("/api/company/<path:name>/docs")
def api_company_docs(name):
    docs = qa_engine.get_company_docs(name)
    return jsonify({"docs": docs, "count": len(docs)})


@app.route("/api/ask", methods=["POST"])
def api_ask():
    body         = request.get_json() or {}
    company_name = (body.get("company_name") or "").strip()
    question     = (body.get("question") or "").strip()
    year         = body.get("year")
    doc_type     = body.get("doc_type")

    if not company_name or not question:
        return jsonify({"error": "company_name and question are required"}), 400

    log.info(f"Q&A → company='{company_name}' question='{question}'")
    result = qa_engine.answer_question(company_name, question, year=year, doc_type=doc_type)
    return jsonify(result)


# ── Extraction triggers (kept from v1) ────────────────────────
@app.route("/api/scan", methods=["POST"])
def api_scan():
    if state["extracting"]:
        return jsonify({"message": "Extraction already running"}), 409
    start_extraction(company_name=None)
    return jsonify({"message": "Extraction scan started for all companies"})


@app.route("/api/scan/<path:company_name>", methods=["POST"])
def api_scan_company(company_name):
    if state["extracting"]:
        return jsonify({"message": "Extraction already running"}), 409
    start_extraction(company_name=company_name)
    return jsonify({"message": f"Extraction scan started for '{company_name}'"})


# ── Scraping triggers (NEW) ───────────────────────────────────
@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    """Trigger a full scrape of all companies."""
    if state["scraping"]:
        return jsonify({"message": "Scraping already running"}), 409
    start_scraping(company_name=None)
    return jsonify({"message": "Scraping started for all companies"})


@app.route("/api/scrape/<path:company_name>", methods=["POST"])
def api_scrape_company(company_name):
    """Trigger scrape for a single company, then auto-extract."""
    if state["scraping"]:
        return jsonify({"message": "Scraping already running"}), 409
    start_scraping(company_name=company_name)
    return jsonify({"message": f"Scraping started for '{company_name}'"})


# ── Filing calendar info ──────────────────────────────────────
@app.route("/api/calendar")
def api_calendar():
    """Returns all filing calendar entries — useful for the frontend scheduler view."""
    calendars = downloader.get_all_filing_calendars()
    return jsonify({"calendars": calendars, "count": len(calendars)})


@app.route("/api/calendar/<path:company_name>")
def api_calendar_company(company_name):
    """Returns filing calendar for one company."""
    resolved = downloader._resolve_company(company_name)
    if not resolved:
        return jsonify({"error": f"Unknown company: {company_name}"}), 404
    cal = downloader.get_filing_calendar(resolved["bse_code"])
    return jsonify(cal or {})


# ── Frontend ──────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


# =============================================================
# STARTUP
# =============================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))

    log.info("=" * 60)
    log.info("FinSight Financial Analysis Platform")
    log.info(f"URL        : http://localhost:{port}")
    log.info(f"Supabase   : {os.getenv('SUPABASE_BUCKET', 'company-documents')}")
    log.info("=" * 60)

    # Step 1: On startup — scrape all companies (background)
    # This also auto-triggers extraction when scraping finishes
    log.info("Step 1: Starting initial scrape of all companies (background)...")
    start_scraping(company_name=None)

    # Step 2: Also run extraction immediately on any already-uploaded PDFs
    # (in case the server restarted but scraping was done before)
    log.info("Step 2: Starting extraction for any existing unprocessed PDFs...")
    start_extraction(company_name=None)

    # Step 3: Smart calendar-aware scheduler
    log.info("Step 3: Starting smart filing calendar scheduler...")
    t = threading.Thread(target=_smart_schedule_loop, daemon=True)
    t.start()

    if __name__ == "__main__":
    app.run(host="0.0.0.0", port=port, debug=False)