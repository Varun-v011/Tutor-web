from dotenv import load_dotenv
load_dotenv()

import logging
import sys
from flask import Flask, jsonify
from flask_cors import CORS
from extensions import limiter

from config.settings import settings
from routes.quiz import quiz_bp
from routes.auth import auth_bp
from routes.admin import admin_bp
from routes.slots import slots_bp
from services.ai_service import load_store


logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = settings.FLASK_SECRET_KEY

    limiter.init_app(app)

    CORS(app, resources={r"/*": {"origins": settings.CORS_ORIGINS}}, supports_credentials=True)
    logger.info("CORS enabled for origins: %s", settings.CORS_ORIGINS)

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(quiz_bp)
    app.register_blueprint(slots_bp)
    load_store()
    @app.errorhandler(404)

    def not_found(e):
        return jsonify({"error": "Endpoint not found.", "status": 404}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify({"error": "HTTP method not allowed.", "status": 405}), 405

    @app.errorhandler(500)
    def internal_error(e):
        logger.exception("Unhandled 500 error")
        return jsonify({"error": "An unexpected server error occurred.", "status": 500}), 500

    config_warnings = settings.validate()
    if config_warnings:
        logger.warning("Configuration warnings:")
        for w in config_warnings:
            logger.warning("   • %s", w)
    else:
        logger.info("Configuration validated — all keys present.")

    logger.info("Registered routes:")
    for rule in app.url_map.iter_rules():
        methods = ", ".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
        logger.info("   %-30s  [%s]", rule.rule, methods)

    return app


app = create_app()

if __name__ == "__main__":
    logger.info("Starting on port %d (debug=%s)", settings.FLASK_PORT, settings.DEBUG)
    app.run(host="0.0.0.0", port=settings.FLASK_PORT, debug=settings.DEBUG)