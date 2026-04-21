"""
routes/quiz.py
──────────────
Flask Blueprint — all quiz and lead-generation endpoints.

Endpoints:
  GET  /health            — Service health check
  POST /generate-quiz     — Generate 5 language questions via AI
  POST /submit-quiz       — Grade answers, book calendar, send email, save to DB
"""

import logging
from datetime import datetime

from flask import Blueprint, request, jsonify
from pydantic import ValidationError

from models.lead import (
    QuizGenerateRequest,
    QuizSubmitRequest,
    QuizGenerateResponse,
    QuizSubmitResponse,
    GradedResult,
)
from services import ai_service, google_calendar_service, email_service, supabase_service
from extensions import limiter

logger = logging.getLogger(__name__)

# All routes in this blueprint are prefixed with nothing;
# register with app.register_blueprint(quiz_bp) in app.py.
quiz_bp = Blueprint("quiz", __name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _json_error(message: str, status: int = 400) -> tuple:
    """Return a consistent JSON error response."""
    return jsonify({"error": message}), status


def _extract_json_body() -> tuple[dict | None, tuple | None]:
    """
    Parse request body as JSON.
    Returns (data_dict, None) on success or (None, error_response) on failure.
    """
    data = request.get_json(silent=True)
    if data is None:
        return None, _json_error(
            "Request body must be valid JSON with Content-Type: application/json"
        )
    return data, None


# ─────────────────────────────────────────────────────────────────────────────
#  GET /health
# ─────────────────────────────────────────────────────────────────────────────

@quiz_bp.get("/health")
def health_check():
    """
    Simple liveness probe.
    Returns 200 with service status — useful for load balancers and monitoring.
    """
    from config.settings import settings
    warnings = settings.validate()

    return jsonify({
        "status": "ok",
        "ai_provider": settings.AI_PROVIDER,
        "email_provider": settings.EMAIL_PROVIDER,
        "config_warnings": warnings,
    }), 200


# ─────────────────────────────────────────────────────────────────────────────
#  POST /generate-quiz
# ─────────────────────────────────────────────────────────────────────────────

@quiz_bp.post("/generate-quiz")
@limiter.limit("20 per hour") 
def generate_quiz():
    """
    Generate 5 language-diagnostic quiz questions.
    """
    data, err = _extract_json_body()
    if err:
        return err

    try:
        req = QuizGenerateRequest(**data)
    except ValidationError as exc:
        logger.warning("Validation error on /generate-quiz: %s", exc)
        return jsonify({
            "error": "Validation failed",
            "details": exc.errors(),
        }), 422

    logger.info(
        "Generating quiz: language='%s' difficulty='%s'",
        req.language, req.difficulty,
    )

    try:
        questions = ai_service.generate_questions(
            language=req.language,
            difficulty=req.difficulty,
        )
    except EnvironmentError as exc:
        logger.error("AI config error: %s", exc)
        return _json_error(str(exc), 500)
    except ValueError as exc:
        logger.error("AI service ValueError: %s", exc)
        return _json_error(f"AI returned an invalid response: {exc}", 502)
    except Exception as exc:
        logger.exception("Unexpected error in /generate-quiz")
        return _json_error(f"Internal server error: {exc}", 500)

    response = QuizGenerateResponse(
        questions=questions,
        language=req.language,
        difficulty=req.difficulty,
    )
    return jsonify(response.model_dump()), 200


# ─────────────────────────────────────────────────────────────────────────────
#  POST /submit-quiz
# ─────────────────────────────────────────────────────────────────────────────

@quiz_bp.post("/submit-quiz")
@limiter.limit("10 per hour") 
def submit_quiz():
    """
    Full lead-capture pipeline:
      1. Validate student contact info + quiz answers.
      2. Grade answers via AI → GradedResult.
      3. Create Google Calendar event → Google Meet link.
      4. Send booking confirmation email with Meet link + results.
      5. Save student, quiz result, and booking to Supabase.
      6. Return the full result to the frontend.
    """
    data, err = _extract_json_body()
    if err:
        return err

    try:
        req = QuizSubmitRequest(**data)
    except ValidationError as exc:
        logger.warning("Validation error on /submit-quiz: %s", exc)
        return jsonify({
            "error": "Validation failed",
            "details": exc.errors(),
        }), 422

    logger.info(
        "Quiz submission: student='%s' email='%s' language='%s' answers=%d",
        req.student_name,
        req.student_email,
        req.language,
        len(req.answers),
    )

    # Step 1: Grade
    answers_for_grading = [
        {
            "question_id": a.question_id,
            "question_text": a.question_text,
            "student_answer": a.student_answer,
        }
        for a in req.answers
    ]

    try:
        graded: GradedResult = ai_service.grade_quiz(
            language=req.language,
            answers=answers_for_grading,
        )
    except EnvironmentError as exc:
        logger.error("AI config error during grading: %s", exc)
        return _json_error(str(exc), 500)
    except ValueError as exc:
        logger.error("AI grading ValueError: %s", exc)
        return _json_error(f"AI returned an invalid grading response: {exc}", 502)
    except Exception as exc:
        logger.exception("Unexpected error during quiz grading")
        return _json_error(f"Grading failed: {exc}", 500)

    # Step 2: Calendar booking
    warnings = []
    meet_link = None
    event_info = {}

    try:
        event_info = google_calendar_service.create_event(
            student_email=req.student_email,
            student_name=req.student_name,
            language=req.language,
            start_dt=req.booking_start,
            graded_result=graded,
        )
        meet_link = event_info.get("meet_link")
    except FileNotFoundError as exc:
        msg = f"Calendar booking skipped — credentials file missing: {exc}"
        logger.warning(msg)
        warnings.append(msg)
    except Exception as exc:
        msg = f"Calendar booking failed: {exc}"
        logger.error(msg)
        warnings.append(msg)

    # Step 3: Email
    email_sent = False
    try:
        email_sent = email_service.send_booking_confirmation(
            student_email=req.student_email,
            student_name=req.student_name,
            language=req.language,
            meet_link=meet_link,
            booking_start=req.booking_start,
            graded_result=graded,
        )
        if not email_sent:
            warnings.append("Email sending failed — check email service configuration.")
    except Exception as exc:
        msg = f"Email sending raised an exception: {exc}"
        logger.error(msg)
        warnings.append(msg)

    # Step 4: Save to Supabase
    try:
        student_id = supabase_service.save_student(req, graded)
        if student_id:
            supabase_service.save_quiz_result(student_id, req, graded)
            if event_info:
                supabase_service.save_booking(student_id, req, event_info)
    except Exception as exc:
        msg = f"Database save failed: {exc}"
        logger.error(msg)
        warnings.append(msg)

    logger.info(
        "Submit complete: score=%d cefr=%s meet=%s email=%s",
        graded.overall_score,
        graded.cefr_level,
        bool(meet_link),
        email_sent,
    )

    response_data = QuizSubmitResponse(
        message="Quiz graded and session booked successfully.",
        graded_result=graded,
        meet_link=meet_link,
        booking_start=req.booking_start.isoformat() if req.booking_start else None,
        email_sent=email_sent,
    ).model_dump()

    if warnings:
        response_data["warnings"] = warnings

    return jsonify(response_data), 200