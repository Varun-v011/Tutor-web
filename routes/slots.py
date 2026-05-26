# routes/slots.py
import logging
from flask import Blueprint, jsonify, request
from supabase import create_client
from datetime import datetime, timezone
import os

logger = logging.getLogger(__name__)
slots_bp = Blueprint("slots", __name__, url_prefix="/slots")
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

# ══════════════════════════════════════════════════════════════
#  STUDENT-FACING ROUTES
# ══════════════════════════════════════════════════════════════

# ── GET /slots ────────────────────────────────────────────────
@slots_bp.route("", methods=["GET"])
def get_available_slots():
    try:
        now = datetime.now(timezone.utc).isoformat()
        data = supabase.table("demo_slots") \
               .select("id, slot_start, slot_end, max_seats, booked_seats") \
               .eq("is_active", True) \
               .gt("slot_start", now) \
               .order("slot_start", desc=False) \
               .execute()
        available = [s for s in data.data if s["booked_seats"] < s["max_seats"]]
        return jsonify(available)
    except Exception as e:
        logger.error("Failed to fetch available slots: %s", e)
        return jsonify({"error": str(e)}), 500

# ── POST /slots/<slot_id>/book ────────────────────────────────
@slots_bp.route("/<slot_id>/book", methods=["POST"])
def book_slot(slot_id):
    body          = request.get_json(silent=True) or {}
    student_id    = body.get("student_id")
    student_name  = body.get("student_name")
    student_email = body.get("student_email")
    language      = body.get("language", "English")
    cefr_level    = body.get("cefr_level", "")
    overall_score = body.get("overall_score", "")

    if not all([student_id, student_name, student_email]):
        return jsonify({"error": "student_id, student_name and student_email are required"}), 400

    try:
        slot = supabase.table("demo_slots").select("*").eq("id", slot_id).eq("is_active", True).single().execute()

        if not slot.data:
            return jsonify({"error": "Slot not found or inactive"}), 404

        s = slot.data

        if s["booked_seats"] >= s["max_seats"]:
            return jsonify({"error": "Slot is fully booked"}), 409

        slot_start_dt = datetime.fromisoformat(s["slot_start"].replace("Z", "+00:00"))
        if slot_start_dt < datetime.now(timezone.utc):
            return jsonify({"error": "Slot has already passed"}), 410

        existing = supabase.table("demo_bookings").select("id").eq("student_id", student_id).eq("status", "scheduled").execute()
        if existing.data:
            return jsonify({"error": "Student already has an active booking"}), 409

        meet_link = s.get("meet_link")
        html_link = s.get("html_link")

        booking = supabase.table("demo_bookings").insert({
            "student_id":    student_id,
            "slot_id":       slot_id,
            "booking_start": s["slot_start"],
            "booking_end":   s["slot_end"],
            "meet_link":     meet_link,
            "html_link":     html_link,
            "status":        "scheduled",
        }).execute()

        supabase.table("demo_slots").update({"booked_seats": s["booked_seats"] + 1}).eq("id", slot_id).execute()
        supabase.table("students").update({"status": "demo_booked"}).eq("id", student_id).execute()

        try:
            _send_confirmation_email(
                student_name=student_name,
                student_email=student_email,
                language=language,
                cefr_level=cefr_level,
                overall_score=overall_score,
                slot_start=s["slot_start"],
                slot_end=s["slot_end"],
                meet_link=meet_link,
            )
        except Exception as email_err:
            logger.warning("Confirmation email failed: %s", email_err)

        logger.info("Student %s booked slot %s — meet: %s", student_id, slot_id, meet_link)

        return jsonify({
            "ok":         True,
            "booking_id": booking.data[0]["id"],
            "meet_link":  meet_link,
            "html_link":  html_link,
            "slot_start": s["slot_start"],
            "slot_end":   s["slot_end"],
        }), 201

    except Exception as e:
        logger.error("Failed to book slot: %s", e)
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════
#  ADMIN ROUTES  (prefix: /slots/admin)
# ══════════════════════════════════════════════════════════════

# ── GET /slots/admin ──────────────────────────────────────────
@slots_bp.route("/admin", methods=["GET"])
def admin_get_all_slots():
    try:
        data = supabase.table("demo_slots") \
               .select("id, slot_start, slot_end, max_seats, booked_seats, is_active, meet_link, html_link, calendar_event_id, created_at") \
               .order("slot_start", desc=False) \
               .execute()
        return jsonify(data.data)
    except Exception as e:
        logger.error("Admin: failed to fetch all slots: %s", e)
        return jsonify({"error": str(e)}), 500

# ── POST /slots/admin ─────────────────────────────────────────
@slots_bp.route("/admin", methods=["POST"])
def admin_create_slot():
    body = request.get_json(silent=True) or {}
    slot_start_str = body.get("slot_start")
    slot_end_str   = body.get("slot_end")
    max_seats      = int(body.get("max_seats", 1))
    title          = body.get("title", "Demo Session Slot")

    if not slot_start_str or not slot_end_str:
        return jsonify({"error": "slot_start and slot_end are required"}), 400

    try:
        slot_start_dt = datetime.fromisoformat(slot_start_str)
        slot_end_dt   = datetime.fromisoformat(slot_end_str)
        
        # 1. Create Google Calendar event + Meet link
        from services.google_calendar_service import create_slot_event
        cal = create_slot_event(slot_start=slot_start_dt, slot_end=slot_end_dt, title=title)
        
        # 2. Persist in DB
        row = supabase.table("demo_slots").insert({
            "slot_start": slot_start_str, "slot_end": slot_end_str,
            "max_seats": max_seats, "booked_seats": 0, "is_active": True,
            "meet_link": cal["meet_link"], "html_link": cal["html_link"], "calendar_event_id": cal["event_id"],
        }).execute()

        return jsonify({
            "ok": True, "slot_id": row.data[0]["id"], "meet_link": cal["meet_link"],
            "html_link": cal["html_link"], "calendar_event_id": cal["event_id"]
        }), 201

    except Exception as e:
        logger.error("Failed to create slot: %s", e)
        return jsonify({"error": str(e)}), 500

# ── PATCH /slots/admin/<slot_id> ──────────────────────────────
@slots_bp.route("/admin/<slot_id>", methods=["PATCH"])
def admin_update_slot(slot_id):
    body = request.get_json(silent=True) or {}
    updates = {}
    if "is_active" in body: updates["is_active"] = bool(body["is_active"])
    if "max_seats" in body: updates["max_seats"] = int(body["max_seats"])

    if not updates: return jsonify({"error": "Nothing to update."}), 400

    try:
        supabase.table("demo_slots").update(updates).eq("id", slot_id).execute()
        return jsonify({"ok": True, "updated": updates})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── DELETE /slots/admin/<slot_id> ─────────────────────────────
@slots_bp.route("/admin/<slot_id>", methods=["DELETE"])
def admin_delete_slot(slot_id):
    try:
        slot = supabase.table("demo_slots").select("*").eq("id", slot_id).single().execute()
        if not slot.data: return jsonify({"error": "Slot not found"}), 404
        if slot.data.get("booked_seats", 0) > 0:
            return jsonify({"error": "Cannot delete a slot with existing bookings."}), 409

        calendar_event_id = slot.data.get("calendar_event_id")
        if calendar_event_id:
            try:
                from services.google_calendar_service import delete_slot_event
                delete_slot_event(calendar_event_id)
            except Exception as cal_err:
                logger.warning("Could not delete calendar event %s: %s", calendar_event_id, cal_err)

        supabase.table("demo_slots").delete().eq("id", slot_id).execute()
        return jsonify({"ok": True, "deleted_slot_id": slot_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ══════════════════════════════════════════════════════════════
#  EMAIL HELPER
# ══════════════════════════════════════════════════════════════

def _send_confirmation_email(student_name, student_email, language, cefr_level, overall_score, slot_start, slot_end, meet_link):
    try:
        dt = datetime.fromisoformat(slot_start.replace("Z", "+00:00"))
        formatted_date = dt.strftime("%A, %d %B %Y")
        formatted_time = dt.strftime("%I:%M %p")
    except Exception:
        formatted_date = slot_start
        formatted_time = ""

    subject = f"Your {language} Demo Session is Confirmed! 🎓"
    
    email_data = {
        "student_name": student_name,
        "language": language,
        "cefr_level": cefr_level,
        "score": overall_score,
        "date": formatted_date,
        "time": formatted_time,
        "meet_link": meet_link or "No link provided"
    }

    text_body = (
        f"Hi {student_name},\n\n"
        f"Your demo session is booked!\n"
        f"Language: {language} | Level: {cefr_level} ({overall_score}/100)\n"
        f"Date: {formatted_date} at {formatted_time}\n"
        f"Join Here: {email_data['meet_link']}\n\n"
        f"Please be ready 5 minutes before your session!"
    )

    from services.email_service import send_email
    send_email(to=student_email, subject=subject, text_body=text_body, context_data=email_data)