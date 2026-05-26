"""
services/ai_service.py
──────────────────────
Runtime question service — reads from Supabase, never calls the LLM for questions.

Responsibility split
────────────────────
  question_generator.py  →  generates questions, writes to Supabase  (run offline/cron)
  ai_service.py          →  reads from Supabase into memory, serves to routes  (runtime)
  grade_quiz()           →  the ONLY LLM call at runtime (personalised, cannot pre-generate)

Startup
───────
  Call load_store() once in app.py.
  Reads all 12 rows from Supabase into _STORE keyed by label:

    _STORE = {
        "english_beginner":      [q1,q2,q3,q4,q5],
        "japanese_beginner":     [q1,q2,q3,q4,q5],
        "german_beginner":       [q1,q2,q3,q4,q5],
        "french_beginner":       [q1,q2,q3,q4,q5],
        "english_intermediate":  [q1,q2,q3,q4,q5],
        "japanese_intermediate": [q1,q2,q3,q4,q5],
        "german_intermediate":   [q1,q2,q3,q4,q5],
        "french_intermediate":   [q1,q2,q3,q4,q5],
        "english_advanced":      [q1,q2,q3,q4,q5],
        "japanese_advanced":     [q1,q2,q3,q4,q5],
        "german_advanced":       [q1,q2,q3,q4,q5],
        "french_advanced":       [q1,q2,q3,q4,q5],
    }

  Every user request hits this in-memory dict — zero DB latency, zero LLM calls.

Public API
──────────
  load_store()                         → None         call once in app.py
  get_questions(language, difficulty)  → list[dict]   5 questions from _STORE
  store_status()                       → dict         add to /health
  grade_quiz(language, answers)        → GradedResult

Supabase table (created by question_generator.py)
──────────────────────────────────────────────────
  quiz_questions
    label        TEXT UNIQUE   e.g. 'french_beginner'
    language     TEXT
    difficulty   TEXT
    flow         TEXT          'cognate' | 'standard'
    questions    JSONB         array of 5 question dicts
    generated_at TIMESTAMPTZ
"""

import json
import logging
import re
import threading
from typing import Optional

import google.generativeai as genai
from groq import Groq
from supabase import Client, create_client

from config.settings import settings
from models.lead import GradedResult

logger = logging.getLogger(__name__)

_TABLE = "quiz_questions"

# All 12 labels that question_generator.py writes — beginner first
_LANGUAGES:    list[str] = ["english", "japanese", "german", "french"]
_DIFFICULTIES: list[str] = ["beginner", "intermediate", "advanced"]

_ALL_LABELS: list[str] = (
    [f"{lang}_beginner"      for lang in _LANGUAGES]
    + [f"{lang}_intermediate" for lang in _LANGUAGES]
    + [f"{lang}_advanced"     for lang in _LANGUAGES]
)

# ─────────────────────────────────────────────────────────────────────────────
#  In-memory store
#    _STORE["french_beginner"]     = [q1, q2, q3, q4, q5]
#    _STORE["german_intermediate"] = [q1, q2, q3, q4, q5]
#    ... (12 keys total)
# ─────────────────────────────────────────────────────────────────────────────
_STORE:      dict[str, list[dict]] = {}
_STORE_LOCK: threading.RLock       = threading.RLock()


# =============================================================================
#  SECTION 1 — SUPABASE CLIENT
# =============================================================================

_supabase: Optional[Client] = None


def _db() -> Client:
    global _supabase
    if _supabase is None:
        if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_KEY:
            raise EnvironmentError(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env"
            )
        _supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    return _supabase


# =============================================================================
#  SECTION 2 — STORE LOADER
# =============================================================================

def load_store() -> None:
    """
    Fetch all quiz_questions rows from Supabase and load them into _STORE.

    Call once in app.py before handling any requests:

        from services.ai_service import load_store
        load_store()

    Logs a WARNING for any missing labels — run question_generator.py to fill them.
    If a label is already in _STORE (e.g. from a previous reload), it is replaced.
    """
    logger.info("Loading question store from Supabase …")

    rows = (
        _db()
        .table(_TABLE)
        .select("label, language, difficulty, flow, questions, generated_at")
        .execute()
    )
    fetched = rows.data or []

    new_store: dict[str, list[dict]] = {}

    for row in fetched:
        label     = (row.get("label") or "").strip()
        questions = row.get("questions")

        if not label:
            logger.warning("Skipping row with empty label")
            continue

        if not isinstance(questions, list) or len(questions) < 5:
            logger.warning(
                "[%s] questions field is invalid (got %s) — skipping",
                label, type(questions).__name__,
            )
            continue

        new_store[label] = questions[:5]
        logger.info(
            "  ✓  [%-28s]  flow=%-8s  generated=%s",
            label,
            row.get("flow", "?"),
            (row.get("generated_at") or "?")[:19],
        )

    missing = [l for l in _ALL_LABELS if l not in new_store]
    if missing:
        logger.warning(
            "%d / 12 labels missing from Supabase: %s\n"
            "  → Run:  python -m services.question_generator",
            len(missing), missing,
        )

    with _STORE_LOCK:
        _STORE.clear()
        _STORE.update(new_store)

    logger.info(
        "Store loaded — %d / 12 labels in memory  (%d questions total)",
        len(new_store), len(new_store) * 5,
    )


# =============================================================================
#  SECTION 3 — LLM CLIENT  (grade_quiz only)
# =============================================================================

_gemini_model: Optional[genai.GenerativeModel] = None
_groq_client:  Optional[Groq]                  = None


def _get_gemini() -> genai.GenerativeModel:
    global _gemini_model
    if _gemini_model is None:
        if not settings.GEMINI_API_KEY:
            raise EnvironmentError("GEMINI_API_KEY is not set in .env")
        genai.configure(api_key=settings.GEMINI_API_KEY)
        _gemini_model = genai.GenerativeModel(
            getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash"),
        )
    return _gemini_model


def _get_groq() -> Groq:
    global _groq_client
    if _groq_client is None:
        if not settings.GROQ_API_KEY:
            raise EnvironmentError("GROQ_API_KEY is not set in .env")
        _groq_client = Groq(api_key=settings.GROQ_API_KEY)
    return _groq_client


def _call_llm_once(prompt: str, temperature: float = 0.2, max_tokens: int = 1000) -> str:
    """Single real-time LLM call — used only by grade_quiz."""
    provider = getattr(settings, "AI_PROVIDER", "gemini")

    if provider == "gemini":
        response = _get_gemini().generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        return response.text

    if provider == "groq":
        completion = _get_groq().chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {"role": "system", "content": "You are a language examiner. Always respond with valid JSON only."},
                {"role": "user",   "content": prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        return completion.choices[0].message.content

    raise ValueError(f"Unknown AI_PROVIDER '{provider}'. Set to 'gemini' or 'groq' in .env.")


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


# =============================================================================
#  SECTION 4 — GRADING PROMPT
# =============================================================================

_GRADE_PROMPT = """
You are an expert {language} language examiner.
Grade the following student quiz answers rigorously and objectively.

── Student Answers ──────────────────────────────────────────────────────────
{answers_block}
─────────────────────────────────────────────────────────────────────────────

Scoring: MCQ correct=20 pts, wrong=0.  Open: 0-20 pts.  Total max=100.
CEFR: 0-20 -> A1   21-40 -> A2   41-60 -> B1   61-75 -> B2   76-90 -> C1   91-100 -> C2

Return ONLY a valid JSON object, no markdown, no backticks:
{{
  "overall_score":  <int 0-100>,
  "cefr_level":     "<A1|A2|B1|B2|C1|C2>",
  "strengths":      ["...", "..."],
  "weaknesses":     ["...", "..."],
  "recommendation": "<2-3 sentence personalised study suggestion>",
  "per_question":   [
    {{"question_id": <int>, "score": <int 0-20>, "feedback": "<one sentence>"}}
  ]
}}
"""

_ANSWER_LINE = "Q{question_id} [{skill}]: {question_text}\nStudent answer: {student_answer}\n"


# =============================================================================
#  SECTION 5 — PUBLIC API
# =============================================================================

def get_questions(language: str, difficulty: str) -> list[dict]:
    """
    Return the 5 pre-generated questions for this language + difficulty.

    Served from _STORE (in-memory) — zero DB calls, zero LLM calls.

    Label lookup: "{language}_{difficulty}" (both lower-cased).
    Matches exactly what question_generator.py writes to Supabase.

    Args:
        language:   "English"|"Japanese"|"German"|"French"  (case-insensitive)
        difficulty: "beginner"|"intermediate"|"advanced"    (case-insensitive)

    Returns:
        list[dict] — exactly 5 question dicts.

    Raises:
        RuntimeError: load_store() was never called or store is empty.
        KeyError:     Label not in store — run question_generator.py.

    Question dict fields (always present)
    ──────────────────────────────────────
      id              int
      flow            "cognate" | "standard"
      skill           str
      type            "mcq" | "open"
      text            str
      options         dict {"A":…,"B":…,"C":…,"D":…} | None
      correct_answer  "A"|"B"|"C"|"D" | None
      hints           list[str]  — reveal one-by-one on hint button click

    Cognate (beginner) questions also include
    ──────────────────────────────────────────
      foreign_word    str
      transliteration str
      tooltip         str   — shown on word hover
      success_message str   — shown on correct answer
      bridge_fact     str   — shown after answering
    """
    label = f"{language.strip().lower()}_{difficulty.strip().lower()}"

    with _STORE_LOCK:
        if not _STORE:
            raise RuntimeError(
                "Question store is empty — call load_store() in app.py "
                "and run: python -m services.question_generator"
            )
        questions = _STORE.get(label)

    if questions is None:
        raise KeyError(
            f"No questions for label '{label}'. "
            f"Run: python -m services.question_generator "
            f"--lang {language.title()} --diff {difficulty}"
        )

    logger.debug("Served [%s] — 5 questions from memory", label)
    return questions


def store_status() -> dict:
    """
    Return per-label store health.  Wire into your /health endpoint:

        from services.ai_service import store_status
        return jsonify({"status": "ok", "store": store_status()})

    Response shape:
        {
          "loaded_labels":   12,
          "missing_labels":  [],
          "total_questions": 60,
          "labels": {
            "english_beginner":    {"flow": "cognate",  "questions": 5},
            "french_intermediate": {"flow": "standard", "questions": 5},
            ...
          }
        }
    """
    with _STORE_LOCK:
        snapshot = dict(_STORE)

    label_info = {
        label: {
            "flow":      "cognate" if "beginner" in label else "standard",
            "questions": len(questions),
        }
        for label, questions in snapshot.items()
    }

    missing = [l for l in _ALL_LABELS if l not in snapshot]

    return {
        "loaded_labels":   len(snapshot),
        "missing_labels":  missing,
        "total_questions": len(snapshot) * 5,
        "labels":          label_info,
    }


def grade_quiz(language: str, answers: list[dict]) -> GradedResult:
    """
    Grade a completed quiz via the LLM.

    This is the ONLY function in ai_service.py that calls the LLM at
    request time.  Grading is always personalised and cannot be pre-generated.

    Args:
        language: Target language for contextual grading.
        answers:  list of dicts with:
                    question_id    int
                    question_text  str
                    student_answer str
                    skill          str (optional)

    Returns:
        GradedResult Pydantic model.

    Raises:
        ValueError:      Malformed LLM response or missing keys.
        EnvironmentError: Missing API key.
    """
    answers_block = "\n".join(
        _ANSWER_LINE.format(
            question_id   = a.get("question_id"),
            skill         = a.get("skill", "—"),
            question_text = str(a.get("question_text", "")).replace("\n", " ")[:300],
            student_answer= a.get("student_answer", ""),
        )
        for a in answers
    )

    prompt = _GRADE_PROMPT.format(language=language, answers_block=answers_block)
    logger.info("Grading quiz: language=%s answers=%d", language, len(answers))

    try:
        raw         = _call_llm_once(prompt, temperature=0.2, max_tokens=1000)
        result_dict = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"AI returned malformed JSON during grading: {exc}") from exc

    required = {
        "overall_score","cefr_level","strengths",
        "weaknesses","recommendation","per_question",
    }
    missing = required - set(result_dict.keys())
    if missing:
        raise ValueError(f"AI grading response missing keys: {missing}")

    graded = GradedResult(**result_dict)
    logger.info("Grading complete: score=%d cefr=%s", graded.overall_score, graded.cefr_level)
    return graded