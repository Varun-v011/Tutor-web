from flask import Blueprint, redirect, request, make_response
from supabase import create_client
import os
from extensions import limiter

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")


@auth_bp.route("/login")
@limiter.limit("10 per minute")
def login():
    response = supabase.auth.sign_in_with_oauth({
        "provider": "google",
        "options": {"redirect_to": os.getenv("REDIRECT_URL")}
    })
    return redirect(response.url)


@auth_bp.route("/callback")
def callback():
    code = request.args.get("code")
    session = supabase.auth.exchange_code_for_session({"auth_code": code})
    token = session.session.access_token
    resp = make_response(redirect(f"{FRONTEND_URL}/admin"))
    resp.set_cookie(
        "sb_token", token,
        httponly=True,
        samesite="None",   # required for cross-port cookie (5000 → 5173)
        secure=False,      # set True in production (HTTPS)
    )
    return resp


@auth_bp.route("/logout")
def logout():
    resp = make_response(redirect(f"{FRONTEND_URL}/"))
    resp.delete_cookie("sb_token")
    return resp