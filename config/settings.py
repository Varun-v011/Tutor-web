"""
config/settings.py
──────────────────
Central settings object loaded from environment variables via python-dotenv.
Every API key and credential lives here — no hardcoded secrets anywhere else.

Usage:
    from config.settings import settings
    print(settings.GEMINI_API_KEY)
"""

import os
from dotenv import load_dotenv

# Load .env file from the project root
load_dotenv()


class Settings:
    """Immutable settings bag populated from environment variables."""

    # ── Flask ─────────────────────────────────────────────────────────────────
    FLASK_ENV: str         = os.getenv("FLASK_ENV", "development")
    FLASK_SECRET_KEY: str  = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
    FLASK_PORT: int        = int(os.getenv("FLASK_PORT", "5000"))
    DEBUG: bool            = FLASK_ENV == "development"

    # ── AI Provider ──────────────────────────────────────────────────────────
    # Switch between "gemini" and "groq" without touching ai_service.py
    AI_PROVIDER: str       = os.getenv("AI_PROVIDER", "gemini").lower()

    GEMINI_API_KEY: str    = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str      = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    GROQ_API_KEY: str      = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str        = os.getenv("GROQ_MODEL", "llama3-70b-versatile")

    # ── Google Calendar ───────────────────────────────────────────────────────
    GOOGLE_CREDENTIALS_FILE: str = os.getenv(
        "GOOGLE_CREDENTIALS_FILE", "config/google_credentials.json"
    )
    GOOGLE_TOKEN_FILE: str       = os.getenv(
        "GOOGLE_TOKEN_FILE", "config/google_token.json"
    )
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_PROJECT_ID: str = os.getenv("GOOGLE_PROJECT_ID", "")


    CALENDAR_TIMEZONE: str       = os.getenv("CALENDAR_TIMEZONE", "Asia/Kolkata")
    EVENT_DURATION_MINUTES: int  = int(os.getenv("EVENT_DURATION_MINUTES", "30"))
    GOOGLE_CALENDAR_ID: str      = os.getenv("GOOGLE_CALENDAR_ID", "primary")

    # ── Email ─────────────────────────────────────────────────────────────────
    EMAIL_PROVIDER: str          = os.getenv("EMAIL_PROVIDER", "gmail").lower()

    # Gmail SMTP
    GMAIL_SENDER: str            = os.getenv("GMAIL_SENDER", "")
    GMAIL_APP_PASSWORD: str      = os.getenv("GMAIL_APP_PASSWORD", "")

    # Resend
    RESEND_API_KEY: str          = os.getenv("RESEND_API_KEY", "")
    RESEND_FROM_EMAIL: str       = os.getenv("RESEND_FROM_EMAIL", "")

    # ── Tutor branding ────────────────────────────────────────────────────────
    TUTOR_NAME: str              = os.getenv("TUTOR_NAME", "Your Tutor")
    TUTOR_EMAIL: str             = os.getenv("TUTOR_EMAIL", "")
    
    # ────Supabase────────────────────────────────────────────────────────────────────────
    SUPABASE_URL: str            = os.getenv("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY: str            = os.getenv("SUPABASE_SERVICE_KEY", "")
    SUPABASE_TABLE: str          = os.getenv("SUPABASE_TABLE", "")

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Comma-separated list of allowed frontend origins
    CORS_ORIGINS: list[str]      = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://localhost:5173,http://127.0.0.1:5500"
    ).split(",")
    
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3000")
    PASSWORD_RESET_TOKEN_TTL_MINUTES: int = int(os.getenv("PASSWORD_RESET_TOKEN_TTL_MINUTES", "30"))

    def validate(self) -> list[str]:
        """
        Return a list of human-readable warnings for missing critical settings.
        Call this at startup to catch configuration mistakes early.
        """
        warnings = []

        if self.AI_PROVIDER == "gemini" and not self.GEMINI_API_KEY:
            warnings.append("GEMINI_API_KEY is not set.")
        if self.AI_PROVIDER == "groq" and not self.GROQ_API_KEY:
            warnings.append("GROQ_API_KEY is not set.")
        if not os.path.exists(self.GOOGLE_CREDENTIALS_FILE):
            warnings.append(
                f"Google credentials file not found: {self.GOOGLE_CREDENTIALS_FILE}"
            )
        if self.EMAIL_PROVIDER == "gmail":
            if not self.GMAIL_SENDER or not self.GMAIL_APP_PASSWORD:
                warnings.append("GMAIL_SENDER or GMAIL_APP_PASSWORD is not set.")
        if self.EMAIL_PROVIDER == "resend" and not self.RESEND_API_KEY:
            warnings.append("RESEND_API_KEY is not set.")

        return warnings


# Singleton — import this everywhere
settings = Settings()
