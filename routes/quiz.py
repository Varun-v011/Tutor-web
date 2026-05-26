"""
routes/quiz.py
──────────────
Flask Blueprint — all quiz and lead-generation endpoints.

Endpoints:
  GET  /health          — Service liveness + store status
  POST /generate-quiz   — Return 5 questions from the in-memory store
  POST /submit-quiz     — Grade answers, book calendar, send email, save to DB

No LLM calls happen in this file. Questions come from the pre-populated
Supabase store (question_generator.py wrote them, ai_service.py loaded them).
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

quiz_bp = Blueprint("quiz", __name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

LANGUAGE_ALIASES = {
    "en": "English",
    "english": "English",

    "de": "German",
    "german": "German",

    "fr": "French",
    "french": "French",

    "jp": "Japanese",   # frontend shortcut
    "ja": "Japanese",   # standard language code
    "japanese": "Japanese",
}


def _json_error(message: str, status: int = 400) -> tuple:
    return jsonify({"error": message}), status


def _extract_json_body() -> tuple[dict | None, tuple | None]:
    data = request.get_json(silent=True)
    if data is None:
        return None, _json_error(
            "Request body must be valid JSON with Content-Type: application/json"
        )
    return data, None


def _normalize_language(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[raw]
    raise ValueError(
        f"Unsupported language '{value}'. Use one of: en, de, fr, jp, ja, "
        "or full names English, German, French, Japanese."
    )


# =============================================================================
#  GET /health
# =============================================================================

@quiz_bp.get("/health")
def health_check():
    """
    Liveness probe + store status.

    store.missing_labels will be non-empty if question_generator.py has not
    been run yet — include in monitoring alerts.
    """
    from config.settings import settings
    config_warnings = settings.validate()

    return jsonify({
        "status": "ok",
        "ai_provider": settings.AI_PROVIDER,
        "email_provider": settings.EMAIL_PROVIDER,
        "config_warnings": config_warnings,
        "store": ai_service.store_status(),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }), 200


# =============================================================================
#  POST /generate-quiz
# =============================================================================

@quiz_bp.post("/generate-quiz")
@limiter.limit("30 per hour")
def generate_quiz():
    """
    Return 5 pre-generated quiz questions for the requested language/difficulty.

    Questions are served from memory — no LLM call, no DB call.

    Request body:
        { "language": "French", "difficulty": "beginner" }
        { "language": "fr",     "difficulty": "beginner" }
        { "language": "jp",     "difficulty": "beginner" }

    Response body:
        { "questions": [...], "language": "French", "difficulty": "beginner" }

    Label mapping (beginner-first, matches question_generator.py):
        english_beginner      japanese_beginner     german_beginner     french_beginner
        english_intermediate  japanese_intermediate german_intermediate french_intermediate
        english_advanced      japanese_advanced     german_advanced     french_advanced
    """
    data, err = _extract_json_body()
    if err:
        return err

    try:
        req = QuizGenerateRequest(**data)
    except ValidationError as exc:
        logger.warning("Validation error on /generate-quiz: %s", exc)
        return jsonify({"error": "Validation failed", "details": exc.errors()}), 422

    try:
        normalized_language = _normalize_language(req.language)
    except ValueError as exc:
        logger.warning("Language normalization error on /generate-quiz: %s", exc)
        return _json_error(str(exc), 422)

    difficulty = (req.difficulty or "").strip().lower()

    logger.info(
        "generate-quiz: language='%s' difficulty='%s' (raw='%s')",
        normalized_language, difficulty, req.language,
    )

    try:
        questions = ai_service.get_questions(
            language=normalized_language,
            difficulty=difficulty,
        )

    except RuntimeError as exc:
        logger.error("Store not ready: %s", exc)
        return _json_error(
            "Question store not ready. Contact support if this persists.", 503
        )

    except KeyError as exc:
        logger.error("Label missing from store: %s", exc)
        return _json_error(
            f"Questions not yet available for {normalized_language}/{difficulty}. "
            "Please try again shortly.", 503
        )

    except Exception as exc:
        logger.exception("Unexpected error in /generate-quiz")
        return _json_error(f"Internal server error: {exc}", 500)

    response = QuizGenerateResponse(
        questions=questions,
        language=normalized_language,
        difficulty=difficulty,
    )
    return jsonify(response.model_dump()), 200


# =============================================================================
#  POST /submit-quiz
# =============================================================================

@quiz_bp.post("/submit-quiz")
@limiter.limit("10 per hour")
def submit_quiz():
    """
    Full lead-capture pipeline:
      1. Validate student info + answers.
      2. Grade via LLM → GradedResult.
      3. Book Google Calendar event → Meet link.
      4. Send confirmation email.
      5. Save student, result, booking to Supabase.
      6. Return full result to frontend.
    """
    data, err = _extract_json_body()
    if err:
        return err

    try:
        req = QuizSubmitRequest(**data)
    except ValidationError as exc:
        logger.warning("Validation error on /submit-quiz: %s", exc)
        return jsonify({"error": "Validation failed", "details": exc.errors()}), 422

    try:
        normalized_language = _normalize_language(req.language)
    except ValueError as exc:
        logger.warning("Language normalization error on /submit-quiz: %s", exc)
        return _json_error(str(exc), 422)

    logger.info(
        "submit-quiz: student='%s' email='%s' language='%s' difficulty='%s' answers=%d",
        req.student_name, req.student_email,
        normalized_language, req.difficulty, len(req.answers),
    )

    # ── Step 1: Grade ──────────────────────────────────────────────────────
    answers_for_grading = [
        {
            "question_id": a.question_id,
            "question_text": a.question_text,
            "student_answer": a.student_answer,
            "skill": getattr(a, "skill", "—"),
        }
        for a in req.answers
    ]

    try:
        graded: GradedResult = ai_service.grade_quiz(
            language=normalized_language,
            answers=answers_for_grading,
        )
    except EnvironmentError as exc:
        logger.error("AI config error during grading: %s", exc)
        return _json_error(str(exc), 500)
    except ValueError as exc:
        logger.error("AI grading error: %s", exc)
        return _json_error(f"AI returned an invalid grading response: {exc}", 502)
    except Exception as exc:
        logger.exception("Unexpected error during grading")
        return _json_error(f"Internal server error: {exc}", 500)

    warnings = []
    meet_link = None
    event_info = {}

    # ── Step 2: Calendar booking ───────────────────────────────────────────
    try:
        event_info = google_calendar_service.create_event(
            student_email=req.student_email,
            student_name=req.student_name,
            language=normalized_language,
            start_dt=req.booking_start,
            graded_result=graded,
        )
        meet_link = event_info.get("meet_link")
    except FileNotFoundError as exc:
        msg = f"Calendar booking skipped — credentials missing: {exc}"
        logger.warning(msg)
        warnings.append(msg)
    except Exception as exc:
        msg = f"Calendar booking failed: {exc}"
        logger.error(msg)
        warnings.append(msg)

    # ── Step 3: Email ──────────────────────────────────────────────────────
    email_sent = False
    try:
        email_sent = email_service.send_booking_confirmation(
            student_email=req.student_email,
            student_name=req.student_name,
            language=normalized_language,
            meet_link=meet_link,
            booking_start=req.booking_start,
            graded_result=graded,
        )
        if not email_sent:
            warnings.append("Email sending failed — check email service configuration.")
    except Exception as exc:
        msg = f"Email exception: {exc}"
        logger.error(msg)
        warnings.append(msg)

    # ── Step 4: Save to Supabase ───────────────────────────────────────────
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
        "submit-quiz complete: score=%d cefr=%s meet=%s email=%s",
        graded.overall_score, graded.cefr_level, bool(meet_link), email_sent,
    )

    response_data = QuizSubmitResponse(
        message="Quiz graded and session booked successfully.",
        graded_result=graded,
        meet_link=meet_link,
        booking_start=req.booking_start.isoformat() if req.booking_start else None,
        email_sent=email_sent,
    ).model_dump()

    response_data["language"] = normalized_language

    if warnings:
        response_data["warnings"] = warnings

    return jsonify(response_data), 200