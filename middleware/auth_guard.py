
from functools import wraps
from flask import request, redirect, jsonify
from supabase import create_client
import os

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

ADMIN_EMAILS = [e.strip() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]


def require_auth(f):
    """Validates sb_token cookie. Injects user into route handler."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("sb_token")
        if not token:
            return redirect("/auth/login")
        try:
            user = supabase.auth.get_user(token)
            return f(user, *args, **kwargs)
        except Exception:
            return redirect("/auth/login")
    return decorated


def require_admin(f):
    """Like require_auth but also checks ADMIN_EMAILS allowlist."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("sb_token")
        if not token:
            return redirect("/auth/login")
        try:
            user = supabase.auth.get_user(token)
            email = user.user.email
            if ADMIN_EMAILS and email not in ADMIN_EMAILS:
                return jsonify({"error": "Forbidden — not an admin account"}), 403
            return f(user, *args, **kwargs)
        except Exception:
            return redirect("/auth/login")
    return decorated