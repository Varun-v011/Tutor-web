"""
models/lead.py
──────────────
Pydantic data models for leads, quiz payloads, and API responses.

These models act as the contract between the frontend, Flask routes,
and service layer — validated automatically on every request.
"""

from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────────
#  Inbound request models (frontend → Flask)
# ─────────────────────────────────────────────────────────────────────────────

class QuizGenerateRequest(BaseModel):
    language: str = Field(..., min_length=2, max_length=60)
    difficulty: str = Field(
        default="intermediate",
        pattern=r"^(beginner|intermediate|advanced)$",
    )
    student_name:  Optional[str] = Field(default=None, max_length=120)
    learning_goal: Optional[str] = Field(       # ← add this
        default=None,
        max_length=200,
        description="Why the student is learning, e.g. 'Travel to Japan'",
    )

class QuizAnswer(BaseModel):
    """A single answer to one quiz question."""
    question_id: int    = Field(..., ge=1, le=5)
    question_text: str  = Field(..., min_length=0)
    student_answer: str = Field(..., min_length=1, max_length=2000)
    skill:          Optional[str] = None 

class QuizSubmitRequest(BaseModel):
    """
    POST /submit-quiz
    Full submission: student contact + their 5 answers + booking slot.
    """
    # ── Contact info ──────────────────────────────────────────────────────────
    student_name:  str      = Field(..., min_length=2, max_length=120)
    student_email: EmailStr
    student_phone: Optional[str] = Field(None, max_length=30)

    # ── Quiz answers ──────────────────────────────────────────────────────────
    language: str = Field(..., min_length=2, max_length=60)
    learning_goal: Optional[str] = Field(default=None, max_length=200)
    answers: list[QuizAnswer] = Field(
        ..., min_length=1, max_length=5,
        description="List of answers, one per generated question",
    )

    # ── Booking slot chosen by the student ───────────────────────────────────
    # ISO 8601 format: "2025-08-15T14:00:00"
    booking_start: datetime = Field(
        ...,
        description="Session start time in ISO 8601 format",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Internal / service-layer models
# ─────────────────────────────────────────────────────────────────────────────

class GradedResult(BaseModel):
    """
    Structured output from ai_service.grade_quiz().
    Returned to the frontend AND stored alongside the lead record.
    """
    overall_score:   int   = Field(..., ge=0, le=100, description="0–100 composite score")
    cefr_level:      str   = Field(..., description="A1 | A2 | B1 | B2 | C1 | C2")
    strengths:       list[str]
    weaknesses:      list[str]
    recommendation:  str   = Field(..., description="AI-written 2–3 sentence study suggestion")
    per_question:    list[dict] = Field(
        ...,
        description="[{question_id, score, feedback}, …] — one entry per answer",
    )


class Lead(BaseModel):
    """
    A fully-resolved lead: contact info + quiz result + booking details.
    Extend this to persist to a database (SQLAlchemy, MongoDB, etc.)
    """
    # Identity
    name:  str
    email: str
    phone: Optional[str] = None

    # Language context
    language:   str
    difficulty: str = "intermediate"

    # AI evaluation
    graded_result: Optional[GradedResult] = None

    # Calendar / booking
    calendar_event_id: Optional[str]  = None
    meet_link:         Optional[str]  = None
    booking_start:     Optional[datetime] = None

    # Audit
    created_at: datetime = Field(default_factory=datetime.utcnow)

    learning_goal: Optional[str] = None
# ─────────────────────────────────────────────────────────────────────────────
#  API response shapes (Flask → frontend)
# ─────────────────────────────────────────────────────────────────────────────

class QuizGenerateResponse(BaseModel):
    """Returned by POST /generate-quiz."""
    questions: list[dict] = Field(
        ...,
        description="[{id, text, type, options?}, …] — 5 questions",
    )
    language:   str
    difficulty: str


class QuizSubmitResponse(BaseModel):
    """Returned by POST /submit-quiz on full success."""
    message:       str
    graded_result: GradedResult
    meet_link:     Optional[str]  = None
    booking_start: Optional[str]  = None
    email_sent:    bool           = False
