"""
services/google_calendar_service.py
─────────────────────────────────────
Google Calendar integration with automatic Google Meet link generation.
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

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
]

# ── OAuth helper ─────────────────────────────────────────────────────────────

def _get_credentials() -> Credentials:
    creds: Credentials | None = None

    if os.path.exists(settings.GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(settings.GOOGLE_TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired Google OAuth token.")
            creds.refresh(Request())
        else:
            if not os.path.exists(settings.GOOGLE_CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"Google credentials not found at: {settings.GOOGLE_CREDENTIALS_FILE}\n"
                    "Download your OAuth 2.0 credentials JSON from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(settings.GOOGLE_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=8080)

        with open(settings.GOOGLE_TOKEN_FILE, "w") as token_file:
            token_file.write(creds.to_json())

    return creds

def _extract_meet_link(created_event: dict) -> str | None:
    """Pull the Google Meet video URI from a Calendar API event response."""
    conference_data = created_event.get("conferenceData", {})
    entry_points    = conference_data.get("entryPoints", [])
    return next((ep["uri"] for ep in entry_points if ep.get("entryPointType") == "video"), None)


# ── NEW: Admin Slot Creation (For Dashboard) ─────────────────────────────────

def create_slot_event(slot_start: datetime, slot_end: datetime, title: str = "Demo Session Slot") -> dict:
    """Creates a placeholder event for the admin slot and generates a Meet link."""
    if slot_start.tzinfo is None:
        slot_start = slot_start.replace(tzinfo=timezone.utc)
    if slot_end.tzinfo is None:
        slot_end = slot_end.replace(tzinfo=timezone.utc)

    event_body = {
        "summary": title,
        "description": "Available demo slot created by the admin.\nA student will be assigned when they book this slot.",
        "start": {"dateTime": slot_start.isoformat(), "timeZone": settings.CALENDAR_TIMEZONE},
        "end": {"dateTime": slot_end.isoformat(), "timeZone": settings.CALENDAR_TIMEZONE},
        "attendees": [{"email": settings.TUTOR_EMAIL, "displayName": settings.TUTOR_NAME, "organizer": True}],
        "reminders": {"useDefault": False, "overrides": [{"method": "email", "minutes": 60}, {"method": "popup", "minutes": 15}]},
        "conferenceData": {"createRequest": {"requestId": str(uuid.uuid4()), "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
        "guestsCanModify": False,
        "guestsCanSeeOtherGuests": True,
    }

    creds = _get_credentials()
    service = build("calendar", "v3", credentials=creds)

    try:
        created_event = service.events().insert(
            calendarId=settings.GOOGLE_CALENDAR_ID,
            body=event_body,
            conferenceDataVersion=1,
            sendUpdates="none",
        ).execute()
    except HttpError as exc:
        logger.error("Google Calendar API error creating slot: %s", exc)
        raise

    meet_link = _extract_meet_link(created_event)
    return {
        "event_id":  created_event["id"],
        "meet_link": meet_link,
        "html_link": created_event.get("htmlLink", ""),
    }

def delete_slot_event(event_id: str) -> bool:
    """Deletes a Google Calendar event when an admin removes a slot."""
    creds = _get_credentials()
    service = build("calendar", "v3", credentials=creds)
    try:
        service.events().delete(
            calendarId=settings.GOOGLE_CALENDAR_ID, eventId=event_id, sendUpdates="none"
        ).execute()
        return True
    except HttpError as exc:
        if exc.resp.status == 410:
            return False
        raise


# ── LEGACY: Direct-Book Event ────────────────────────────────────────────────

def _build_event_body(
    student_name: str, student_email: str, language: str,
    start_dt: datetime, duration_minutes: int, graded_result: GradedResult | None,
) -> dict:
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    if graded_result:
        description_lines = [
            f"🎓 Language Session: {language}",
            f"📊 Student Level:    {graded_result.cefr_level} (Score: {graded_result.overall_score}/100)",
            "", "📝 Areas to focus on:", *[f"  • {w}" for w in graded_result.weaknesses],
            "", "✅ Strengths:", *[f"  • {s}" for s in graded_result.strengths],
            "", f"💡 Recommendation: {graded_result.recommendation}",
        ]
        description = "\n".join(description_lines)
        summary = f"{language} Lesson — {student_name} ({graded_result.cefr_level}, {graded_result.overall_score}/100)"
    else:
        description = f"{language} language tutoring session with {student_name}."
        summary = f"{language} Lesson — {student_name}"

    return {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": settings.CALENDAR_TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": settings.CALENDAR_TIMEZONE},
        "attendees": [
            {"email": student_email, "displayName": student_name},
            {"email": settings.TUTOR_EMAIL, "displayName": settings.TUTOR_NAME, "organizer": True},
        ],
        "reminders": {"useDefault": False, "overrides": [{"method": "email", "minutes": 60}, {"method": "popup", "minutes": 15}]},
        "conferenceData": {"createRequest": {"requestId": str(uuid.uuid4()), "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
        "guestsCanModify": False,
        "guestsCanSeeOtherGuests": True,
    }

def create_event(
    student_email: str, student_name: str, language: str,
    start_dt: datetime, graded_result: GradedResult | None = None,
) -> dict:
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)

    duration = settings.EVENT_DURATION_MINUTES
    creds = _get_credentials()
    service = build("calendar", "v3", credentials=creds)

    event_body = _build_event_body(student_name, student_email, language, start_dt, duration, graded_result)

    try:
        created_event = service.events().insert(
            calendarId=settings.GOOGLE_CALENDAR_ID, body=event_body, conferenceDataVersion=1, sendUpdates="all",
        ).execute()
    except HttpError as exc:
        logger.error("Google Calendar API error: %s", exc)
        raise

    meet_link = _extract_meet_link(created_event)
    return {
        "event_id":  created_event["id"],
        "meet_link": meet_link,
        "html_link": created_event.get("htmlLink", ""),
    }

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Authorising Google Calendar access…")
    creds = _get_credentials()
    print(f"✓ Authorisation successful. Token saved to: {settings.GOOGLE_TOKEN_FILE}")