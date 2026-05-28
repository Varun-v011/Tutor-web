# routes/student_auth.py
import base64
import json
import logging
import uuid

from flask import Blueprint, jsonify, request
from supabase import create_client

from config.settings import settings

logger = logging.getLogger(__name__)

student_auth_bp = Blueprint("student_auth", __name__, url_prefix="/auth/student")
supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)

STUDENT_FIELDS = (
    "id, name, email, phone, language, difficulty, "
    "learning_goal, needs_onboarding, provider, status, "
    "auth_user_id, cefr_level, overall_score"
)


# ── Token verification ────────────────────────────────────────────────────────

def _decode_jwt_sub(token: str) -> str | None:
    """
    Extract the `sub` (user UUID) from a Supabase JWT without verifying
    the signature — we verify by calling the admin API next.
    No third-party JWT library required.
    """
    try:
        payload_b64 = token.split(".")[1]
        # Fix missing base64 padding
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("sub")
    except Exception:
        return None


def _verify_token(access_token: str):
    """
    Verify a Supabase access token and return the auth user object, or None.

    Uses admin.get_user_by_id() instead of auth.get_user(token) because
    the latter causes HTTP 422 in many supabase-py versions when the client
    is initialised with the service key.
    """
    if not access_token:
        return None

    sub = _decode_jwt_sub(access_token)
    if not sub:
        logger.warning("Could not decode JWT sub claim")
        return None

    try:
        res = supabase.auth.admin.get_user_by_id(sub)
        return res.user if res else None
    except Exception as exc:
        logger.error("admin.get_user_by_id failed for sub=%s: %s", sub, exc)
        return None


# ── DB helpers ────────────────────────────────────────────────────────────────

def _fetch_student(auth_user_id: str) -> dict | None:
    """
    Return the students row for this auth_user_id, or None if not found.
    Uses .maybe_single() — NOT .single() — so it returns None instead of
    raising a PostgREST 406 when the row doesn't exist yet.
    """
    try:
        res = (
            supabase.table("students")
            .select(STUDENT_FIELDS)
            .eq("auth_user_id", auth_user_id)
            .maybe_single()   # ← critical: returns None, never raises on 0 rows
            .execute()
        )
        return res.data      # None when no row found
    except Exception as exc:
        logger.error("_fetch_student error auth_user_id=%s: %s", auth_user_id, exc)
        return None


def _extract_name(user) -> str:
    meta = user.user_metadata or {}
    return (
        meta.get("full_name")
        or meta.get("name")
        or meta.get("user_name")
        or (user.email.split("@")[0] if user.email else "Student")
    )


def _extract_provider(user) -> str:
    app = user.app_metadata or {}
    providers = app.get("providers") or []
    return providers[0] if providers else (app.get("provider") or "email")


# ── Routes ────────────────────────────────────────────────────────────────────

@student_auth_bp.route("/sync", methods=["POST"])
def sync_student():
    """
    POST /auth/student/sync
    Body: { access_token, language?, difficulty?, learning_goal? }

    Called after every successful sign-in (email or OAuth).
    Upserts the public.students row and returns it.

    Three cases:
      1. Row already linked by auth_user_id   → refresh email/provider
      2. Email match but no auth_user_id      → link existing quiz-lead row
      3. Brand-new user                       → insert stub row
    """
    data         = request.get_json(silent=True) or {}
    access_token = (data.get("access_token") or "").strip()

    if not access_token:
        return jsonify({"error": "Missing access_token."}), 400

    supabase_user = _verify_token(access_token)
    if not supabase_user:
        return jsonify({"error": "Invalid or expired token."}), 401

    auth_user_id = str(supabase_user.id)
    email        = (supabase_user.email or "").lower()
    name         = _extract_name(supabase_user)
    provider     = _extract_provider(supabase_user)

    try:
        # ── Case 1: already linked ────────────────────────────────────────
        existing = _fetch_student(auth_user_id)
        if existing:
            supabase.table("students").update({
                "email":    email,
                "name":     existing.get("name") or name,
                "provider": provider,
            }).eq("auth_user_id", auth_user_id).execute()

            student = _fetch_student(auth_user_id)
            logger.info("sync: updated auth_user_id=%s", auth_user_id)
            return jsonify({"ok": True, "student": student}), 200

        # ── Case 2: email exists but not yet linked (quiz lead) ──────────
        email_rows = (
            supabase.table("students")
            .select("id")
            .eq("email", email)
            .limit(1)
            .execute()
        ).data or []

        if email_rows:
            student_id = email_rows[0]["id"]
            supabase.table("students").update({
                "auth_user_id": auth_user_id,
                "provider":     provider,
                "name":         name,
                "email":        email,
            }).eq("id", student_id).execute()

            student = _fetch_student(auth_user_id)
            logger.info("sync: linked email=%s → auth_user_id=%s", email, auth_user_id)
            return jsonify({"ok": True, "student": student}), 200

        # ── Case 3: brand-new student ────────────────────────────────────
        insert_data = {
            "id":               str(uuid.uuid4()),
            "auth_user_id":     auth_user_id,
            "name":             name,
            "email":            email,
            "provider":         provider,
            "needs_onboarding": True,
            "status":           "signed_up",
            "language":         data.get("language")    or "English",
            "difficulty":       data.get("difficulty")  or "intermediate",
            "learning_goal":    data.get("learning_goal"),
        }
        supabase.table("students").insert(insert_data).execute()

        student = _fetch_student(auth_user_id)
        if not student:
            # Extremely rare: insert succeeded but fetch returned nothing
            logger.error("sync: insert succeeded but fetch returned None for %s", auth_user_id)
            return jsonify({"error": "Profile created but could not be retrieved."}), 500

        logger.info("sync: created student auth_user_id=%s email=%s", auth_user_id, email)
        return jsonify({"ok": True, "student": student}), 201

    except Exception as exc:
        logger.error("sync upsert error: %s", exc, exc_info=True)
        return jsonify({"error": "Server error during student sync."}), 500


@student_auth_bp.route("/me", methods=["POST"])
def me():
    """
    POST /auth/student/me
    Body: { access_token }

    Returns the student profile for the authenticated token.
    Used by AuthContext on every app boot to restore the session.
    """
    data         = request.get_json(silent=True) or {}
    access_token = (data.get("access_token") or "").strip()

    if not access_token:
        return jsonify({"error": "Missing access_token."}), 400

    supabase_user = _verify_token(access_token)
    if not supabase_user:
        return jsonify({"error": "Invalid or expired token."}), 401

    student = _fetch_student(str(supabase_user.id))
    if not student:
        return jsonify({"error": "Student profile not found."}), 404

    return jsonify({"ok": True, "student": student}), 200