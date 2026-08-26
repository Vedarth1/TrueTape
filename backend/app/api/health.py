from flask import Blueprint, jsonify
from sqlalchemy import text

from ..extensions import db

bp = Blueprint("health", __name__)


@bp.get("/health")
def health():
    checks = {"api": "ok"}
    status = 200
    try:
        db.session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc.__class__.__name__}"
        status = 503
    return jsonify({"status": "ok" if status == 200 else "degraded",
                    "checks": checks}), status