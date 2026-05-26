"""
services/question_generator.py
────────────────────────────────
Standalone CLI — generates all 60 quiz questions and stores them in Supabase.
Run this ONCE to populate the DB, then on a schedule to refresh (default: 30 days).
The Flask app never calls this — it only reads from the DB via ai_service.py.

Usage
─────
  python -m services.question_generator             # generate missing/stale only
  python -m services.question_generator --force     # regenerate all 12 labels
  python -m services.question_generator --status    # print table health and exit
  python -m services.question_generator --lang Japanese
  python -m services.question_generator --lang German --diff advanced

Pool design
───────────
  12 labels  = 4 languages × 3 difficulties
  5 questions per label
  60 total questions stored in Supabase

  Labels (the unique key in the DB):
    english_beginner      japanese_beginner     german_beginner     french_beginner
    english_intermediate  japanese_intermediate german_intermediate french_intermediate
    english_advanced      japanese_advanced     german_advanced     french_advanced

Gemini free-tier pacing
───────────────────────
  Hard limit : 15 RPM
  We enforce : one call every 4.5 s  →  13.3 RPM  (safe headroom)
  Full run   : 12 calls × 4.5 s = ~54 seconds

  Per-model intervals (model name read from GEMINI_MODEL in .env):
    gemini-2.5-flash      →  6.5 s  (≈ 9.2 RPM)
    gemini-2.5-flash-lite →  4.5 s  (≈ 13.3 RPM)
    gemini-2.5-pro        →  8.0 s  (≈ 7.5 RPM)
    anything else         →  6.5 s  (safe default)

Supabase table  (run this DDL once)
────────────────────────────────────
  CREATE TABLE quiz_questions (
      id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      label        TEXT UNIQUE NOT NULL,     -- e.g.  'french_beginner'
      language     TEXT NOT NULL,            -- e.g.  'french'
      difficulty   TEXT NOT NULL,            -- e.g.  'beginner'
      flow         TEXT NOT NULL,            -- 'cognate' | 'standard'
      questions    JSONB NOT NULL,           -- array of exactly 5 question dicts
      generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  );
  CREATE INDEX idx_qq_label ON quiz_questions (label);
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import threading
from datetime import datetime, timezone
from typing import Optional

from supabase import Client, create_client

from config.settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("question_generator")


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — LABELS & GENERATION ORDER
# ═══════════════════════════════════════════════════════════════════════════════

LANGUAGES:    list[str] = ["English", "Japanese", "German", "French"]
DIFFICULTIES: list[str] = ["beginner", "intermediate", "advanced"]
REFRESH_DAYS: int        = 30    # regenerate a label when older than this
_TABLE:       str        = "quiz_questions"

# Generation order: ALL beginner labels first so they are available fastest
GENERATION_PRIORITY: list[tuple[str, str]] = (
    [(lang, "beginner")      for lang in LANGUAGES]
    + [(lang, "intermediate") for lang in LANGUAGES]
    + [(lang, "advanced")     for lang in LANGUAGES]
)


def make_label(language: str, difficulty: str) -> str:
    """Canonical label used as the unique DB key.  e.g. 'french_beginner'"""
    return f"{language.strip().lower()}_{difficulty.strip().lower()}"


def flow_for(difficulty: str) -> str:
    return "cognate" if difficulty.lower() == "beginner" else "standard"


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — GEMINI RATE LIMITER
# ═══════════════════════════════════════════════════════════════════════════════

_MODEL_INTERVALS: dict[str, float] = {
    "gemini-2.5-flash":      6.5,
    "gemini-2.5-flash-lite": 4.5,
    "gemini-2.5-pro":        8.0,
}
_DEFAULT_INTERVAL: float = 6.5


def _gemini_model_name() -> str:
    return os.getenv("GEMINI_MODEL") or getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash")


def _call_interval() -> float:
    return _MODEL_INTERVALS.get(_gemini_model_name(), _DEFAULT_INTERVAL)


class _RateLimiter:
    """Enforces a minimum gap between consecutive Gemini API calls."""

    def __init__(self) -> None:
        self._last = 0.0
        self._lock  = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = _call_interval() - (time.monotonic() - self._last)
            if gap > 0:
                logger.debug("Rate limiter: sleeping %.2f s", gap)
                time.sleep(gap)
            self._last = time.monotonic()


_rate_limiter = _RateLimiter()


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — LLM CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

try:
    import google.generativeai as genai
    _HAS_GEMINI = True
except ImportError:
    _HAS_GEMINI = False

try:
    from groq import Groq
    _HAS_GROQ = True
except ImportError:
    _HAS_GROQ = False

_gemini_client: object     = None
_groq_client: Optional[object] = None


def _get_gemini():
    global _gemini_client
    if not _HAS_GEMINI:
        raise EnvironmentError("google-generativeai is not installed.")
    if _gemini_client is None:
        if not settings.GEMINI_API_KEY:
            raise EnvironmentError("GEMINI_API_KEY not set in .env")
        genai.configure(api_key=settings.GEMINI_API_KEY)
        _gemini_client = genai.GenerativeModel(
            _gemini_model_name(),
            generation_config=genai.types.GenerationConfig(
                response_mime_type="application/json"
            ),
        )
    return _gemini_client


def _get_groq():
    global _groq_client
    if not _HAS_GROQ:
        raise EnvironmentError("groq is not installed.")
    if _groq_client is None:
        if not settings.GROQ_API_KEY:
            raise EnvironmentError("GROQ_API_KEY not set in .env")
        _groq_client = Groq(api_key=settings.GROQ_API_KEY)
    return _groq_client


def _call_llm(prompt: str, temperature: float = 0.4, max_tokens: int = 1800) -> str:
    """Call the configured AI provider. Applies rate limiting for Gemini."""
    provider = getattr(settings, "AI_PROVIDER", "gemini")

    if provider == "gemini":
        _rate_limiter.wait()
        response = _get_gemini().generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
            ),
        )
        logger.debug("Gemini response received")
        return response.text

    if provider == "groq":
        completion = _get_groq().chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {"role": "system", "content": "You are a language tutor. Always respond with valid JSON only."},
                {"role": "user",   "content": prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        return completion.choices[0].message.content

    raise ValueError(f"Unknown AI_PROVIDER '{provider}'.")


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — BEGINNER STRATEGY REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════

_BEGINNER_STRATEGIES: dict[str, dict] = {
    "english": {
        "strategy_name": "Contextual / Action-based",
        "directive": (
            "Focus on universal high-frequency words with emojis: "
            "'Stop' 🛑, 'Go' 🚦, 'Hello' 👋, 'Yes' ✅, 'No' ❌, 'Open' 🔓, 'Help' 🆘. "
            "Embed the emoji in the question text so meaning is context-deducible."
        ),
        "transliteration_note": "Transliteration is N/A for English; put IPA in the tooltip.",
    },
    "japanese": {
        "strategy_name": "Loanwords (Katakana Sounds)",
        "directive": (
            "Use Romaji ONLY — never raw Kanji or Hiragana in question text. "
            "Pick English loanwords phonetically adapted to Japanese: "
            "'Kamera' (Camera), 'Kohii' (Coffee), 'Terebi' (TV), "
            "'Takushii' (Taxi), 'Rajio' (Radio), 'Suupaa' (Supermarket)."
        ),
        "transliteration_note": "Show Romaji AND Katakana e.g. 'Kamera / カメラ'.",
    },
    "german": {
        "strategy_name": "Structural Similarities",
        "directive": (
            "Use Germanic–English overlaps where visual similarity alone gives the meaning: "
            "'Haus' (House), 'Wasser' (Water), 'Finger', 'Arm', "
            "'Wind', 'Fisch' (Fish), 'Gold', 'Sand', 'Ring', 'Gras' (Grass)."
        ),
        "transliteration_note": "German pronunciation in parentheses e.g. (HOUSE for Haus).",
    },
    "french": {
        "strategy_name": "Perfect Cognates",
        "directive": (
            "Use words IDENTICAL or near-identical to English: "
            "'Restaurant', 'Taxi', 'Menu', 'Lion', 'Important', "
            "'Nation', 'Photo', 'Possible', 'Hôtel', 'Café'. "
            "The meaning must be instantly obvious to any English speaker."
        ),
        "transliteration_note": "French pronunciation in parentheses e.g. (reh-stoh-RAHN).",
    },
}

_DEFAULT_STRATEGY: dict = {
    "strategy_name": "Common Vocabulary",
    "directive": (
        "Choose the most universally-known words in {language} for English speakers — "
        "loanwords, international brand words, or words with strong visual similarity to English."
    ),
    "transliteration_note": "Provide pronunciation in simple English phonetics.",
}


def _get_strategy(language: str) -> dict:
    s = _BEGINNER_STRATEGIES.get(language.lower(), _DEFAULT_STRATEGY.copy())
    return {k: v.format(language=language) if isinstance(v, str) else v for k, v in s.items()}


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 5 — PROMPTS
# ═══════════════════════════════════════════════════════════════════════════════

_BEGINNER_PROMPT = """
You are a motivational {language} tutor designing EXACTLY 5 "Super Easy" first-win questions
for complete beginners who have never studied {language} before.

── Strategy: {strategy_name} ────────────────────────────────────────────────
{directive}
─────────────────────────────────────────────────────────────────────────────

PRONUNCIATION GUIDANCE
{transliteration_note}

RULES FOR ALL 5 QUESTIONS
1. Each question uses a DIFFERENT foreign word.
2. The foreign word MUST appear visibly in the question text.
3. MCQ options must be concrete, emoji-enhanced nouns or short phrases.
4. correct_answer must be exactly "A", "B", "C", or "D".
5. hints — exactly 3 progressive hints:
     hint 1: very vague category ("It's something you find in a city")
     hint 2: shape/sound clue ("It sounds almost identical in English")
     hint 3: near-giveaway ("Think of where you go to eat a meal")
6. success_message: short celebratory line (max 10 words) + 1 emoji.
7. bridge_fact: one sentence linking the word to English.
8. Return ONLY a raw JSON array of EXACTLY 5 objects — no markdown, no backticks.

[
  {{
    "id": 1,
    "flow": "cognate",
    "skill": "Cognate Recognition",
    "type": "mcq",
    "text": "<full question text including the {language} word>",
    "foreign_word": "<{language} word>",
    "transliteration": "<pronunciation hint>",
    "tooltip": "<hover translation + brief note>",
    "options": {{"A": "<text+emoji>", "B": "...", "C": "...", "D": "..."}},
    "correct_answer": "A",
    "hints": ["<vague>", "<sound/shape>", "<near-giveaway>"],
    "success_message": "<celebratory line>",
    "bridge_fact": "<one linking sentence>"
  }},
  ... 4 more with different words
]
"""

_STANDARD_PROMPT = """
You are an expert {language} language tutor. Generate EXACTLY 5 quiz questions
for a {difficulty} learner.

RULES
- Every MCQ option (A, B, C, D) must contain actual text — never empty strings.
- Question text must be complete and self-contained.
- Reading Comprehension: include the full passage, then ask the question.
- Vocabulary MCQ: name WHICH word you are testing in the question text.
- Every question must include a hints list with exactly 2 progressive hints:
    hint 1: gentle category clue (does not give the answer away)
    hint 2: specific grammatical or contextual nudge
- correct_answer must be exactly "A","B","C","D" for MCQ, null for open.
- Return ONLY a raw JSON array of EXACTLY 5 objects — no markdown, no backticks.

Language: {language}
Difficulty: {difficulty}

[
  {{"id":1,"flow":"standard","skill":"Grammar","type":"mcq",
    "text":"...","options":{{"A":"...","B":"...","C":"...","D":"..."}},"correct_answer":"A","hints":["...","..."]}},
  {{"id":2,"flow":"standard","skill":"Vocabulary","type":"mcq",
    "text":"...","options":{{"A":"...","B":"...","C":"...","D":"..."}},"correct_answer":"A","hints":["...","..."]}},
  {{"id":3,"flow":"standard","skill":"Reading Comprehension","type":"open",
    "text":"...","options":null,"correct_answer":null,"hints":["...","..."]}},
  {{"id":4,"flow":"standard","skill":"Situational Awareness","type":"mcq",
    "text":"...","options":{{"A":"...","B":"...","C":"...","D":"..."}},"correct_answer":"A","hints":["...","..."]}},
  {{"id":5,"flow":"standard","skill":"Writing Production","type":"open",
    "text":"...","options":null,"correct_answer":null,"hints":["...","..."]}}
]
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 6 — JSON PARSING & VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _extract_json_array(raw: str) -> Optional[str]:
    """Find the first balanced JSON array in raw text."""
    text = _strip_fences(raw)
    for start, ch in enumerate(text):
        if ch != "[":
            continue
        stack, in_str, esc = [], False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                esc = not esc and c == "\\"
                if not esc and c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c in "[{":
                stack.append(c)
            elif c in "]}":
                if not stack:
                    break
                op = stack.pop()
                if (op, c) not in (("[", "]"), ("{", "}")):
                    break
                if not stack:
                    return text[start:i + 1]
    return None


def _validate_questions(batch: list) -> None:
    """Raise ValueError if the 5-question batch has any structural problem."""
    if not isinstance(batch, list) or len(batch) < 5:
        raise ValueError(f"Need 5 questions, got {len(batch) if isinstance(batch, list) else type(batch).__name__}")
    for q in batch[:5]:
        qid = q.get("id", "?")
        if not str(q.get("text", "")).strip():
            raise ValueError(f"Q{qid}: empty text")
        if q.get("type") == "mcq":
            opts = q.get("options") or {}
            missing = [k for k in ("A","B","C","D") if not str(opts.get(k,"")).strip()]
            if missing:
                raise ValueError(f"Q{qid}: empty options {missing}")
            if q.get("correct_answer") not in ("A","B","C","D"):
                raise ValueError(f"Q{qid}: correct_answer must be A/B/C/D, got {q.get('correct_answer')!r}")
        if not isinstance(q.get("hints"), list) or len(q["hints"]) < 2:
            raise ValueError(f"Q{qid}: needs at least 2 hints")


def _parse_response(raw: str) -> list[dict]:
    """Parse LLM output into a validated list of exactly 5 question dicts."""
    # Try direct strip first, then balanced extraction
    for candidate in [_strip_fences(raw), _extract_json_array(raw)]:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            # Unwrap any container dict the model might emit
            if isinstance(parsed, dict):
                for key in ("questions","data","items","quiz","batch"):
                    if isinstance(parsed.get(key), list):
                        parsed = parsed[key]
                        break
                else:
                    # single-value dict with a list
                    lists = [v for v in parsed.values() if isinstance(v, list)]
                    if len(lists) == 1:
                        parsed = lists[0]
            _validate_questions(parsed)
            return parsed[:5]
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    raise ValueError("Could not extract a valid 5-question array from LLM response")


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 7 — SINGLE-LABEL GENERATOR
# ═══════════════════════════════════════════════════════════════════════════════

def generate_questions(language: str, difficulty: str) -> list[dict]:
    """
    Call the LLM once, parse and validate the 5-question response.
    Retries up to 3 times on malformed JSON.
    Rate limiter fires automatically inside _call_llm for Gemini.
    """
    if difficulty.lower() == "beginner":
        strat  = _get_strategy(language)
        prompt = _BEGINNER_PROMPT.format(
            language=language,
            strategy_name=strat["strategy_name"],
            directive=strat["directive"],
            transliteration_note=strat["transliteration_note"],
        )
        temp = 0.35
    else:
        prompt = _STANDARD_PROMPT.format(language=language, difficulty=difficulty)
        temp   = 0.30

    label = make_label(language, difficulty)
    last_err = None

    for attempt in range(1, 4):
        try:
            raw    = _call_llm(prompt, temperature=temp, max_tokens=1800)
            result = _parse_response(raw)
            logger.info("  [%s] questions generated (attempt %d)", label, attempt)
            return result
        except (json.JSONDecodeError, ValueError) as exc:
            last_err = exc
            logger.warning("  [%s] attempt %d/3 failed: %s", label, attempt, exc)

    raise ValueError(f"[{label}] all 3 attempts failed: {last_err}")


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 8 — SUPABASE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _db() -> Client:
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_KEY:
        raise EnvironmentError("Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)


def _fetch_row(db: Client, label: str) -> Optional[dict]:
    """Return the existing row for *label*, or None if missing."""
    rows = (
        db.table(_TABLE)
        .select("label, generated_at")
        .eq("label", label)
        .limit(1)
        .execute()
    )
    return rows.data[0] if rows.data else None


def _row_age_days(row: dict) -> Optional[float]:
    try:
        ts = datetime.fromisoformat(row["generated_at"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86_400
    except (KeyError, ValueError):
        return None


def _upsert(db: Client, language: str, difficulty: str, questions: list[dict]) -> None:
    """
    Insert or replace the row for this label.
    Conflict target: label (UNIQUE).
    """
    db.table(_TABLE).upsert(
        {
            "label":        make_label(language, difficulty),
            "language":     language.lower(),
            "difficulty":   difficulty.lower(),
            "flow":         flow_for(difficulty),
            "questions":    questions,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="label",
    ).execute()


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 9 — STATUS REPORTER
# ═══════════════════════════════════════════════════════════════════════════════

def print_status(db: Client) -> None:
    """Print a table showing which labels are stored, complete, and fresh."""
    print("\n── Quiz Question Store Status ──────────────────────────────────────")
    print(f"  {'label':<28}  {'flow':<10}  {'stored?':<10}  age")
    print("  " + "─" * 60)

    total = 0
    for language, difficulty in GENERATION_PRIORITY:
        label = make_label(language, difficulty)
        row   = _fetch_row(db, label)
        age   = _row_age_days(row) if row else None

        stored_mark = "✓  yes" if row else "✗  missing"
        age_str     = f"{age:.1f}d" if age is not None else "—"
        stale_tag   = "  ← STALE" if (age is not None and age >= REFRESH_DAYS) else ""

        print(f"  {label:<28}  {flow_for(difficulty):<10}  {stored_mark:<10}  {age_str}{stale_tag}")
        if row:
            total += 1

    print("  " + "─" * 60)
    print(f"  {total}/12 labels stored  ({total * 5}/60 questions)\n")


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 10 — MAIN RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

class _LabelResult:
    def __init__(self, language: str, difficulty: str) -> None:
        self.label      = make_label(language, difficulty)
        self.language   = language
        self.difficulty = difficulty
        self.saved      = False
        self.skipped    = False
        self.skip_reason = ""
        self.error      = ""


def run(
    force:       bool            = False,
    lang_filter: Optional[str]   = None,
    diff_filter: Optional[str]   = None,
) -> None:
    """
    Generate and store questions for all matching labels.

    Args:
        force:       Regenerate even if the label is fresh.
        lang_filter: Only process this language (case-insensitive).
        diff_filter: Only process this difficulty.
    """
    db = _db()
    logger.info("Connected to Supabase ✓")

    work = [
        (lang, diff)
        for lang, diff in GENERATION_PRIORITY
        if (lang_filter is None or lang.lower() == lang_filter.lower())
        and (diff_filter is None or diff.lower() == diff_filter.lower())
    ]

    if not work:
        logger.error(
            "No matching combos for lang=%s diff=%s\n"
            "  Valid languages   : %s\n"
            "  Valid difficulties: %s",
            lang_filter, diff_filter, LANGUAGES, DIFFICULTIES,
        )
        sys.exit(1)

    model   = _gemini_model_name()
    interval = _call_interval()
    logger.info(
        "=== Question Generator starting ===\n"
        "  Labels to process : %d / 12\n"
        "  Questions per label: 5\n"
        "  Max API calls     : %d\n"
        "  Model             : %s  (%.1f s/call → %.1f RPM)\n"
        "  Est. max duration : ~%.0f s\n"
        "  Force regenerate  : %s",
        len(work), len(work),
        model, interval, 60 / interval,
        len(work) * interval,
        force,
    )

    results: list[_LabelResult] = []
    start = time.monotonic()

    for n, (language, difficulty) in enumerate(work, 1):
        result = _LabelResult(language, difficulty)
        logger.info(
            "── Label %d/%d: %s ─────────────────────────────────",
            n, len(work), result.label,
        )

        # Check existing row
        existing = _fetch_row(db, result.label)
        if existing and not force:
            age = _row_age_days(existing)
            if age is not None and age < REFRESH_DAYS:
                result.skipped    = True
                result.skip_reason = f"fresh ({age:.1f}d old, threshold={REFRESH_DAYS}d)"
                logger.info("  SKIP — %s", result.skip_reason)
                results.append(result)
                continue

        # Generate and store
        try:
            questions = generate_questions(language, difficulty)
            _upsert(db, language, difficulty, questions)
            result.saved = True
            logger.info("  ✓  5 questions saved  [label=%s]", result.label)
        except Exception as exc:
            result.error = str(exc)
            logger.error("  ✗  FAILED: %s", exc)

        results.append(result)

    elapsed = time.monotonic() - start

    saved   = sum(1 for r in results if r.saved)
    skipped = sum(1 for r in results if r.skipped)
    failed  = sum(1 for r in results if r.error)

    logger.info(
        "\n=== Done in %.0f s ===\n"
        "  Saved    : %d labels  (%d questions)\n"
        "  Skipped  : %d labels  (still fresh)\n"
        "  Failed   : %d labels\n"
        "  Total DB : %d / 12 labels  (%d / 60 questions)",
        elapsed,
        saved,   saved * 5,
        skipped,
        failed,
        saved + skipped, (saved + skipped) * 5,
    )

    if failed:
        logger.warning("Re-run to retry failures — saved labels won't be regenerated.")


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 11 — CLI
# ═══════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="question_generator",
        description=(
            "Generate language quiz questions and upsert them into Supabase.\n"
            "Safe to re-run — fresh labels are skipped automatically.\n"
            "Respects Gemini free-tier rate limits."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python -m services.question_generator
  python -m services.question_generator --force
  python -m services.question_generator --status
  python -m services.question_generator --lang Japanese
  python -m services.question_generator --lang German --diff advanced
        """,
    )
    p.add_argument("--force",  action="store_true",
                   help="Regenerate all labels even if still within REFRESH_DAYS.")
    p.add_argument("--status", action="store_true",
                   help="Print store health and exit.")
    p.add_argument("--lang",   metavar="LANGUAGE", default=None,
                   help="Only process this language (English|Japanese|German|French).")
    p.add_argument("--diff",   metavar="DIFFICULTY", default=None,
                   help="Only process this difficulty (beginner|intermediate|advanced).")
    return p


if __name__ == "__main__":
    args = _build_parser().parse_args()
    if args.status:
        print_status(_db())
        sys.exit(0)
    run(force=args.force, lang_filter=args.lang, diff_filter=args.diff)