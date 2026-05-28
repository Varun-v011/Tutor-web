"""
services/email_service.py
──────────────────────────
Transactional email delivery — two providers supported:

  "gmail"   — Uses stdlib smtplib + SSL. Requires a Gmail App Password.
              No external dependencies. Great for personal tutors.

  "resend"  — Uses the Resend API (https://resend.com).
              Supports custom domains & higher deliverability.

Set EMAIL_PROVIDER in .env to switch between them.

Public API:
  send_booking_confirmation(student_email, student_name, language,
                            meet_link, booking_start, graded_result)
    → bool   True if sent successfully, False on error.
"""

import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from typing import Optional

import resend

from config.settings import settings
from models.lead import GradedResult

logger = logging.getLogger(__name__)


# ── Email template builder ────────────────────────────────────────────────────

def _build_email_content(
    student_name: str,
    language: str,
    meet_link: Optional[str],
    booking_start: Optional[datetime],
    graded_result: Optional[GradedResult],
    tutor_name: str,
) -> tuple[str, str]:
    """
    Build plain-text and HTML versions of the booking confirmation email.

    Returns:
        (plain_text, html_body) as a tuple of strings.
    """
    # Format the booking time nicely
    time_str = (
        booking_start.strftime("%A, %B %d %Y at %I:%M %p (%Z)")
        if booking_start
        else "as scheduled"
    )

    # Score section — only shown when a grading result is available
    if graded_result:
        score_section_plain = (
            f"\n📊 YOUR QUIZ RESULTS\n"
            f"{'─' * 40}\n"
            f"  Overall Score  : {graded_result.overall_score}/100\n"
            f"  CEFR Level     : {graded_result.cefr_level}\n"
            f"  Key Strengths  : {', '.join(graded_result.strengths)}\n"
            f"  Focus Areas    : {', '.join(graded_result.weaknesses)}\n"
            f"\n  💡 {graded_result.recommendation}\n"
        )
        score_section_html = f"""
        <div style="background:#f8f6f0;border-left:4px solid #C9973A;padding:16px;margin:20px 0;border-radius:0 8px 8px 0;">
          <h3 style="margin:0 0 12px;color:#0D1B2A;font-size:15px;">📊 Your Quiz Results</h3>
          <table style="font-size:14px;color:#374151;width:100%;">
            <tr><td style="padding:3px 0;font-weight:600;width:140px;">Overall Score</td>
                <td>{graded_result.overall_score}/100</td></tr>
            <tr><td style="padding:3px 0;font-weight:600;">CEFR Level</td>
                <td><strong>{graded_result.cefr_level}</strong></td></tr>
            <tr><td style="padding:3px 0;font-weight:600;vertical-align:top;">Strengths</td>
                <td>{"<br>".join("✅ " + s for s in graded_result.strengths)}</td></tr>
            <tr><td style="padding:3px 0;font-weight:600;vertical-align:top;">Focus Areas</td>
                <td>{"<br>".join("🎯 " + w for w in graded_result.weaknesses)}</td></tr>
          </table>
          <p style="margin:12px 0 0;font-size:13px;color:#6B7280;font-style:italic;">
            💡 {graded_result.recommendation}
          </p>
        </div>
        """
    else:
        score_section_plain = ""
        score_section_html  = ""

    # Meet link block
    meet_block_plain = (
        f"\n🎥 JOIN YOUR SESSION\n"
        f"{'─' * 40}\n"
        f"  {meet_link}\n"
        if meet_link
        else "\n  Your teacher will share the join link before the session.\n"
    )
    meet_block_html = (
        f"""
        <div style="text-align:center;margin:24px 0;">
          <a href="{meet_link}"
             style="background:#0D1B2A;color:white;text-decoration:none;
                    padding:14px 32px;border-radius:10px;font-weight:600;
                    font-size:15px;display:inline-block;">
            🎥 Join Google Meet
          </a>
        </div>
        """
        if meet_link
        else "<p style='color:#6B7280;text-align:center;'>Your teacher will share the join link before the session.</p>"
    )

    # ── Plain text ────────────────────────────────────────────────────────────
    plain = f"""Hello {student_name},

Your {language} tutoring session is confirmed! 🎉

📅 DATE & TIME
{'─' * 40}
  {time_str}
{meet_block_plain}
{score_section_plain}
If you need to reschedule or have any questions, simply reply to this email.

See you soon!
{tutor_name}

──────────────────────────────────────
This email was sent by the Lingua Tutor platform.
"""

    # ── HTML ──────────────────────────────────────────────────────────────────
    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
</head>
<body style="margin:0;padding:0;background:#F5F0E8;font-family:'Outfit',Arial,sans-serif;">

  <table width="100%" cellpadding="0" cellspacing="0">
  <tr><td align="center" style="padding:32px 16px;">
  <table width="600" cellpadding="0" cellspacing="0"
         style="max-width:600px;width:100%;background:white;
                border-radius:16px;overflow:hidden;
                box-shadow:0 4px 24px rgba(0,0,0,0.08);">

    <!-- Header -->
    <tr><td style="background:#0D1B2A;padding:28px 32px;text-align:center;">
      <h1 style="margin:0;color:white;font-size:22px;font-weight:700;letter-spacing:0.5px;">
        Lingua<span style="color:#C9973A;">.</span>
      </h1>
      <p style="margin:6px 0 0;color:rgba(255,255,255,0.5);font-size:12px;
                text-transform:uppercase;letter-spacing:1.5px;">
        Language Tutoring
      </p>
    </td></tr>

    <!-- Body -->
    <tr><td style="padding:32px;">
      <h2 style="margin:0 0 6px;color:#0D1B2A;font-size:20px;">
        Your session is confirmed! 🎉
      </h2>
      <p style="margin:0 0 20px;color:#6B7280;font-size:14px;">
        Hello <strong>{student_name}</strong>, here are your booking details for your
        <strong>{language}</strong> lesson.
      </p>

      <!-- Session time -->
      <div style="background:#F5F0E8;border-radius:10px;padding:16px;margin-bottom:16px;">
        <p style="margin:0;font-size:13px;color:#9CA3AF;text-transform:uppercase;
                  letter-spacing:1px;font-weight:600;">📅 Session Time</p>
        <p style="margin:6px 0 0;font-size:16px;font-weight:700;color:#0D1B2A;">
          {time_str}
        </p>
      </div>

      {meet_block_html}
      {score_section_html}

      <hr style="border:none;border-top:1px solid #EDE6D9;margin:24px 0;"/>
      <p style="margin:0;font-size:13px;color:#9CA3AF;text-align:center;">
        Questions? Reply directly to this email.
      </p>
    </td></tr>

    <!-- Footer -->
    <tr><td style="background:#F5F0E8;padding:20px 32px;text-align:center;">
      <p style="margin:0;font-size:12px;color:#9CA3AF;">
        Sent by <strong style="color:#0D1B2A;">{tutor_name}</strong> via Lingua Tutor
      </p>
    </td></tr>

  </table>
  </td></tr>
  </table>

</body>
</html>
"""
    return plain, html


# ── Gmail SMTP sender ─────────────────────────────────────────────────────────

def _send_via_gmail(
    to_email: str,
    to_name: str,
    subject: str,
    plain_body: str,
    html_body: str,
) -> bool:
    """
    Send an email using Gmail SMTP with TLS (port 587).

    Requires:
        GMAIL_SENDER       — your Gmail address
        GMAIL_APP_PASSWORD — 16-character App Password (not your real password)
                             https://myaccount.google.com/apppasswords

    Returns:
        True on success, False on any SMTP error.
    """
    if not settings.GMAIL_SENDER or not settings.GMAIL_APP_PASSWORD:
        logger.error("Gmail credentials not configured. Set GMAIL_SENDER and GMAIL_APP_PASSWORD.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{settings.TUTOR_NAME} <{settings.GMAIL_SENDER}>"
    msg["To"]      = f"{to_name} <{to_email}>"
    msg["Reply-To"] = settings.GMAIL_SENDER

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(settings.GMAIL_SENDER, settings.GMAIL_APP_PASSWORD)
            server.sendmail(settings.GMAIL_SENDER, to_email, msg.as_string())
        logger.info("Gmail: email sent to %s", to_email)
        return True
    except smtplib.SMTPException as exc:
        logger.error("Gmail SMTP error: %s", exc)
        return False


# ── Resend API sender ─────────────────────────────────────────────────────────

def _send_via_resend(
    to_email: str,
    to_name: str,
    subject: str,
    plain_body: str,
    html_body: str,
) -> bool:
    """
    Send an email via the Resend API.

    Requires:
        RESEND_API_KEY   — API key from resend.com
        RESEND_FROM_EMAIL — verified sender address (must match your Resend domain)

    Returns:
        True on success, False on any API error.
    """
    if not settings.RESEND_API_KEY:
        logger.error("RESEND_API_KEY is not set.")
        return False

    resend.api_key = settings.RESEND_API_KEY

    try:
        response = resend.Emails.send({
            "from":    f"{settings.TUTOR_NAME} <{settings.RESEND_FROM_EMAIL}>",
            "to":      [f"{to_name} <{to_email}>"],
            "subject": subject,
            "text":    plain_body,
            "html":    html_body,
            "reply_to": settings.TUTOR_EMAIL or settings.RESEND_FROM_EMAIL,
        })
        logger.info("Resend: email sent. id=%s", response.get("id"))
        return True
    except Exception as exc:
        logger.error("Resend API error: %s", exc)
        return False


# ── Public function ───────────────────────────────────────────────────────────

def send_booking_confirmation(
    student_email: str,
    student_name: str,
    language: str,
    meet_link: Optional[str],
    booking_start: Optional[datetime],
    graded_result: Optional[GradedResult] = None,
) -> bool:
    """
    Send a booking confirmation email to the student.

    Automatically chooses Gmail or Resend based on EMAIL_PROVIDER in .env.

    Args:
        student_email:  Recipient's email address.
        student_name:   Recipient's display name.
        language:       Target language (shown in subject & body).
        meet_link:      Google Meet URL (may be None if calendar call failed).
        booking_start:  Session start datetime (may be None).
        graded_result:  Optional — includes quiz results in the email.

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    subject = (
        f"✅ Your {language} lesson is confirmed"
        + (
            f" — Level {graded_result.cefr_level}"
            if graded_result
            else ""
        )
    )

    plain, html = _build_email_content(
        student_name=student_name,
        language=language,
        meet_link=meet_link,
        booking_start=booking_start,
        graded_result=graded_result,
        tutor_name=settings.TUTOR_NAME,
    )

    provider = settings.EMAIL_PROVIDER
    logger.info(
        "Sending booking confirmation via %s to %s", provider, student_email
    )

    if provider == "gmail":
        return _send_via_gmail(student_email, student_name, subject, plain, html)
    elif provider == "resend":
        return _send_via_resend(student_email, student_name, subject, plain, html)
    else:
        logger.error(
            "Unknown EMAIL_PROVIDER '%s'. Set it to 'gmail' or 'resend'.", provider
        )
        return False
    
    
# reset password email (no booking details, just a reset link)
def send_password_reset_email(student_email: str, student_name: str, reset_link: str) -> bool:
    subject = "Reset your Lingua password"

    plain = f"""Hello {student_name or 'there'},

We received a request to reset your password.

Reset your password:
{reset_link}

This link will expire soon and can only be used once.

If you did not request this, you can safely ignore this email.

{settings.TUTOR_NAME}
"""

    html = f"""
    <div style="font-family: Arial, sans-serif; line-height: 1.6; color: #111;">
      <h2>Reset your password</h2>
      <p>Hello {student_name or 'there'},</p>
      <p>We received a request to reset your password.</p>
      <p>
        <a href="{reset_link}" style="display:inline-block;padding:12px 18px;background:#C8922A;color:#111;text-decoration:none;border-radius:10px;font-weight:700;">
          Reset Password
        </a>
      </p>
      <p>If the button does not work, use this link:</p>
      <p><a href="{reset_link}">{reset_link}</a></p>
      <p>This link will expire soon and can only be used once.</p>
      <p>If you did not request this, you can safely ignore this email.</p>
      <p>{settings.TUTOR_NAME}</p>
    </div>
    """

    try:
        if settings.EMAIL_PROVIDER == "gmail":
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = settings.GMAIL_SENDER
            msg["To"] = student_email
            msg.attach(MIMEText(plain, "plain"))
            msg.attach(MIMEText(html, "html"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(settings.GMAIL_SENDER, settings.GMAIL_APP_PASSWORD)
                server.sendmail(settings.GMAIL_SENDER, student_email, msg.as_string())

            logger.info("Password reset email sent via Gmail to %s", student_email)
            return True

        if settings.EMAIL_PROVIDER == "resend":
            params = {
                "from": settings.RESEND_FROM_EMAIL,
                "to": [student_email],
                "subject": subject,
                "html": html,
                "text": plain,
            }
            resend.Emails.send(params)
            logger.info("Password reset email sent via Resend to %s", student_email)
            return True

        logger.error("Unknown EMAIL_PROVIDER: %s", settings.EMAIL_PROVIDER)
        return False

    except Exception as exc:
        logger.error("Failed to send password reset email to %s: %s", student_email, exc)
        return False
