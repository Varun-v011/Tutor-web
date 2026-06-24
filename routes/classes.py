"""
routes/classes.py
──────────────────
Class schedule + enrollment notification module.
Minimal, no extra dependencies, Render free-tier friendly.

enrolled_count is a plain integer admin updates manually in the dashboard —
no complex enrollment tracking, no auth, no payment.

Public:
  GET  /classes                   list classes (with seat availability calc)
  POST /classes/<id>/interest     student submits interest → admin notified

Admin (x-admin-key or cookie):
  GET    /admin/classes                 list all classes
  POST   /admin/classes                 create class
  PATCH  /admin/classes/<id>            update any field (incl. enrolled_count)
  DELETE /admin/classes/<id>            delete class
  GET    /admin/classes/requests        all interest form submissions
  PATCH  /admin/classes/requests/<id>   update request status (contacted/enrolled/declined)
"""
import logging
import os
from flask import Blueprint, request, jsonify
from supabase import create_client
from middleware.auth_guard import require_admin

logger = logging.getLogger(__name__)
classes_bp = Blueprint("classes", __name__)

_sb = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY"),
)

LANGUAGES     = {"English", "German", "Japanese", "French"}
VALID_STATUS  = {"upcoming", "ongoing", "completed", "paused"}
VALID_REQ_ST  = {"new", "contacted", "enrolled", "declined"}


def _add_seat_meta(cls: dict) -> dict:
    """Compute available seats and fill percentage from stored counts."""
    enrolled   = cls.get("enrolled_count", 0) or 0
    max_seats  = cls.get("max_seats", 20) or 20
    cls["enrolled"]  = enrolled
    cls["available"] = max(0, max_seats - enrolled)
    cls["pct_full"]  = round(enrolled / max_seats * 100) if max_seats else 0
    return cls


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC
# ─────────────────────────────────────────────────────────────────────────────

@classes_bp.get("/classes")
def public_list_classes():
    """
    Returns classes visible to students.
    Query params: language, status (comma-separated, default: ongoing,upcoming)
    """
    language = request.args.get("language", "").strip()
    statuses = [s.strip() for s in
                request.args.get("status", "ongoing,upcoming").split(",")
                if s.strip() in VALID_STATUS]
    if not statuses:
        statuses = ["ongoing", "upcoming"]

    try:
        q = (
            _sb.table("classes")
            .select("id,name,language,level,instructor,description,"
                    "schedule,start_date,max_seats,enrolled_count,price,status")
            .in_("status", statuses)
            .order("status")          # ongoing first, then upcoming
            .order("created_at")
        )
        if language and language in LANGUAGES:
            q = q.eq("language", language)

        rows = q.execute().data
    except Exception as e:
        logger.error("Public classes fetch error: %s", e)
        return jsonify({"error": "Could not load classes."}), 500

    return jsonify([_add_seat_meta(r) for r in rows])


@classes_bp.post("/classes/<class_id>/interest")
def submit_interest(class_id: str):
    """
    Student expresses interest. Saves an enrollment_request record.
    Admin sees it in the dashboard under Enrollment Requests.
    No payment, no auth, no complexity.
    """
    body  = request.get_json(silent=True) or {}
    name  = str(body.get("name",  "")).strip()
    email = str(body.get("email", "")).strip()
    phone = str(body.get("phone", "")).strip()
    msg   = str(body.get("message", "")).strip()

    if not name or not email or "@" not in email:
        return jsonify({"error": "Please provide a valid name and email."}), 400

    # Fetch class to validate it exists and isn't closed
    try:
        cls = (
            _sb.table("classes")
            .select("name, status")
            .eq("id", class_id)
            .single()
            .execute()
            .data
        )
    except Exception:
        return jsonify({"error": "Class not found."}), 404

    if cls["status"] in ("completed", "paused"):
        return jsonify({"error": "This class is not currently accepting requests."}), 409

    # Prevent duplicate from same email for same class
    existing = (
        _sb.table("enrollment_requests")
        .select("id")
        .eq("class_id", class_id)
        .eq("email", email)
        .execute()
        .data
    )
    if existing:
        return jsonify({
            "ok":      True,
            "message": "We already have your request. We'll be in touch soon!"
        })

    try:
        _sb.table("enrollment_requests").insert({
            "class_id":   class_id,
            "class_name": cls["name"],
            "name":       name,
            "email":      email,
            "phone":      phone or None,
            "message":    msg or None,
        }).execute()
    except Exception as e:
        logger.error("Interest insert failed: %s", e)
        return jsonify({"error": "Could not save your request. Please try again."}), 500

    logger.info("Interest: %s <%s> → %s", name, email, cls["name"])
    return jsonify({
        "ok":      True,
        "message": "Request received! We'll contact you within 24 hours."
    }), 201


# ─────────────────────────────────────────────────────────────────────────────
#  ADMIN — CLASSES CRUD
# ─────────────────────────────────────────────────────────────────────────────

@classes_bp.get("/admin/classes")
@require_admin
def admin_list_classes():
    """All classes with seat meta + pending request count."""
    try:
        classes = (
            _sb.table("classes")
            .select("*")
            .order("created_at", desc=True)
            .execute()
            .data
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Count new (unhandled) requests per class in one query
    try:
        req_rows = (
            _sb.table("enrollment_requests")
            .select("class_id")
            .eq("status", "new")
            .execute()
            .data
        )
        pending: dict[str, int] = {}
        for r in req_rows:
            cid = r["class_id"]
            pending[cid] = pending.get(cid, 0) + 1
    except Exception:
        pending = {}

    result = []
    for c in classes:
        c = _add_seat_meta(c)
        c["pending_requests"] = pending.get(c["id"], 0)
        result.append(c)

    return jsonify(result)


@classes_bp.post("/admin/classes")
@require_admin
def admin_create_class():
    body = request.get_json(silent=True) or {}

    required = ["name", "language", "level", "schedule"]
    missing  = [f for f in required if not str(body.get(f, "")).strip()]
    if missing:
        return jsonify({"error": f"Missing fields: {missing}"}), 400
    if body["language"] not in LANGUAGES:
        return jsonify({"error": f"language must be one of {sorted(LANGUAGES)}"}), 400

    payload = {
        "name":           body["name"].strip(),
        "language":       body["language"],
        "level":          body.get("level", "").strip(),
        "instructor":     body.get("instructor", "TBD").strip(),
        "description":    body.get("description", "").strip() or None,
        "schedule":       body["schedule"].strip(),
        "start_date":     body.get("start_date") or None,
        "max_seats":      max(1, int(body.get("max_seats", 20))),
        "enrolled_count": max(0, int(body.get("enrolled_count", 0))),
        "price":          body.get("price", "Contact us").strip(),
        "status":         body.get("status", "upcoming"),
    }
    if payload["status"] not in VALID_STATUS:
        return jsonify({"error": "Invalid status."}), 400

    try:
        row = _sb.table("classes").insert(payload).execute().data[0]
        logger.info("Admin created class: %s", row["name"])
        return jsonify(_add_seat_meta(row)), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


UPDATABLE_FIELDS = {
    "name", "language", "level", "instructor", "description",
    "schedule", "start_date", "max_seats", "enrolled_count", "price", "status"
}

@classes_bp.patch("/admin/classes/<class_id>")
@require_admin
def admin_update_class(class_id: str):
    """
    Update any field on a class.
    Admin uses this to bump enrolled_count manually —
    just send {"enrolled_count": 14}.
    """
    body    = request.get_json(silent=True) or {}
    payload = {k: v for k, v in body.items() if k in UPDATABLE_FIELDS}

    if "status" in payload and payload["status"] not in VALID_STATUS:
        return jsonify({"error": "Invalid status."}), 400
    if "enrolled_count" in payload:
        payload["enrolled_count"] = max(0, int(payload["enrolled_count"]))
    if not payload:
        return jsonify({"error": "No valid fields to update."}), 400

    try:
        row = _sb.table("classes").update(payload).eq("id", class_id).execute().data[0]
        logger.info("Admin updated class %s: %s", class_id, list(payload.keys()))
        return jsonify(_add_seat_meta(row))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@classes_bp.delete("/admin/classes/<class_id>")
@require_admin
def admin_delete_class(class_id: str):
    try:
        _sb.table("enrollment_requests").delete().eq("class_id", class_id).execute()
        _sb.table("classes").delete().eq("id", class_id).execute()
        logger.info("Admin deleted class %s", class_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
#  ADMIN — ENROLLMENT REQUESTS
# ─────────────────────────────────────────────────────────────────────────────

@classes_bp.get("/admin/classes/requests")
@require_admin
def admin_list_requests():
    """All interest requests, newest first. Filter by ?status=new"""
    status_filter = request.args.get("status", "")
    try:
        q = (
            _sb.table("enrollment_requests")
            .select("*")
            .order("created_at", desc=True)
        )
        if status_filter in VALID_REQ_ST:
            q = q.eq("status", status_filter)
        return jsonify(q.execute().data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@classes_bp.patch("/admin/classes/requests/<req_id>")
@require_admin
def admin_update_request(req_id: str):
    """Mark a request as contacted / enrolled / declined."""
    body       = request.get_json(silent=True) or {}
    new_status = body.get("status")
    if new_status not in VALID_REQ_ST:
        return jsonify({"error": f"status must be one of {VALID_REQ_ST}"}), 400
    try:
        row = (
            _sb.table("enrollment_requests")
            .update({"status": new_status})
            .eq("id", req_id)
            .execute()
            .data[0]
        )
        logger.info("Request %s → %s", req_id, new_status)
        return jsonify(row)
    except Exception as e:
        return jsonify({"error": str(e)}), 500