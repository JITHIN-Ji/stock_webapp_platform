"""
notifier.py
===========
Sends a compact summary email to the stock master after each extraction.
Provides one-click Approve / Reject links — no login required.

Requires in .env:
    SENDGRID_API_KEY=SG.xxxx
    STOCK_MASTER_EMAIL=master@example.com
    FROM_EMAIL=noreply@yourdomain.com
    BASE_URL=https://your-server.com          # used to build approve/reject URLs

Supabase schema change needed (run once):
    ALTER TABLE company_profiles
        ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
    CREATE INDEX IF NOT EXISTS idx_cp_status ON company_profiles (status);
"""

import os
import logging
import secrets
import datetime
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, ReplyTo, Header
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

SENDGRID_API_KEY    = os.getenv("SENDGRID_API_KEY", "")
STOCK_MASTER_EMAIL  = os.getenv("STOCK_MASTER_EMAIL", "")
FROM_EMAIL          = os.getenv("FROM_EMAIL", "noreply@finsight.com")
BASE_URL            = os.getenv("BASE_URL", "http://localhost:5000").rstrip("/")
COMPANY_HOLDER_EMAIL = os.getenv("COMPANY_HOLDER_EMAIL", "")


# ── Database token helpers ─────────────────────────────

def _generate_token(bse_code: str, action: str) -> str:
    """
    Invalidate any existing unused tokens for this bse_code + action,
    then create a fresh one. This ensures old emails cannot be clicked
    after a new scrape has run.
    """
    # Invalidate all previous unused tokens for this company + action
    try:
        supabase.table("approval_tokens") \
            .update({"used": True}) \
            .eq("bse_code", bse_code) \
            .eq("action", action) \
            .eq("used", False) \
            .execute()
        log.debug(f"Invalidated old {action} tokens for {bse_code}")
    except Exception as e:
        log.warning(f"Could not invalidate old tokens for {bse_code}: {e}")

    # Generate fresh token
    token = secrets.token_urlsafe(32)
    expires_at = (
        datetime.datetime.utcnow() +
        datetime.timedelta(days=7)
    )

    supabase.table("approval_tokens").insert({
        "token":      token,
        "bse_code":   bse_code,
        "action":     action,
        "used":       False,
        "expires_at": expires_at.isoformat(),
    }).execute()

    return token


def consume_token(token: str) -> dict | None:
    """
    Validate and consume token from DB.
    """
    try:
        res = supabase.rpc(
            "consume_approval_token",
            {"p_token": token}
        ).execute()

        if not res.data:
            return None  # expired, used, or not found

        return {
            "bse_code": res.data[0]["bse_code"],
            "action":   res.data[0]["action"],
        }
    except Exception as e:
        log.error(f"consume_token error: {e}")
        return None


# ── Email builder ─────────────────────────────────────────────

def _fmt_cr(value) -> str:
    """Format a number as ₹ Cr or return —."""
    if value is None:
        return "—"
    try:
        v = float(value)
        if abs(v) >= 1000:
            return f"₹{v/1000:.1f}K Cr"
        return f"₹{v:.0f} Cr"
    except Exception:
        return str(value)


def _fmt_pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.1f}%"
    except Exception:
        return str(value)


def _build_email_html(profile: dict, approve_url: str, reject_url: str,
                      regeneration_note: str = "") -> str:
    name    = profile.get("company_name", "Unknown")
    bse     = profile.get("bse_code", "")
    nse     = profile.get("nse_symbol", "")
    quarter = profile.get("latest_quarter", "—")
    year    = profile.get("latest_year", "—")
    verdict = profile.get("verdict", "No verdict generated.")

    # ── Latest annual P&L ─────────────────────────────────
    annual_pl   = profile.get("annual_pl") or []
    latest_year = annual_pl[0] if annual_pl else {}

    # ── Latest quarter ────────────────────────────────────
    quarterly    = profile.get("quarterly_data") or []
    latest_q     = quarterly[0] if quarterly else {}

    # ── Latest balance sheet ──────────────────────────────
    balance      = profile.get("balance_sheet") or []
    latest_bs    = balance[0] if balance else {}

    # ── Latest ratios ─────────────────────────────────────
    ratios       = profile.get("ratios") or []
    latest_r     = ratios[0] if ratios else {}

    # ── Shareholding ──────────────────────────────────────
    sh           = profile.get("shareholding") or []
    latest_sh    = sh[0] if sh else {}

    # ── Segments ──────────────────────────────────────────
    segments     = profile.get("segments") or []
    seg_rows = "".join(
        f"<tr><td style='padding:3px 16px 3px 0;color:#666;font-size:12px;'>{s.get('name','')}</td>"
        f"<td style='padding:3px 0;font-size:12px;font-weight:500;'>{s.get('percentage','')}%</td></tr>"
        for s in segments[:5]
    )

    # ── Recent announcements ──────────────────────────────
    announcements = profile.get("recent_announcements") or []
    ann_rows = "".join(
        f"<tr><td style='padding:3px 16px 3px 0;color:#888;font-size:11px;width:80px;'>{a.get('date','')}</td>"
        f"<td style='padding:3px 0;font-size:12px;'>{a.get('title','')} — "
        f"<span style='color:#666;'>{a.get('summary','')[:80]}</span></td></tr>"
        for a in announcements[:3]
    )

    def cr(v):
        if v is None: return "—"
        try:
            f = float(v)
            return f"₹{f/1000:.1f}K Cr" if abs(f) >= 1000 else f"₹{f:.0f} Cr"
        except: return str(v)

    def pct(v, suffix="%"):
        if v is None: return "—"
        try: return f"{float(v):.1f}{suffix}"
        except: return str(v)

    def num(v):
        if v is None: return "—"
        try: return f"{float(v):.1f}"
        except: return str(v)

    def section(title):
        return f"<p style='font-size:11px;font-weight:600;color:#888;letter-spacing:0.08em;margin:20px 0 6px;text-transform:uppercase;'>{title}</p>"

    rows_annual = [
        ("Revenue",          cr(latest_year.get("revenue"))),
        ("Net Profit",       cr(latest_year.get("net_profit"))),
        ("Operating Profit", cr(latest_year.get("operating_profit"))),
        ("OPM",              pct(latest_year.get("opm_percent"))),
        ("Net Margin",       pct(profile.get("latest_net_margin"))),
        ("EPS",              f"₹{num(latest_year.get('eps'))}"),
    ]
    rows_returns = [
        ("ROE",              pct(profile.get("latest_roe"))),
        ("ROCE",             pct(profile.get("latest_roce"))),
        ("D/E Ratio",        f"{num(profile.get('latest_debt_to_equity'))}x"),
        ("Interest Cover",   f"{num(latest_r.get('interest_coverage'))}x"),
        ("Current Ratio",    num(latest_r.get("current_ratio"))),
    ]
    rows_quarter = [
        ("Revenue",          cr(latest_q.get("revenue"))),
        ("Net Profit",       cr(latest_q.get("net_profit"))),
        ("OPM",              pct(latest_q.get("opm_percent"))),
        ("EPS",              f"₹{num(latest_q.get('eps'))}"),
    ]
    rows_bs = [
        ("Total Assets",     cr(latest_bs.get("total_assets"))),
        ("Borrowings",       cr(latest_bs.get("borrowings"))),
        ("Reserves",         cr(latest_bs.get("reserves"))),
        ("CWIP",             cr(latest_bs.get("cwip"))),
        ("Free Cash Flow",   cr((profile.get("cash_flow") or [{}])[0].get("free_cash_flow"))),
    ]

    def tbl(rows):
        return (
            "<table style='width:100%;border-collapse:collapse;margin-bottom:4px;'>"
            + "".join(
                f"<tr><td style='padding:4px 16px 4px 0;color:#666;font-size:12px;'>{k}</td>"
                f"<td style='padding:4px 0;font-size:12px;font-weight:500;'>{v}</td></tr>"
                for k, v in rows
            )
            + "</table>"
        )

    # shareholding bar
    sh_html = ""
    if latest_sh:
        sh_html = f"""
        <p style='font-size:12px;color:#444;margin:6px 0 2px;'>
          Promoter <b>{pct(latest_sh.get('promoter'))}</b> &nbsp;|&nbsp;
          FII <b>{pct(latest_sh.get('fii'))}</b> &nbsp;|&nbsp;
          DII <b>{pct(latest_sh.get('dii'))}</b> &nbsp;|&nbsp;
          Public <b>{pct(latest_sh.get('public'))}</b>
          &nbsp;<span style='color:#aaa;font-size:11px;'>({latest_sh.get('quarter','')})</span>
        </p>"""

    table_rows = "".join(
        f"<tr><td style='padding:6px 16px 6px 0;color:#666;font-size:13px;'>{k}</td>"
        f"<td style='padding:6px 0;font-size:13px;font-weight:500;'>{v}</td></tr>"
        for k, v in rows_annual
    )

    return f"""
<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;
             color: #1a1a1a; background: #f5f5f5; padding: 24px;">
  <div style="background:#fff; border-radius:12px; padding:32px; border:1px solid #e0e0e0;">

    <p style="font-size:12px; color:#888; margin:0 0 4px;">
      FinSight · New extraction ready for review
    </p>

    <!-- ← THIS LINE tells master it may have been live before -->
    <p style="font-size:12px; color:#e67e22; margin:0 0 16px;">
      ⚠ This company is currently hidden from the UI pending your approval.
    </p>
    {regeneration_note}

    <h2 style="margin:0 0 4px; font-size:22px;">{name}</h2>
    <p style="margin:0 0 24px; color:#666; font-size:13px;">
      BSE: {bse} &nbsp;|&nbsp; NSE: {nse} &nbsp;|&nbsp; 
      Latest: {quarter} / {year}
    </p>

    <table style="width:100%; border-collapse:collapse; margin-bottom:24px;">
      {table_rows}
    </table>

    <div style="background:#f8f8f8; border-left:3px solid #4a4a4a;
                padding:12px 16px; border-radius:0 8px 8px 0; margin-bottom:28px;">
      <p style="margin:0; font-size:13px; line-height:1.6; color:#333;">
        {verdict}
      </p>
    </div>

    <div>
      <a href="{approve_url}"
         style="display:inline-block; padding:12px 28px; background:#1a7f4b;
                color:#fff; text-decoration:none; border-radius:8px;
                font-weight:600; font-size:14px; margin-right:12px;">
        ✓ Approve — Make Visible
      </a>
      <a href="{reject_url}"
         style="display:inline-block; padding:12px 28px; background:#c0392b;
                color:#fff; text-decoration:none; border-radius:8px;
                font-weight:600; font-size:14px;">
        ✗ Reject — Keep Hidden
      </a>
    </div>

    <p style="font-size:11px; color:#aaa; margin:24px 0 0;">
      Links expire in 7 days. Company stays hidden until you approve.
    </p>
  </div>
</body>
</html>
"""
def _send_single_email(profile: dict, to_email: str,
                       regeneration_note: str = "") -> bool:
    bse_code     = profile.get("bse_code", "")
    company_name = profile.get("company_name", "Unknown Company")

    approve_token = _generate_token(bse_code, "approved")
    reject_token  = _generate_token(bse_code, "rejected")
    approve_url   = f"{BASE_URL}/api/approve/{approve_token}"
    reject_url    = f"{BASE_URL}/api/reject/{reject_token}"

    html_content = _build_email_html(profile, approve_url, reject_url,
                                  regeneration_note=regeneration_note)
    import uuid
    message_id = f"<review-{bse_code}-{uuid.uuid4()}@wingoraventures.com>"

    message = Mail(
        from_email   = FROM_EMAIL,
        to_emails    = to_email,
        subject      = f"[FinSight Review] {company_name} (BSE:{bse_code})",
        html_content = html_content,
    )
    message.reply_to = ReplyTo(
        email = os.getenv("REPLY_TO_EMAIL", "reply@reply.wingoraventures.com"),
        name  = "FinSight Review"
    )
    message.header = Header("Message-ID", message_id)

    try:
        supabase.table("company_profiles") \
            .update({"review_message_id": message_id}) \
            .eq("bse_code", bse_code).execute()
    except Exception as e:
        log.warning(f"Could not save message_id: {e}")

    try:
        sg   = SendGridAPIClient(SENDGRID_API_KEY)
        resp = sg.send(message)
        log.info(f"Email sent to {to_email} — {resp.status_code}")
        return resp.status_code in (200, 202)
    except Exception as e:
        log.error(f"SendGrid error to {to_email}: {e}")
        return False


# ── Public API ────────────────────────────────────────────────

def notify_stock_master(profile: dict,
                        send_to_holder: bool = False) -> bool:
    if not SENDGRID_API_KEY or not STOCK_MASTER_EMAIL:
        log.warning("SendGrid or master email not set")
        return False

    bse_code = profile.get("bse_code", "")
    if not bse_code:
        return False

    # always send to stockmaster
    note = """
    <div style="background:#fff3cd; border-left:3px solid #e67e22;
                padding:10px 16px; border-radius:0 8px 8px 0; margin-bottom:16px;">
    <p style="margin:0; font-size:12px; color:#856404;">
        ♻ <b>Regenerated Profile</b> — Updated based on previous rejection feedback.
        Please review the changes.
    </p>
    </div>
    """ if send_to_holder else ""

    sent = _send_single_email(profile, STOCK_MASTER_EMAIL,
                            regeneration_note=note)

    if send_to_holder and COMPANY_HOLDER_EMAIL:
        _send_single_email(profile, COMPANY_HOLDER_EMAIL,
                        regeneration_note=note)
        log.info(f"Also sent to holder: {COMPANY_HOLDER_EMAIL}")
            
    return sent


def process_approval(
    token: str,
    ip_address: str = "",
    user_agent: str = "",
) -> tuple[bool, str, str]:
    entry = consume_token(token)

    if not entry:
        return False, "", ""

    bse_code = entry["bse_code"]
    action = entry["action"]

    try:
        # Update profile status
        if action == "approved":
            res = supabase.table("company_profiles") \
                .update({"status": "approved"}) \
                .eq("bse_code", bse_code) \
                .execute()
        else:
            # only reject if NOT already approved by someone else
            res = supabase.table("company_profiles") \
                .update({"status": "rejected"}) \
                .eq("bse_code", bse_code) \
                .neq("status", "approved") \
                .execute()
            
            # if already approved → fetch current data for response
            if not res.data:
                res = supabase.table("company_profiles") \
                    .select("*") \
                    .eq("bse_code", bse_code) \
                    .execute()

        if not res.data:
            return False, action, bse_code

        profile = res.data[0]

        company_name = profile.get(
            "company_name",
            bse_code
        )

        # Write audit log
        supabase.table("approval_logs").insert({
            "bse_code": bse_code,
            "company_name": company_name,
            "action": action,
            "ip_address": ip_address,
            "user_agent": user_agent,
        }).execute()

        log.info(
            f"Approval processed: "
            f"{company_name} -> {action}"
        )

        return True, action, company_name

    except Exception as e:
        log.error(f"process_approval error: {e}")
        return False, action, bse_code




def process_inbound_reply(sender: str, subject: str, body: str) -> bool:
    import re, threading

    # security check
    allowed = [e for e in [STOCK_MASTER_EMAIL, COMPANY_HOLDER_EMAIL] if e]
    if not any(a.lower() in sender.lower() for a in allowed):
        log.warning(f"Unknown sender: {sender}")
        return False

    # extract BSE code
    match = re.search(r'BSE:(\d+)', subject)
    if not match:
        log.warning(f"No BSE code in subject: {subject}")
        return False

    bse_code = match.group(1)

    # fetch profile
    try:
        res = supabase.table("company_profiles") \
            .select("status, rejection_count, master_feedback, holder_feedback") \
            .eq("bse_code", bse_code) \
            .limit(1).execute()

        if not res.data:
            log.warning(f"No profile BSE:{bse_code}")
            return False

        profile  = res.data[0]
        status   = profile.get("status")
        rej_count = profile.get("rejection_count") or 0

        if status != "rejected":
            log.info(f"BSE:{bse_code} status={status} not rejected — skip")
            return False

    except Exception as e:
        log.error(f"DB fetch error: {e}")
        return False

    # identify sender
    is_master = STOCK_MASTER_EMAIL.lower() in sender.lower()
    is_holder = bool(COMPANY_HOLDER_EMAIL) and \
                COMPANY_HOLDER_EMAIL.lower() in sender.lower()

    # ── ROUND 1: only master got email ────────────────────
    if rej_count == 0:
        # only master reply expected → regenerate immediately
        if is_master:
            log.info(f"Round 1 master feedback BSE:{bse_code} → regenerating")
            import extractor
            threading.Thread(
                target=extractor.regenerate_with_feedback,
                args=(bse_code, body, 1),
                daemon=True
            ).start()
            return True
        else:
            log.info(f"Round 1: holder replied but only master expected — ignore")
            return False

    # ── ROUND 2+: both got email ──────────────────────────
    # save this person's feedback
    update_field = {}
    if is_master:
        update_field["master_feedback"] = body
        log.info(f"Master feedback saved BSE:{bse_code}")
    elif is_holder:
        update_field["holder_feedback"] = body
        log.info(f"Holder feedback saved BSE:{bse_code}")

    try:
        supabase.table("company_profiles") \
            .update(update_field) \
            .eq("bse_code", bse_code).execute()
    except Exception as e:
        log.error(f"Save feedback error: {e}")
        return False

    # re-fetch to check if both replied
    try:
        res2 = supabase.table("company_profiles") \
            .select("master_feedback, holder_feedback") \
            .eq("bse_code", bse_code) \
            .limit(1).execute()
        row = res2.data[0]
    except Exception as e:
        log.error(f"Re-fetch error: {e}")
        return False

    master_fb = row.get("master_feedback") or ""
    holder_fb = row.get("holder_feedback") or ""

    # if holder email not configured → only need master
    if not COMPANY_HOLDER_EMAIL:
        if not master_fb:
            return True
        combined = master_fb
    else:
        # need both
        if not master_fb or not holder_fb:
            log.info(
                f"BSE:{bse_code} waiting for both — "
                f"master:{bool(master_fb)} holder:{bool(holder_fb)}"
            )
            return True  # wait for other person
        combined = (
            f"Stock Master feedback:\n{master_fb}"
            f"\n\nCompany Holder feedback:\n{holder_fb}"
        )

    # clear feedbacks + increment count
    new_count = rej_count + 1
    try:
        supabase.table("company_profiles") \
            .update({
                "rejection_count": new_count,
                "master_feedback":  None,
                "holder_feedback":  None,
            }) \
            .eq("bse_code", bse_code).execute()
    except Exception:
        pass

    # trigger regeneration
    import extractor
    threading.Thread(
        target=extractor.regenerate_with_feedback,
        args=(bse_code, combined, new_count),
        daemon=True
    ).start()

    log.info(f"Regeneration triggered BSE:{bse_code} round:{new_count}")
    return True