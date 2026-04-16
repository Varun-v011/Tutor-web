"""
app.py — Entry point for the Lingua Tutor Flask backend.
══════════════════════════════════════════════════════════

Run in development:
    python app.py

Run in production (via Gunicorn):
    gunicorn app:app --workers 4 --bind 0.0.0.0:5000

Run with auto-reload:
    flask --app app run --debug
"""

import logging
import sys
from flask import Flask, jsonify
from flask_cors import CORS

from config.settings import settings
from routes.quiz import quiz_bp


# ── Logging ───────────────────────────────────────────────────────────────────
# Configure before anything else so all modules use the same format.
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ── Factory function (also usable for testing) ────────────────────────────────

def create_app() -> Flask:
    """
    Application factory.

    Creates and configures the Flask app:
      - Sets secret key
      - Registers CORS
      - Mounts all blueprints
      - Attaches global error handlers

    Returns:
        Configured Flask application instance.
    """
    app = Flask(__name__)
    app.secret_key = settings.FLASK_SECRET_KEY

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Allow requests from the origins listed in .env.
    # In production, restrict this to your frontend domain only.
    CORS(app, resources={r"/*": {"origins": settings.CORS_ORIGINS}})
    logger.info("CORS enabled for origins: %s", settings.CORS_ORIGINS)

    # ── Register blueprints ───────────────────────────────────────────────────
    app.register_blueprint(quiz_bp)
    logger.info("Blueprint registered: quiz_bp")

    # ── Global error handlers ─────────────────────────────────────────────────

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "Endpoint not found.", "status": 404}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify({
            "error": "HTTP method not allowed on this endpoint.",
            "status": 405,
        }), 405

    @app.errorhandler(500)
    def internal_error(e):
        logger.exception("Unhandled 500 error")
        return jsonify({
            "error": "An unexpected server error occurred.",
            "status": 500,
        }), 500

    # ── Startup config validation ─────────────────────────────────────────────
    # Warn about missing credentials at boot so they're caught immediately.
    config_warnings = settings.validate()
    if config_warnings:
        logger.warning("⚠  Configuration warnings:")
        for w in config_warnings:
            logger.warning("   • %s", w)
    else:
        logger.info("✓  Configuration validated — all keys present.")

    # ── Route summary ──────────────────────────────────────────────────────────
    logger.info("Registered routes:")
    for rule in app.url_map.iter_rules():
        methods = ", ".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
        logger.info("   %-30s  [%s]", rule.rule, methods)

    return app


# ── Entrypoint ────────────────────────────────────────────────────────────────
app = create_app()

if __name__ == "__main__":
    logger.info(
        "Starting Lingua Tutor API on port %d (debug=%s)",
        settings.FLASK_PORT,
        settings.DEBUG,
    )
    app.run(
        host="0.0.0.0",
        port=settings.FLASK_PORT,
        debug=settings.DEBUG,
    )
