"""
services/supabase_service.py
────────────────────────────
Handles all Supabase DB writes for leads, quiz results, and bookings.
"""

import os
import logging
from supabase import create_client
from models.lead import Lead, GradedResult

logger = logging.getLogger(__name__)
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def save_student(req, graded: GradedResult) -> str | None:
    """
    Upsert student into students table.
    Uses email as conflict key — safe to call multiple times.
    Returns student UUID or None on failure.
    """
    try:
        result = supabase.table("students").upsert({
            "name":           req.student_name,
            "email":          req.student_email,
            "phone":          req.student_phone,
            "language":       req.language,
            "difficulty":     "intermediate",
            "learning_goal":  req.learning_goal,   # ← cold call reason
            "overall_score":  graded.overall_score,
            "cefr_level":     graded.cefr_level,
            "strengths":      graded.strengths,
            "weaknesses":     graded.weaknesses,
            "recommendation": graded.recommendation,
            "status":         "signed_up",
        }, on_conflict="email").execute()

        student_id = result.data[0]["id"]
        logger.info("Student saved: id=%s email=%s", student_id, req.student_email)
        return student_id

    except Exception as exc:
        logger.error("Failed to save student: %s", exc)
        return None


def save_quiz_result(student_id: str, req, graded: GradedResult) -> None:
    """Save full quiz result with per_question JSONB."""
    try:
        supabase.table("quiz_results").insert({
            "student_id":     student_id,
            "language":       req.language,
            "difficulty":     "intermediate",
            "learning_goal":  req.learning_goal,
            "overall_score":  graded.overall_score,
            "cefr_level":     graded.cefr_level,
            "strengths":      graded.strengths,
            "weaknesses":     graded.weaknesses,
            "recommendation": graded.recommendation,
            "per_question":   graded.per_question,
            "raw_answers":    [a.dict() for a in req.answers],
        }).execute()
        logger.info("Quiz result saved for student_id=%s", student_id)

    except Exception as exc:
        logger.error("Failed to save quiz result: %s", exc)


def save_booking(student_id: str, req, event_info: dict) -> None:
    """Save demo booking with Google Calendar + Meet details."""
    try:
        supabase.table("demo_bookings").insert({
            "student_id":        student_id,
            "booking_start":     req.booking_start.isoformat(),
            "calendar_event_id": event_info.get("event_id"),
            "meet_link":         event_info.get("meet_link"),
            "html_link":         event_info.get("html_link"),
            "status":            "scheduled",
        }).execute()

        # Update student status to demo_booked
        supabase.table("students").update(
            {"status": "demo_booked"}
        ).eq("id", student_id).execute()

        logger.info("Booking saved for student_id=%s", student_id)

    except Exception as exc:
        logger.error("Failed to save booking: %s", exc)