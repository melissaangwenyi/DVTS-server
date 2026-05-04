"""
server/data/email_service.py — Gmail SMTP host notifications

DESIGN: This module is FAIL-SAFE.
  - If env vars aren't set, send_host_notification() returns False silently.
  - If SMTP fails, errors are logged but never crash the check-in flow.
  - Every call is wrapped in try/except — a guard's check-in never fails
    because email is broken.

Required environment variables (set in Railway):
  SMTP_HOST       e.g. smtp.gmail.com
  SMTP_PORT       e.g. 587
  SMTP_USER       e.g. your-system@gmail.com
  SMTP_PASSWORD   16-char Gmail app password (NOT your normal password)
  SMTP_FROM_NAME  e.g. "VTS — University of Nairobi"  (optional)

If any of SMTP_HOST/SMTP_USER/SMTP_PASSWORD are missing, this module logs
"[Email] disabled — env vars not set" and every send returns False.
"""

import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr


def _email_enabled() -> bool:
    return all(
        os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")
    )


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
    Sends a notification email when a visitor checks in for a host.

    Returns True on send, False if disabled or failed.
    Never raises — check-in must keep working even if email blows up.
    """
    if not _email_enabled():
        print("[Email] disabled — env vars not set, skipping notification.")
        return False

    if not host_email or "@" not in host_email:
        print(f"[Email] no valid host email for {host_name}, skipping.")
        return False

    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASSWORD")
    from_name = os.environ.get("SMTP_FROM_NAME", "Visitor Tracking System")

    subject = f"Visitor arrived: {visitor_name}"
    text_body = (
        f"Hello {host_name},\n\n"
        f"A visitor has just checked in for you at {unit}.\n\n"
        f"Visitor: {visitor_name}\n"
        f"Category: {visitor_category}\n"
        f"Reason:   {reason or '—'}\n"
        f"Time:     {check_in_time}\n\n"
        f"This is an automated notification from the Visitor Tracking System.\n"
        f"Please proceed to receive your visitor."
    )
    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 540px; margin: 0 auto;
                background: #FAFAF7; padding: 24px; border-radius: 12px;">
      <div style="background: #008564; color: #fff; padding: 14px 20px;
                  border-radius: 10px; margin-bottom: 18px;">
        <div style="font-size: 13px; opacity: 0.9;">VISITOR ARRIVED</div>
        <div style="font-size: 18px; font-weight: 600; margin-top: 4px;">
          {visitor_name}
        </div>
      </div>
      <p style="color: #1A1A1A; line-height: 1.6;">Hello <strong>{host_name}</strong>,</p>
      <p style="color: #1A1A1A; line-height: 1.6;">
        A visitor has just checked in for you at
        <strong>{unit}</strong>.
      </p>
      <table style="width: 100%; border-collapse: collapse; margin: 14px 0;">
        <tr><td style="padding: 8px 0; color: #6B6B6B; font-size: 13px;">Visitor</td>
            <td style="padding: 8px 0; font-weight: 500; text-align: right;">{visitor_name}</td></tr>
        <tr><td style="padding: 8px 0; color: #6B6B6B; font-size: 13px;
                       border-top: 1px solid #EEEEE6;">Category</td>
            <td style="padding: 8px 0; font-weight: 500; text-align: right;
                       border-top: 1px solid #EEEEE6;">{visitor_category}</td></tr>
        <tr><td style="padding: 8px 0; color: #6B6B6B; font-size: 13px;
                       border-top: 1px solid #EEEEE6;">Reason</td>
            <td style="padding: 8px 0; font-weight: 500; text-align: right;
                       border-top: 1px solid #EEEEE6;">{reason or '—'}</td></tr>
        <tr><td style="padding: 8px 0; color: #6B6B6B; font-size: 13px;
                       border-top: 1px solid #EEEEE6;">Time</td>
            <td style="padding: 8px 0; font-weight: 500; text-align: right;
                       border-top: 1px solid #EEEEE6;">{check_in_time}</td></tr>
      </table>
      <p style="color: #6B6B6B; font-size: 12px; margin-top: 24px;
                border-top: 1px solid #EEEEE6; padding-top: 14px;">
        Please proceed to receive your visitor.<br>
        — Visitor Tracking System
      </p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr((from_name, smtp_user))
    msg["To"]      = host_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls(context=ctx)
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print(f"[Email] sent to {host_email} for visitor {visitor_name}")
        return True
    except Exception as e:
        print(f"[Email] send failed: {e}")
        return False