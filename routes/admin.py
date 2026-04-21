import logging
from flask import Blueprint, jsonify, request
from middleware.auth_guard import require_admin
from supabase import create_client
from datetime import datetime, timezone
import os

logger = logging.getLogger(__name__)
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

ALLOWED_STATUSES = {
    "signed_up", "demo_booked", "demo_completed",
    "demo_no_show", "enrolled", "dropped"
}


@admin_bp.route("/dashboard")
@require_admin
def dashboard(user):
    return jsonify({"message": f"Welcome {user.user.email}", "email": user.user.email})


@admin_bp.route("/students")
@require_admin
def get_students(user):
    try:
        data = supabase.table("students").select("*").order("created_at", desc=True).execute()
        return jsonify(data.data)
    except Exception as e:
        logger.error("Failed to fetch students: %s", e)
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/not-booked")
@require_admin
def not_booked(user):
    try:
        data = supabase.table("v_not_booked").select("*").execute()
        return jsonify(data.data)
    except Exception as e:
        logger.error("Failed to fetch not-booked: %s", e)
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/upcoming-demos")
@require_admin
def upcoming_demos(user):
    try:
        data = supabase.table("v_upcoming_demos").select("*").execute()
        return jsonify(data.data)
    except Exception as e:
        logger.error("Failed to fetch upcoming demos: %s", e)
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/stats")
@require_admin
def stats(user):
    try:
        students = supabase.table("students").select("id, status, overall_score, language").execute().data
        bookings = supabase.table("demo_bookings").select("id, status, booking_start").execute().data
        now = datetime.now(timezone.utc)

        total = len(students)
        booked = sum(1 for s in students if s["status"] == "demo_booked")
        enrolled = sum(1 for s in students if s["status"] == "enrolled")
        cold_leads = sum(1 for s in students if s["status"] == "signed_up")
        no_shows = sum(1 for s in students if s["status"] == "demo_no_show")
        upcoming = sum(
            1 for b in bookings
            if b["status"] == "scheduled"
            and datetime.fromisoformat(b["booking_start"].replace("Z", "+00:00")) > now
        )
        scores = [s["overall_score"] for s in students if s.get("overall_score") is not None]
        avg_score = round(sum(scores) / len(scores)) if scores else 0

        lang_counts = {}
        for s in students:
            l = s.get("language") or "Unknown"
            lang_counts[l] = lang_counts.get(l, 0) + 1

        return jsonify({
            "total_students": total,
            "demo_booked": booked,
            "enrolled": enrolled,
            "cold_leads": cold_leads,
            "upcoming_demos": upcoming,
            "no_shows": no_shows,
            "avg_score": avg_score,
            "conversion_pct": round(booked / total * 100) if total else 0,
            "lang_breakdown": lang_counts,
        })
    except Exception as e:
        logger.error("Failed to fetch stats: %s", e)
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/students/<student_id>/status", methods=["POST"])
@require_admin
def update_status(user, student_id):
    body = request.get_json(silent=True) or {}
    new_status = body.get("status")
    if new_status not in ALLOWED_STATUSES:
        return jsonify({"error": f"Invalid status. Allowed: {sorted(ALLOWED_STATUSES)}"}), 400
    try:
        supabase.table("students").update({"status": new_status}).eq("id", student_id).execute()
        logger.info("Admin %s updated student %s → %s", user.user.email, student_id, new_status)
        return jsonify({"ok": True, "status": new_status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500