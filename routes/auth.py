from flask import Blueprint, request, jsonify, make_response
from supabase import create_client
import bcrypt, os

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

@auth_bp.route("/login", methods=["POST"])
def login():
    data     = request.get_json(silent=True) or {}
    email    = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    result = supabase.table("admin_users") \
             .select("password_hash") \
             .eq("email", email) \
             .single() \
             .execute()

    if not result.data:
        return jsonify({"error": "Invalid credentials"}), 401

    stored_hash = result.data["password_hash"].encode()
    if not bcrypt.checkpw(password.encode(), stored_hash):
        return jsonify({"error": "Invalid credentials"}), 401

    resp = make_response(jsonify({"ok": True, "email": email}))
    resp.set_cookie(
        "admin_key",
        os.getenv("ADMIN_TOKEN", "supersecrettoken123"),  # ✅ matches auth_guard.py
        httponly=True,
        samesite="Lax",
        max_age=60 * 60 * 24 * 7,
    )
    return resp

@auth_bp.route("/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("admin_key")
    return resp