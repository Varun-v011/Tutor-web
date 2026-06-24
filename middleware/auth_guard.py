from functools import wraps
from flask import request, jsonify
import os

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("x-admin-key") or request.cookies.get("admin_key")

        print("cookies:", dict(request.cookies))
        print("admin_key:", request.cookies.get("admin_key"))
        print("header:", request.headers.get("x-admin-key"))
        print("expected:", ADMIN_TOKEN)

        if not ADMIN_TOKEN:
            return jsonify({"error": "Server misconfigured"}), 500

        if not key or key != ADMIN_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401

        return f(*args, **kwargs)
    return decorated