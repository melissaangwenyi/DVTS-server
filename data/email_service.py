"""
server/data/email_service.py — Email notifications via Gmail REST API

Uses Gmail API over HTTPS (port 443) — works on Railway free tier which
blocks outbound SMTP (port 587).

Required environment variables:
  SMTP_USER       your Gmail address  e.g. you@gmail.com
  SMTP_PASSWORD   Gmail App Password  (16 chars, NO spaces)
  SMTP_FROM_NAME  Display name        e.g. "VTS — University of Nairobi"

How it works:
  Gmail allows sending via REST API using Basic Auth with an App Password.
  We encode the message as RFC-2822, base64url it, and POST to:
  https://gmail.googleapis.com/gmail/v1/users/me/messages/send
  No extra libraries needed — just urllib (stdlib).
"""

import os
import base64
import json
import urllib.request
import urllib.error
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr


def _config():
    return {
        "user":      os.environ.get("SMTP_USER", ""),
        "password":  os.environ.get("SMTP_PASSWORD", "").replace(" ", ""),
        "from_name": os.environ.get("SMTP_FROM_NAME", "Visitor Tracking System"),
    }


def _enabled() -> bool:
    c = _config()
    return bool(c["user"] and c["password"])


def send_host_notification(
    host_email: str,
    host_name: str,
    visitor_name: str,
    visitor_category: str,
    unit: str,
    check_in_time: str,
    reason: str = None,
) -> bool:
    """
    Sends a visitor-arrival notification to the host via Gmail REST API.
    Returns True on success, False if disabled or failed.
    Never raises — check-in must keep working even if email blows up.
    """
    if not _enabled():
        print("[Email] disabled — SMTP_USER or SMTP_PASSWORD not set.")
        return False

    if not host_email or "@" not in host_email:
        print(f"[Email] invalid host email: {host_email!r}")
        return False

    c = _config()

    # ── Build message ──────────────────────────────────────────────────
    subject   = f"Visitor arrived: {visitor_name}"
    text_body = (
        f"Hello {host_name},\n\n"
        f"A visitor has checked in for you at {unit}.\n\n"
        f"Visitor:  {visitor_name}\n"
        f"Category: {visitor_category}\n"
        f"Reason:   {reason or '—'}\n"
        f"Time:     {check_in_time}\n\n"
        f"Please proceed to receive your visitor.\n"
        f"— Visitor Tracking System"
    )
    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;
                background:#FAFAF7;padding:24px;border-radius:12px;">
      <div style="background:#008564;color:#fff;padding:14px 20px;
                  border-radius:10px;margin-bottom:18px;">
        <div style="font-size:12px;opacity:.85;">VISITOR ARRIVED</div>
        <div style="font-size:18px;font-weight:700;margin-top:4px;">{visitor_name}</div>
      </div>
      <p style="color:#1A1A1A;line-height:1.6;margin-bottom:14px;">
        Hello <strong>{host_name}</strong>, a visitor has checked in for you
        at <strong>{unit}</strong>.
      </p>
      <table style="width:100%;border-collapse:collapse;">
        <tr><td style="padding:7px 0;color:#6B6B6B;font-size:13px;">Visitor</td>
            <td style="padding:7px 0;font-weight:600;text-align:right;">{visitor_name}</td></tr>
        <tr style="border-top:1px solid #EEEEE6;">
            <td style="padding:7px 0;color:#6B6B6B;font-size:13px;">Category</td>
            <td style="padding:7px 0;font-weight:600;text-align:right;">{visitor_category}</td></tr>
        <tr style="border-top:1px solid #EEEEE6;">
            <td style="padding:7px 0;color:#6B6B6B;font-size:13px;">Reason</td>
            <td style="padding:7px 0;text-align:right;">{reason or '—'}</td></tr>
        <tr style="border-top:1px solid #EEEEE6;">
            <td style="padding:7px 0;color:#6B6B6B;font-size:13px;">Time</td>
            <td style="padding:7px 0;text-align:right;">{check_in_time}</td></tr>
      </table>
      <p style="color:#9A9A92;font-size:11px;margin-top:20px;
                border-top:1px solid #EEEEE6;padding-top:12px;">
        Visitor Tracking System — University of Nairobi
      </p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr((c["from_name"], c["user"]))
    msg["To"]      = host_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    # ── Encode as base64url (Gmail API requirement) ────────────────────
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    # ── POST to Gmail REST API using App Password (Basic Auth) ─────────
    credentials = base64.b64encode(
        f"{c['user']}:{c['password']}".encode()
    ).decode("utf-8")

    payload = json.dumps({"raw": raw}).encode("utf-8")
    url     = f"https://gmail.googleapis.com/gmail/v1/users/{c['user']}/messages/send"

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            print(f"[Email] sent OK → {host_email} | response: {body[:80]}")
            return True
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        print(f"[Email] HTTP {e.code}: {err_body[:200]}")
        return False
    except Exception as e:
        print(f"[Email] send error: {e}")
        return False