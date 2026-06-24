from functools import wraps
from flask import request, jsonify
import os

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "supersecrettoken123")

def require_admin(f):
    """Check admin_key cookie or x-admin-key header against ADMIN_TOKEN."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("x-admin-key") or request.cookies.get("admin_key","admin123")
        if not key or key != ADMIN_TOKEN:  # ✅ was ADMIN_PASSWORD, now ADMIN_TOKEN
            return jsonify({"error": "Unauthorized"}), 401
        class AdminUser:
            class user:
                email = "admin"
        return f( *args, **kwargs)
    return decorated