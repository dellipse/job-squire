# Copyright (C) 2026 D. Brandmeyer
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Outbound email notifications via the user-configured SMTP server."""
import html
import smtplib
import ssl
from email.message import EmailMessage


def send_email(smtp, subject, text_body, html_body=None, extra_to=None):
    """Send an email via the configured SMTP server.

    smtp is a dict: host, port, use_tls, username, password, from_addr, to_addr.
    extra_to is an optional list of additional recipient addresses.
    """
    recipients = [smtp["to_addr"]] if smtp.get("to_addr") else []
    for addr in (extra_to or []):
        addr = addr.strip()
        if addr and addr not in recipients:
            recipients.append(addr)
    if not recipients:
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp["from_addr"] or smtp["username"]
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    port = int(smtp.get("port") or 587)
    host = smtp["host"]
    if port == 465:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as s:
            _auth_send(s, smtp, msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            if smtp.get("use_tls", True):
                s.starttls(context=ssl.create_default_context())
            _auth_send(s, smtp, msg)


def _auth_send(s, smtp, msg):
    if smtp.get("username"):
        s.login(smtp["username"], smtp.get("password", ""))
    s.send_message(msg)


def build_digest(jobs, base_url=None, recipient_name="you"):
    """Return (subject, text, html) for a batch of newly found jobs."""
    n = len(jobs)
    subject = f"{n} new job match{'es' if n != 1 else ''} for {recipient_name}"
    lines = [f"{n} new posting{'s' if n != 1 else ''} added to your Job Squire instance:\n"]
    rows = []
    for j in jobs:
        sal = f" — {j.salary}" if j.salary else ""
        loc = f" ({j.location})" if j.location else ""
        lines.append(f"• {j.title} at {j.company}{loc}{sal}")
        if j.url:
            lines.append(f"  {j.url}")
        link = j.url or "#"
        rows.append(
            f'<tr><td style="padding:8px 0;border-bottom:1px solid #eee">'
            f'<strong>{html.escape(j.title)}</strong><br>{html.escape(j.company)}{html.escape(loc)}{html.escape(sal)}<br>'
            f'<a href="{html.escape(link)}">View posting</a> · source: {html.escape(j.source)}</td></tr>'
        )
    if base_url:
        lines.append(f"\nReview and triage them here: {base_url}")
    text = "\n".join(lines)
    html = (
        '<div style="font-family:Arial,sans-serif;font-size:14px;color:#222">'
        f"<p>{n} new posting{'s' if n != 1 else ''} added to your Job Squire instance:</p>"
        f'<table style="width:100%;border-collapse:collapse">{"".join(rows)}</table>'
        + (f'<p><a href="{base_url}">Open Job Squire</a></p>' if base_url else "")
        + "</div>"
    )
    return subject, text, html


def build_followup_digest(drafted_jobs, base_url=None, recipient_name="you"):
    """Return (subject, text, html) for a batch of auto-drafted follow-up emails."""
    n = len(drafted_jobs)
    subject = f"{n} follow-up draft{'s' if n != 1 else ''} ready to review"
    lines = [f"{n} follow-up email draft{'s' if n != 1 else ''} were written automatically:\n"]
    rows = []
    for j in drafted_jobs:
        company = j.get("company", "")
        title = j.get("title", "")
        jid = j.get("id")
        lines.append(f"  {title} at {company}")
        link = f"{base_url}/jobs/{jid}" if (base_url and jid) else "#"
        rows.append(
            f'<tr><td style="padding:8px 0;border-bottom:1px solid #eee">'
            f'<strong>{html.escape(title)}</strong> at {html.escape(company)}<br>'
            f'<a href="{html.escape(link)}">Review draft</a></td></tr>'
        )
    lines.append("\nReview and personalize each draft before sending.")
    if base_url:
        lines.append(f"Open Job Squire here: {base_url}")
    text = "\n".join(lines)
    html_body = (
        '<div style="font-family:Arial,sans-serif;font-size:14px;color:#222">'
        f"<p>{n} follow-up email draft{'s' if n != 1 else ''} are ready for your review:</p>"
        f'<table style="width:100%;border-collapse:collapse">{"".join(rows)}</table>'
        "<p style='margin-top:12px;color:#666;font-size:13px'>"
        "Review each draft in Job Squire, personalize if needed, then send.</p>"
        + (f'<p><a href="{base_url}">Open Job Squire</a></p>' if base_url else "")
        + "</div>"
    )
    return subject, text, html_body


def build_weekly_review_email(summary, recommendations, base_url=None, recipient_name="you"):
    """Return (subject, text, html) for the weekly strategy review."""
    subject = "Weekly job search review"
    rec_lines = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(recommendations or []))
    text = f"Weekly Job Search Review\n\n{summary}"
    if rec_lines:
        text += f"\n\nACTION ITEMS FOR THIS WEEK:\n{rec_lines}"
    if base_url:
        text += f"\n\nReview past insights: {base_url}/ai"

    rec_items = "".join(
        f'<li style="margin:6px 0">{html.escape(str(r))}</li>'
        for r in (recommendations or [])
    )
    html_body = (
        '<div style="font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:640px">'
        '<h2 style="font-size:18px;margin-bottom:8px">Weekly Job Search Review</h2>'
        f'<p style="line-height:1.6;white-space:pre-wrap">{html.escape(summary)}</p>'
    )
    if rec_items:
        html_body += (
            '<h3 style="font-size:15px;margin-top:16px">Action items for this week</h3>'
            f'<ol style="margin:.5rem 0;padding-left:1.4rem">{rec_items}</ol>'
        )
    if base_url:
        html_body += f'<p style="margin-top:12px"><a href="{base_url}/ai">View all AI insights</a></p>'
    html_body += "</div>"
    return subject, text, html_body


def build_rejection_alert_email(summary, recommendations, rejection_count, base_url=None):
    """Return (subject, text, html) for a rejection pattern alert."""
    subject = f"Job search alert: pattern detected in rejections ({rejection_count} recent)"
    rec_lines = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(recommendations or []))
    text = f"Rejection Pattern Alert\n\n{summary}"
    if rec_lines:
        text += f"\n\nRECOMMENDED CHANGES:\n{rec_lines}"
    if base_url:
        text += f"\n\nFull analysis: {base_url}/ai"

    rec_items = "".join(
        f'<li style="margin:6px 0">{html.escape(str(r))}</li>'
        for r in (recommendations or [])
    )
    html_body = (
        '<div style="font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:640px">'
        '<h2 style="font-size:18px;margin-bottom:8px;color:#c0392b">Job Search Alert: Rejection Pattern Detected</h2>'
        f'<p style="margin-bottom:12px;color:#666">{rejection_count} rejections detected in the past 14 days.</p>'
        f'<p style="line-height:1.6;white-space:pre-wrap">{html.escape(summary)}</p>'
    )
    if rec_items:
        html_body += (
            '<h3 style="font-size:15px;margin-top:16px">Recommended changes</h3>'
            f'<ol style="margin:.5rem 0;padding-left:1.4rem">{rec_items}</ol>'
        )
    if base_url:
        html_body += f'<p style="margin-top:12px"><a href="{base_url}/ai">View full analysis</a></p>'
    html_body += "</div>"
    return subject, text, html_body


def build_error_report(errors, trigger="scheduled", base_url=None):
    """Return (subject, text, html) for a search run that produced errors."""
    subject = f"JobSquire: search errors in {trigger} run"
    lines = [f"The {trigger} search run encountered {len(errors)} issue(s):\n"]
    items = ""
    for e in errors:
        lines.append(f"  • {e}")
        items += f'<li style="margin:4px 0">{html.escape(e)}</li>'
    if base_url:
        lines.append(f"\nCheck run history: {base_url}/settings")
    text = "\n".join(lines)
    html = (
        '<div style="font-family:Arial,sans-serif;font-size:14px;color:#222">'
        f"<p>The <strong>{html.escape(trigger)}</strong> search run encountered {len(errors)} issue(s):</p>"
        f"<ul style='margin:.5rem 0;padding-left:1.2rem'>{items}</ul>"
        + (f'<p><a href="{base_url}/settings">View run history</a></p>' if base_url else "")
        + "</div>"
    )
    return subject, text, html
