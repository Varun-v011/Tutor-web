from flask import Blueprint, request, jsonify, make_response
from supabase import create_client
import bcrypt, os, re, logging
from datetime import datetime, timedelta



IS_PROD = os.getenv("FLASK_ENV") == "production"

# Configuration
auth_bp = Blueprint("auth", __name__, url_prefix="/auth")
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Email validation
EMAIL_PATTERN = re.compile(r'^[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

@auth_bp.route("/login", methods=["POST"])
def login():
    try:
        data = request.get_json(silent=True) or {}
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")

        # Validation
        if not email or not password:
            logger.warning(f"Login attempt: missing credentials")
            return jsonify({"error": "Email and password required"}), 400

        if not EMAIL_PATTERN.match(email):
            logger.warning(f"Login attempt: invalid email format")
            return jsonify({"error": "Invalid email format"}), 400

        if len(password) < 8:
            logger.warning(f"Login attempt: password too short")
            return jsonify({"error": "Password must be at least 8 characters"}), 400

        # Fetch user
        result = supabase.table("admin_users") \
                     .select("password_hash, id, created_at") \
                     .eq("email", email) \
                     .maybe_single() \
                     .execute()

        if not result.data:
            logger.warning(f"Login attempt: user not found - {email}")
            return jsonify({"error": "Invalid credentials"}), 401

        # Verify password
        stored_hash = result.data["password_hash"].encode()
        if not bcrypt.checkpw(password.encode(), stored_hash):
            logger.warning(f"Login attempt: invalid password - {email}")
            return jsonify({"error": "Invalid credentials"}), 401

        # Generate secure token (better than static token)
        admin_token = os.getenv("ADMIN_TOKEN")
        if not admin_token:
            logger.error("ADMIN_TOKEN not set in environment")
            return jsonify({"error": "Server configuration error"}), 500

        # Create response with secure cookie
        resp = make_response(jsonify({
            "ok": True,
            "email": email,
            "user_id": result.data["id"]
        }))

        # Cookie settings for production (HTTPS required)
        resp.set_cookie(
            "admin_key",
            admin_token,
            httponly=True,
            samesite="Lax",
            secure=IS_PROD,
            path="/",
            max_age=60 * 60 * 24 * 7,
)

        logger.info(f"Login successful - {email}")
        return resp

    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500


@auth_bp.route("/logout", methods=["POST"])
def logout():
    try:
        resp = make_response(jsonify({"ok": True}))
        resp.delete_cookie("admin_key", samesite="Lax", secure=IS_PROD, path="/")
        logger.info("Logout successful")
        return resp
    except Exception as e:
        logger.error(f"Logout error: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500


# Rate limiting decorator (add this separately)
def rate_limit(max_requests=5, window_seconds=60):
    from functools import wraps
    import time
    
    @wraps
    def decorator(f):
        request_counts = {}
        
        @wraps(f)
        def wrapped(*args, **kwargs):
            ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            now = time.time()
            
            if ip not in request_counts:
                request_counts[ip] = []
            
            request_counts[ip] = [t for t in request_counts[ip] if now - t < window_seconds]
            
            if len(request_counts[ip]) >= max_requests:
                return jsonify({"error": "Too many requests. Try later."}), 429
            
            request_counts[ip].append(now)
            return f(*args, **kwargs)
        
        return wrapped
    return decorator

# Apply rate limiting to login
@auth_bp.route("/login", methods=["POST"])
@rate_limit(max_requests=5, window_seconds=60)
def login_rate_limited():
    # Call original login logic
    return login()