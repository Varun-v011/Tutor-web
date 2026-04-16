"""
services/ai_service.py
───────────────────────
All AI interactions live in this single file.

Supported providers (set AI_PROVIDER in .env):
  • "gemini"  — Google Gemini 1.5 Flash (default)
  • "groq"    — Groq LPU with Llama-3 / Mixtral

To switch providers, only change AI_PROVIDER in your .env file.
To add a new provider, implement the same interface (_call_gemini / _call_groq)
and wire it into _call_llm().

Public API:
  generate_questions(language, difficulty, student_name) → list[dict]
  grade_quiz(language, answers)                          → GradedResult
"""

import json
import re
import logging
from typing import Optional


import google.generativeai as genai
from groq import Groq

from config.settings import settings
from models.lead import GradedResult

logger = logging.getLogger(__name__)

# ── Configure SDK clients once at import time ─────────────────────────────────
_gemini_model: Optional[genai.GenerativeModel] = None
_groq_client:  Optional[Groq]                  = None


def _get_gemini() -> genai.GenerativeModel:
    """Lazy-initialise the Gemini client (avoids errors if key isn't set)."""
    global _gemini_model

    if _gemini_model is None:
        if not settings.GEMINI_API_KEY:
            raise EnvironmentError(
                "GEMINI_API_KEY is not set. Add it to your .env file."
            )
        genai.configure(api_key=settings.GEMINI_API_KEY)
        _gemini_model = genai.GenerativeModel(
            settings.GEMINI_MODEL,
            generation_config=genai.types.GenerationConfig(
                temperature=0.4,       # balanced creativity
                max_output_tokens=1500,
            ),
        )
    return _gemini_model


def _get_groq() -> Groq:
    """Lazy-initialise the Groq client."""
    global _groq_client
    if _groq_client is None:
        if not settings.GROQ_API_KEY:
            raise EnvironmentError(
                "GROQ_API_KEY is not set. Add it to your .env file."
            )
        _groq_client = Groq(api_key=settings.GROQ_API_KEY)
    return _groq_client


# ── Low-level LLM dispatcher ──────────────────────────────────────────────────

def _call_llm(prompt: str, temperature: float = 0.4, max_tokens: int = 1500) -> str:
    """
    Send a prompt to whichever provider is configured and return raw text.

    Args:
        prompt:      The full prompt string.
        temperature: Sampling temperature (0 = deterministic, 1 = creative).
        max_tokens:  Maximum tokens in the completion.

    Returns:
        Raw string response from the model.

    Raises:
        EnvironmentError: If the required API key is missing.
        ValueError:       If AI_PROVIDER is set to an unknown value.
        RuntimeError:     If the LLM call itself fails.
    """
    provider = settings.AI_PROVIDER
    logger.info("Using AI provider: %s | model: %s", 
        provider, 
        settings.GEMINI_MODEL if provider == "gemini" else settings.GROQ_MODEL
    )
    if provider == "gemini":
        model = _get_gemini()
        # Override temperature per-call
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        return response.text

    elif provider == "groq":
        client = _get_groq()
        chat_completion = client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {"role": "system", "content": "You are a language tutor. Always respond with valid JSON only."},
                {"role": "user", "content": prompt}
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},  # ← forces valid JSON
    )
        return chat_completion.choices[0].message.content
    else:
        raise ValueError(
            f"Unknown AI_PROVIDER '{provider}'. "
            "Set AI_PROVIDER to 'gemini' or 'groq' in your .env file."
        )


# ── JSON sanitiser ────────────────────────────────────────────────────────────

def _extract_json(raw: str) -> str:
    """
    Strip markdown code fences (```json … ```) if the model wrapped its response.
    Returns the cleaned string ready for json.loads().
    """
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC FUNCTION 1 — generate_questions
# ─────────────────────────────────────────────────────────────────────────────

# Prompt template for question generation.
# Keeping it here makes prompt engineering easy without touching logic code.
_GENERATE_PROMPT = """
You are an expert {language} language tutor. Generate EXACTLY 5 quiz questions.

CRITICAL RULES:
- Every MCQ option (A, B, C, D) MUST contain actual text — never empty strings
- Question text MUST be complete and self-contained
- For Reading Comprehension: include the full passage THEN ask the question
- For Vocabulary MCQ: specify WHICH word you are testing in the question text
- Return ONLY a raw JSON array, no markdown, no backticks, no explanation

Language: {language}
Difficulty: {difficulty}


Generate 5 questions in this exact structure:
[
  {{"id": 1, "skill": "Grammar", "type": "mcq", "text": "...", "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}}}},
  {{"id": 2, "skill": "Vocabulary", "type": "mcq", "text": "...", "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}}}},
  {{"id": 3, "skill": "Reading Comprehension", "type": "open", "text": "...", "options": null}},
  {{"id": 4, "skill": "Situational Awareness", "type": "mcq", "text": "...", "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}}}},
  {{"id": 5, "skill": "Writing Production", "type": "open", "text": "...", "options": null}}
]

All MCQ options must be non-empty. Output ONLY the JSON array.
"""

def generate_questions(
    language: str,
    difficulty: str = "intermediate",

) -> list[dict]:
    """
    Generate 5 language-assessment questions via the configured LLM.

    Args:
        language:     Target language (e.g. "French", "Spanish").
        difficulty:   "beginner" | "intermediate" | "advanced".


    Returns:
        A list of 5 question dicts, each with keys:
            id, skill, type, text, options (None for open questions).

    Raises:
        ValueError:  If the LLM returns malformed JSON.
        RuntimeError: If the LLM call fails.
    """
    # Pre-compute the optional student name line BEFORE .format()
    # .format() cannot evaluate Python expressions — only named keys.
    # Passing the expression directly caused the 500 error seen in production.
   

    prompt = _GENERATE_PROMPT.format(
        language=language,
        difficulty=difficulty,

    )

    logger.info("Generating questions: language=%s difficulty=%s", language, difficulty)
    last_error = None
    for attempt in range(3):
        try:
            raw = _call_llm(prompt, temperature=0.4, max_tokens=1200)
            cleaned = _extract_json(raw)
            parsed = json.loads(cleaned)

            # ── Smart extraction: handle object wrapper ──
            # LLM sometimes returns {"questions": [...]} instead of [...]
            if isinstance(parsed, dict):
                for key in ("questions", "data", "items", "quiz"):
                    if isinstance(parsed.get(key), list):
                        parsed = parsed[key]
                        break
                else:
                    raise ValueError(f"LLM returned a dict with no recognisable list key: {list(parsed.keys())}")

            if not isinstance(parsed, list) or len(parsed) == 0:
                raise ValueError("LLM returned empty or non-list response")

            # ── Validate no empty MCQ options ──
            for q in parsed:
                if q.get("type") == "mcq" and isinstance(q.get("options"), dict):
                    empty = [k for k, v in q["options"].items() if not str(v).strip()]
                    if empty:
                        raise ValueError(f"Q{q['id']} has empty options: {empty}")
                if not str(q.get("text", "")).strip():
                    raise ValueError(f"Q{q['id']} has empty question text")

            logger.info("Generated %d questions (attempt %d)", len(parsed), attempt + 1)
            return parsed

        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            logger.warning("Attempt %d/3 failed: %s", attempt + 1, exc)

    raise ValueError(f"AI returned bad questions after 3 attempts: {last_error}")


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC FUNCTION 2 — grade_quiz
# ─────────────────────────────────────────────────────────────────────────────

_GRADE_PROMPT = """
You are an expert {language} language examiner.
Grade the following student quiz answers rigorously and objectively.

── Student Answers ──────────────────────────────────────────────────────────
{answers_block}
─────────────────────────────────────────────────────────────────────────────

Scoring rules:
  • MCQ:  correct = 20 points, incorrect = 0
  • Open: score 0–20 based on accuracy, grammar, and natural expression
  • Overall score = sum of all per-question scores (max 100)

CEFR band mapping:
  0–20  → A1 (Beginner)
  21–40 → A2 (Elementary)
  41–60 → B1 (Intermediate)
  61–75 → B2 (Upper-Intermediate)
  76–90 → C1 (Advanced)
  91–100→ C2 (Proficient)

Return ONLY a valid JSON object (no preamble, no markdown fences):
{{
  "overall_score":  <integer 0–100>,
  "cefr_level":     "<A1|A2|B1|B2|C1|C2>",
  "strengths":      ["<strength 1>", "<strength 2>"],
  "weaknesses":     ["<weakness 1>", "<weakness 2>"],
  "recommendation": "<2–3 sentence personalised study suggestion>",
  "per_question":   [
    {{
      "question_id":  <int>,
      "score":        <int 0–20>,
      "feedback":     "<one sentence of specific, constructive feedback>"
    }}
  ]
}}
"""

# Template for each answer block inside the grading prompt
_ANSWER_BLOCK_TEMPLATE = (
    "Q{question_id} [{skill}]: {question_text}\n"
    "Student answer: {student_answer}\n"
)


def grade_quiz(language: str, answers: list[dict]) -> GradedResult:
    """
    Grade a completed quiz using the configured LLM.

    Args:
        language: Target language (used for contextual grading).
        answers:  List of answer dicts, each containing:
                    question_id, question_text, student_answer
                  (plus optional 'skill' from the generated questions).

    Returns:
        A GradedResult Pydantic model with score, CEFR level, feedback, etc.

    Raises:
        ValueError:  If the LLM returns malformed JSON or missing keys.
        RuntimeError: If the LLM call fails.
    """
    # Build the answers block to embed in the prompt
    answers_block = "\n".join(
        _ANSWER_BLOCK_TEMPLATE.format(
            question_id=a.get("question_id"),
            skill=a.get("skill", "—"),
            question_text=a.get("question_text", ""),
            student_answer=a.get("student_answer", ""),
        )
        for a in answers
    )

    prompt = _GRADE_PROMPT.format(
        language=language,
        answers_block=answers_block,
    )

    logger.info("Grading quiz: language=%s num_answers=%d", language, len(answers))

    try:
        raw = _call_llm(prompt, temperature=0.2, max_tokens=1000)
        result_dict = json.loads(_extract_json(raw))
    except json.JSONDecodeError as exc:
        logger.error("LLM returned invalid JSON during grading: %s", exc)
        raise ValueError(
            f"AI returned malformed JSON during grading: {exc}"
        ) from exc

    # Validate required keys before building the model
    required_keys = {
        "overall_score", "cefr_level", "strengths",
        "weaknesses", "recommendation", "per_question",
    }
    missing = required_keys - set(result_dict.keys())
    if missing:
        raise ValueError(f"AI grading response missing keys: {missing}")

    graded = GradedResult(**result_dict)
    logger.info(
        "Grading complete: score=%d cefr=%s",
        graded.overall_score,
        graded.cefr_level,
    )
    return graded