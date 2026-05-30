
import os
import logging
import threading
import time
import datetime
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

import extractor
import downloader
import notifier

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")
CORS(app, origins="*", supports_credentials=True)

@app.after_request
def after_request(response):
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "*")
    response.headers.add("Access-Control-Allow-Methods", "*")
    return response

state = {
    # Scraping
    "scraping":          False,
    "scraping_company":  None,
    "scraping_last_run": None,
    "scraping_message":  "Not started yet",
    "scraping_results":  [],

    # Extraction
    "extracting":          False,
    "extraction_company":  None,
    "extraction_last_run": None,
    "extraction_message":  "Not started yet",
    "extraction_total":    0,
    "extraction_success":  0,
    "extraction_skipped":  0,
    "extraction_failed":   0,
}

_scrape_lock  = threading.Lock()
_extract_lock = threading.Lock()


# =============================================================
# SCRAPING BACKGROUND TASK
# =============================================================

def _run_scraping_background(company_name=None):
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
            "scraping_message": (
                f"Done — "
                f"{sum(r.get('uploaded', 0) for r in results)} uploaded, "
                f"{sum(r.get('skipped',  0) for r in results)} skipped, "
                f"{sum(r.get('failed',   0) for r in results)} failed"
            ),
        })
        log.info(f"Scraping complete: {state['scraping_message']}")

        # Auto-trigger extraction after scraping
        log.info("Auto-triggering extraction after scraping...")
        start_extraction(company_name=company_name)

    except Exception as e:
        state.update({
            "scraping":         False,
            "scraping_company": None,
            "scraping_message": f"Error: {e}",
        })
        log.error(f"Scraping error: {e}")


def start_scraping(company_name=None):
    t = threading.Thread(
        target=_run_scraping_background,
        args=(company_name,),
        daemon=True
    )
    t.start()


# =============================================================
# EXTRACTION BACKGROUND TASK
# =============================================================

def _run_extraction_background(company_name=None):
    with _extract_lock:
        if state["extracting"]:
            log.info("Extraction already running — skipping")
            return
        state["extracting"]         = True
        state["extraction_company"] = company_name
        state["extraction_message"] = (
            f"Processing PDFs for {company_name}..." if company_name
            else "Processing all companies..."
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
            "extraction_message": (
                f"Done — {results['success']} processed, "
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


def start_extraction(company_name=None):
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
    FALLBACK_HOURS = 24
    last_fallback  = datetime.datetime.now()

    while True:
        time.sleep(60 * 60)

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
                    bse_code     = cal.get("bse_code")
                    if not extractor.is_profile_fresh(bse_code, max_age_hours=20):
                        log.info(f"[SMART SCHEDULE] {company_name} due — triggering scrape")
                        start_scraping(company_name=company_name)
                        triggered.append(company_name)
                    else:
                        log.info(f"[SMART SCHEDULE] {company_name} profile fresh — skipping")

            if triggered:
                log.info(f"Smart schedule triggered for: {triggered}")
            else:
                hours_since = (datetime.datetime.now() - last_fallback).total_seconds() / 3600
                if hours_since >= FALLBACK_HOURS:
                    log.info(f"[SMART SCHEDULE] 24h fallback — full extraction only")
                    start_extraction(company_name=None)
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
            "running":  state["scraping"],
            "company":  state["scraping_company"],
            "last_run": state["scraping_last_run"],
            "message":  state["scraping_message"],
            "results":  state["scraping_results"],
        },
        "extraction": {
            "running":  state["extracting"],
            "company":  state["extraction_company"],
            "last_run": state["extraction_last_run"],
            "message":  state["extraction_message"],
            "total":    state["extraction_total"],
            "success":  state["extraction_success"],
            "skipped":  state["extraction_skipped"],
            "failed":   state["extraction_failed"],
        },
    })


@app.route("/api/companies")
def api_companies():
    """
    Returns a lightweight summary list of all companies from company_profiles.
    Used by the Welcome page to populate search + chips.
    """
    data = extractor.get_all_profiles_summary(status="approved")
    return jsonify({"companies": data, "count": len(data)})


@app.route("/api/company/<path:name>")
def api_company(name):

    profile = extractor.get_company_profile(name)

    if not profile:
        return jsonify({"error": f"No data found for '{name}'"}), 404

    # ONLY APPROVED
    if profile.get("status") != "approved":
        return jsonify({"error": "Company not approved"}), 403

    summary = {k: v for k, v in profile.items() if k != "full_data"}

    return jsonify(summary)


@app.route("/api/company/<path:name>/profile")
def api_company_profile(name):

    profile = extractor.get_company_profile(name)

    if not profile:
        return jsonify({"error": f"No profile found for '{name}'"}), 404

    # ONLY APPROVED
    if profile.get("status") != "approved":
        return jsonify({"error": "Company not approved"}), 403

    result = {k: v for k, v in profile.items() if k != "full_data"}

    return jsonify(result)


# ── Extraction triggers ───────────────────────────────────────

@app.route("/api/scan", methods=["POST"])
def api_scan():
    if state["extracting"]:
        return jsonify({"message": "Extraction already running"}), 409
    start_extraction(company_name=None)
    return jsonify({"message": "Extraction started for all companies"})


@app.route("/api/scan/<path:company_name>", methods=["POST"])
def api_scan_company(company_name):
    if state["extracting"]:
        return jsonify({"message": "Extraction already running"}), 409
    start_extraction(company_name=company_name)
    return jsonify({"message": f"Extraction started for '{company_name}'"})


# ── Scraping triggers ─────────────────────────────────────────

@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    if state["scraping"]:
        return jsonify({"message": "Scraping already running"}), 409
    start_scraping(company_name=None)
    return jsonify({"message": "Scraping started for all companies"})


@app.route("/api/scrape/<path:company_name>", methods=["POST"])
def api_scrape_company(company_name):
    if state["scraping"]:
        return jsonify({"message": "Scraping already running"}), 409
    start_scraping(company_name=company_name)
    return jsonify({"message": f"Scraping started for '{company_name}'"})


# ── Filing calendar ───────────────────────────────────────────

@app.route("/api/calendar")
def api_calendar():
    calendars = downloader.get_all_filing_calendars()
    return jsonify({"calendars": calendars, "count": len(calendars)})


@app.route("/api/calendar/<path:company_name>")
def api_calendar_company(company_name):
    resolved = downloader._resolve_company(company_name)
    if not resolved:
        return jsonify({"error": f"Unknown company: {company_name}"}), 404
    cal = downloader.get_filing_calendar(resolved["bse_code"])
    return jsonify(cal or {})


@app.route("/api/approve/<token>")
def api_approve(token):
    """One-click approve link from email."""
    success, action, company_name = notifier.process_approval(
        token=token,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", "")
    )
    if not success:
        return _approval_response_html(
            "Link invalid or expired",
            "This link has already been used or has expired (7-day limit). "
            "Re-run extraction to generate a new review email.",
            success=False
        )
    return _approval_response_html(
        f"✓ {company_name} approved",
        "The company profile is now visible in the FinSight UI.",
        success=True
    )


@app.route("/api/reject/<token>")
def api_reject(token):
    """One-click reject link from email."""
    success, action, company_name = notifier.process_approval(
        token=token,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", "")
    )
    if not success:
        return _approval_response_html(
            "Link invalid or expired",
            "This link has already been used or has expired (7-day limit). "
            "Re-run extraction to generate a new review email.",
            success=False
        )
    return _approval_response_html(
        f"✗ {company_name} rejected",
        "The company profile has been hidden from the FinSight UI. "
        "You can re-run extraction and approve it later.",
        success=False
    )


@app.route("/api/email-reply", methods=["POST"])
def api_email_reply():
    """
    SendGrid Inbound Parse webhook.
    Called when master replies to rejection email.
    """
    # SendGrid posts these fields
    sender  = request.form.get("from", "")
    subject = request.form.get("subject", "")
    body    = request.form.get("text", "")    # plain text
    
    if not body:
        body = request.form.get("html", "")   # fallback to html

    log.info(f"Inbound reply — from: {sender} | subject: {subject}")

    if not sender or not subject:
        return jsonify({"status": "ignored"}), 200

    # process in background
    import threading
    from notifier import process_inbound_reply
    threading.Thread(
        target=process_inbound_reply,
        args=(sender, subject, body),
        daemon=True
    ).start()

    # must return 200 fast — SendGrid retries if timeout
    return jsonify({"status": "received"}), 200


def _approval_response_html(title: str, message: str, success: bool) -> str:
    """Simple HTML confirmation page shown after approve/reject click."""
    color  = "#1a7f4b" if success else "#c0392b"
    icon   = "✓" if success else "✗"
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>FinSight — {title}</title></head>
<body style="font-family: Arial, sans-serif; display: flex; align-items: center;
             justify-content: center; min-height: 100vh; margin: 0; background: #f5f5f5;">
  <div style="background: #fff; border-radius: 12px; padding: 48px 40px;
              max-width: 460px; text-align: center; border: 1px solid #e0e0e0;">
    <div style="font-size: 48px; color: {color}; margin-bottom: 16px;">{icon}</div>
    <h2 style="margin: 0 0 12px; color: #1a1a1a;">{title}</h2>
    <p style="color: #555; font-size: 14px; line-height: 1.6; margin: 0 0 24px;">{message}</p>
    <a href="/" style="font-size: 13px; color: #888; text-decoration: none;">← Back to FinSight</a>
  </div>
</body>
</html>"""


# ── Frontend static files ─────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


# =============================================================
# STARTUP
# =============================================================

def _startup():
    log.info("=" * 60)
    log.info("FinSight Financial Analysis Platform  v3")
    log.info(f"Supabase bucket: {os.getenv('SUPABASE_BUCKET', 'company-documents')}")
    log.info("QA engine: REMOVED — using company_profiles table")
    log.info("=" * 60)

    # Only run extraction on startup, not scraping
    start_extraction(company_name=None)

    # Start smart scheduler
    t = threading.Thread(target=_smart_schedule_loop, daemon=True)
    t.start()


_startup()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)