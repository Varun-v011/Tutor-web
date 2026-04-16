"""
services/google_calendar_service.py
─────────────────────────────────────
Google Calendar integration with automatic Google Meet link generation.

Authentication flow (OAuth 2.0 — first run only):
  1. Put your OAuth credentials JSON at the path in GOOGLE_CREDENTIALS_FILE.
  2. Run `python services/google_calendar_service.py` once — a browser tab
     will open for you to authorise.
  3. A token is saved to GOOGLE_TOKEN_FILE and reused on all future calls.

How Google Meet is generated:
  We pass conferenceDataVersion=1 and a requestId in the event body.
  Google automatically creates a Meet conference and attaches the join link.

Public API:
  create_event(student_email, student_name, language, start_dt, graded_result)
    → dict  {"event_id": str, "meet_link": str, "html_link": str}
"""

import os
import logging
import uuid
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config.settings import settings
from models.lead import GradedResult

logger = logging.getLogger(__name__)

# The scopes required — do NOT reduce these or Meet won't work
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
]


# ── OAuth helper ─────────────────────────────────────────────────────────────

def _get_credentials() -> Credentials:
    """
    Load stored OAuth credentials from disk, refreshing if expired.
    If no token exists, launch the browser-based OAuth flow.

    Returns:
        Valid google.oauth2.credentials.Credentials object.

    Raises:
        FileNotFoundError: If GOOGLE_CREDENTIALS_FILE does not exist.
    """
    creds: Credentials | None = None

    # Try to load existing token
    if os.path.exists(settings.GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(
            settings.GOOGLE_TOKEN_FILE, SCOPES
        )

    # If no valid creds, refresh or run OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired Google OAuth token.")
            creds.refresh(Request())
        else:
            if not os.path.exists(settings.GOOGLE_CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"Google credentials not found at: {settings.GOOGLE_CREDENTIALS_FILE}\n"
                    "Download your OAuth 2.0 credentials JSON from Google Cloud Console\n"
                    "and save it to that path."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                settings.GOOGLE_CREDENTIALS_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0)
            logger.info("New OAuth token obtained via browser flow.")

        # Persist token so next call is instant
        with open(settings.GOOGLE_TOKEN_FILE, "w") as token_file:
            token_file.write(creds.to_json())
        logger.info("OAuth token saved to %s", settings.GOOGLE_TOKEN_FILE)

    return creds


# ── Event builder ─────────────────────────────────────────────────────────────

def _build_event_body(
    student_name: str,
    student_email: str,
    language: str,
    start_dt: datetime,
    duration_minutes: int,
    graded_result: GradedResult | None,
) -> dict:
    """
    Build the Google Calendar API event body dict.

    The 'conferenceData.createRequest' field instructs Google to generate
    a new Google Meet link. conferenceDataVersion=1 is required in the
    API call for this to work.
    """
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    # Build a rich description from the grading result
    if graded_result:
        description_lines = [
            f"🎓 Language Session: {language}",
            f"📊 Student Level:    {graded_result.cefr_level} "
            f"(Score: {graded_result.overall_score}/100)",
            "",
            "📝 Areas to focus on:",
            *[f"  • {w}" for w in graded_result.weaknesses],
            "",
            "✅ Strengths:",
            *[f"  • {s}" for s in graded_result.strengths],
            "",
            f"💡 Recommendation: {graded_result.recommendation}",
            "",
            "This session was booked via the Lingua Tutor lead-generation platform.",
        ]
        description = "\n".join(description_lines)
        summary = (
            f"{language} Lesson — {student_name} "
            f"({graded_result.cefr_level}, {graded_result.overall_score}/100)"
        )
    else:
        description = f"{language} language tutoring session with {student_name}."
        summary = f"{language} Lesson — {student_name}"

    return {
        "summary": summary,
        "description": description,
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": settings.CALENDAR_TIMEZONE,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": settings.CALENDAR_TIMEZONE,
        },
        "attendees": [
            {"email": student_email, "displayName": student_name},
            {
                "email": settings.TUTOR_EMAIL,
                "displayName": settings.TUTOR_NAME,
                "organizer": True,
            },
        ],
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email",  "minutes": 60},   # 1 hour before
                {"method": "popup",  "minutes": 15},   # 15 min before
            ],
        },
        # ── This block generates the Google Meet link ──────────────────────
        "conferenceData": {
            "createRequest": {
                # Unique ID — if the same ID is used twice, Meet reuses the link
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        # Guest list visibility
        "guestsCanModify": False,
        "guestsCanSeeOtherGuests": True,
    }


# ── Public function ───────────────────────────────────────────────────────────

def create_event(
    student_email: str,
    student_name: str,
    language: str,
    start_dt: datetime,
    graded_result: GradedResult | None = None,
) -> dict:
    """
    Create a Google Calendar event with an auto-generated Google Meet link.

    Args:
        student_email:  Email address to invite as attendee.
        student_name:   Display name for the calendar invite.
        language:       Target language (shown in event title and description).
        start_dt:       Event start time (timezone-aware datetime recommended).
        graded_result:  Optional — enriches the event description with quiz data.

    Returns:
        dict with keys:
            event_id  (str)  — Google Calendar event ID
            meet_link (str)  — Google Meet join URL
            html_link (str)  — Direct link to the event in Google Calendar

    Raises:
        HttpError:        If the Calendar API call fails.
        FileNotFoundError: If credentials file is missing.
    """
    # Ensure start_dt is timezone-aware (use UTC if naive)
    if start_dt.tzinfo is None:
        logger.warning(
            "start_dt has no timezone — assuming UTC. "
            "Pass a tz-aware datetime for accurate calendar slots."
        )
        start_dt = start_dt.replace(tzinfo=timezone.utc)

    duration = settings.EVENT_DURATION_MINUTES

    logger.info(
        "Creating calendar event: %s @ %s (%d min)",
        student_name,
        start_dt.isoformat(),
        duration,
    )

    creds   = _get_credentials()
    service = build("calendar", "v3", credentials=creds)

    event_body = _build_event_body(
        student_name, student_email, language,
        start_dt, duration, graded_result,
    )

    try:
        # conferenceDataVersion=1 is REQUIRED for Meet link generation
        created_event = (
            service.events()
            .insert(
                calendarId=settings.GOOGLE_CALENDAR_ID,
                body=event_body,
                conferenceDataVersion=1,   # ← enables Google Meet
                sendUpdates="all",         # emails invite to all attendees
            )
            .execute()
        )
    except HttpError as exc:
        logger.error("Google Calendar API error: %s", exc)
        raise

    # Extract the Meet link from the response
    conference_data = created_event.get("conferenceData", {})
    entry_points    = conference_data.get("entryPoints", [])
    meet_link       = next(
        (ep["uri"] for ep in entry_points if ep.get("entryPointType") == "video"),
        None,
    )

    if not meet_link:
        logger.warning(
            "No Meet link found in event response. "
            "Check that conferenceDataVersion=1 was accepted."
        )

    result = {
        "event_id":  created_event["id"],
        "meet_link": meet_link,
        "html_link": created_event.get("htmlLink", ""),
    }

    logger.info(
        "Event created: id=%s meet=%s",
        result["event_id"],
        result["meet_link"],
    )
    return result


# ── CLI helper — run this once to authorise your Google account ───────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Authorising Google Calendar access…")
    creds = _get_credentials()
    print(f"✓ Authorisation successful. Token saved to: {settings.GOOGLE_TOKEN_FILE}")
