# 🎓 Lingua Tutor — Flask Lead-Generation Backend

> Modular Flask API for a language tutor. AI quiz generation, Google Calendar booking with auto Meet links, and transactional email — all wired together in a clean, scalable structure.

---

## 📁 Folder Structure

```
lingua-tutor/
│
├── app.py                          # Entry point — factory pattern, CORS, error handlers
│
├── config/
│   ├── __init__.py
│   └── settings.py                 # All settings from .env — one place to look
│
├── models/
│   ├── __init__.py
│   └── lead.py                     # Pydantic models: request/response contracts + Lead
│
├── routes/
│   ├── __init__.py
│   └── quiz.py                     # Blueprint: GET /health, POST /generate-quiz, POST /submit-quiz
│
├── services/
│   ├── __init__.py
│   ├── ai_service.py               # generate_questions() + grade_quiz() — Gemini or Groq
│   ├── google_calendar_service.py  # create_event() with auto Google Meet link
│   └── email_service.py            # send_booking_confirmation() — Gmail or Resend
│
├── requirements.txt
├── .env.example                    # ← Copy to .env and fill in your keys
└── README.md
```

---

## 🚀 Quick Start

```bash
# 1. Clone / unzip and enter the project
cd lingua-tutor

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — add your API keys

# 5. Authorise Google Calendar (first run only — opens a browser tab)
python services/google_calendar_service.py

# 6. Start the server
python app.py
# → http://localhost:5000
```

---

## 🔌 API Endpoints

### `GET /health`
Returns service status and any config warnings.

```json
{
  "status": "ok",
  "ai_provider": "gemini",
  "email_provider": "gmail",
  "config_warnings": []
}
```

---

### `POST /generate-quiz`

**Request:**
```json
{
  "language":     "French",
  "difficulty":   "intermediate",
  "student_name": "Sarah"
}
```

**Response:**
```json
{
  "language":   "French",
  "difficulty": "intermediate",
  "questions": [
    {
      "id": 1,
      "skill": "Grammar",
      "type": "mcq",
      "text": "Which sentence uses the correct subjunctive form?",
      "options": { "A": "...", "B": "...", "C": "...", "D": "..." }
    },
    ...
  ]
}
```

---

### `POST /submit-quiz`

Full pipeline: grade → book calendar → send email → return results.

**Request:**
```json
{
  "student_name":  "Sarah Johnson",
  "student_email": "sarah@example.com",
  "student_phone": "+91 98765 43210",
  "language":      "French",
  "answers": [
    { "question_id": 1, "question_text": "Which sentence...", "student_answer": "B" },
    { "question_id": 2, "question_text": "What does...", "student_answer": "She was tired" },
    ...
  ],
  "booking_start": "2025-08-15T14:00:00"
}
```

**Response:**
```json
{
  "message":       "Quiz graded and session booked successfully.",
  "email_sent":    true,
  "meet_link":     "https://meet.google.com/abc-defg-hij",
  "booking_start": "2025-08-15T14:00:00",
  "graded_result": {
    "overall_score":  72,
    "cefr_level":     "B2",
    "strengths":      ["Good vocabulary range", "Accurate past tense usage"],
    "weaknesses":     ["Subjunctive mood", "Complex sentence construction"],
    "recommendation": "Focus on subjunctive exercises...",
    "per_question": [
      { "question_id": 1, "score": 20, "feedback": "Correct! ..." },
      ...
    ]
  }
}
```

---

## 🔑 Swapping AI Provider

Edit `.env`:
```env
AI_PROVIDER=groq          # or "gemini"
GROQ_API_KEY=gsk_...
GROQ_MODEL=llama3-70b-8192
```

No code changes needed — the service layer handles both through `_call_llm()`.

---

## 📧 Swapping Email Provider

```env
EMAIL_PROVIDER=resend     # or "gmail"
RESEND_API_KEY=re_...
RESEND_FROM_EMAIL=tutor@yourdomain.com
```

---

## 📅 Google Calendar Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → Enable **Google Calendar API**
3. Create OAuth 2.0 credentials (Desktop app type)
4. Download JSON → save to `config/google_credentials.json`
5. Run once: `python services/google_calendar_service.py`
6. Complete the browser auth flow — `config/google_token.json` is created automatically

---

## 🏭 Production Deployment

```bash
# Gunicorn with 4 workers
gunicorn app:app --workers 4 --bind 0.0.0.0:5000 --timeout 120

# Or with uvicorn for async support
pip install uvicorn
uvicorn app:app --workers 4 --host 0.0.0.0 --port 5000
```

Set in `.env`:
```env
FLASK_ENV=production
CORS_ORIGINS=https://yourfrontend.com
```

---

## ⚙️ All Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FLASK_SECRET_KEY` | ✅ | — | Random secret string |
| `AI_PROVIDER` | ✅ | `gemini` | `gemini` or `groq` |
| `GEMINI_API_KEY` | if Gemini | — | From aistudio.google.com |
| `GROQ_API_KEY` | if Groq | — | From console.groq.com |
| `GOOGLE_CREDENTIALS_FILE` | ✅ | `config/google_credentials.json` | OAuth credentials |
| `GOOGLE_TOKEN_FILE` | auto | `config/google_token.json` | Auto-generated |
| `CALENDAR_TIMEZONE` | — | `Asia/Kolkata` | e.g. `America/New_York` |
| `EVENT_DURATION_MINUTES` | — | `30` | Session length |
| `EMAIL_PROVIDER` | ✅ | `gmail` | `gmail` or `resend` |
| `GMAIL_SENDER` | if Gmail | — | Your Gmail address |
| `GMAIL_APP_PASSWORD` | if Gmail | — | 16-char App Password |
| `RESEND_API_KEY` | if Resend | — | From resend.com |
| `RESEND_FROM_EMAIL` | if Resend | — | Verified sender address |
| `TUTOR_NAME` | — | `Your Tutor` | Shown in email + calendar |
| `TUTOR_EMAIL` | — | — | Added as organiser to events |
