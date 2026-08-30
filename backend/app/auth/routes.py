# backend/app/auth/routes.py
import bcrypt
from flask import Blueprint, jsonify, request
from flask_jwt_extended import (create_access_token, get_jwt,
                                get_jwt_identity, jwt_required)

from app.extensions import db, jwt
from app.models import User

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

# A precomputed hash of a throwaway password. When the email doesn't exist we
# still run one bcrypt.checkpw against this, so a missing account and a wrong
# password take the same time — no email-enumeration oracle from timing.
_DUMMY_HASH = bcrypt.hashpw(b"truetape-dummy", bcrypt.gensalt())


@bp.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").encode()
    if not email or not password:
        return jsonify({"error": {"code": "BAD_REQUEST",
                                  "message": "email and password are required"}}), 400

    user = db.session.execute(
        db.select(User).filter_by(email=email)).scalar_one_or_none()
    stored = user.password_hash.encode() if user else _DUMMY_HASH
    if not bcrypt.checkpw(password, stored) or user is None:
        return jsonify({"error": {"code": "INVALID_CREDENTIALS",
                                  "message": "email or password is incorrect"}}), 401

    # identity MUST be a string (the JWT `sub` claim), or flask-jwt-extended
    # raises "Subject must be a string".
    token = create_access_token(
        identity=str(user.id),
        additional_claims={"role": user.role, "name": user.name})
    return jsonify({"access_token": token,
                    "user": {"id": str(user.id), "name": user.name,
                             "email": user.email, "role": user.role}}), 200


@bp.post("/signup")
def signup():
    """Self-service registration. Creates a CONSUMER account (read-only).

    Operator and reviewer are privileged roles: they are provisioned by the
    seed/admin flow, never by open signup -- otherwise anyone could grant
    themselves write access to the verification pipeline.
    """
    import re
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not name or not email or not password:
        return jsonify({"error": {"code": "BAD_REQUEST",
                                  "message": "name, email and password are required"}}), 400
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"error": {"code": "BAD_EMAIL",
                                  "message": "that does not look like a valid email"}}), 400
    if len(password) < 8:
        return jsonify({"error": {"code": "WEAK_PASSWORD",
                                  "message": "password must be at least 8 characters"}}), 400

    existing = db.session.execute(
        db.select(User).filter_by(email=email)).scalar_one_or_none()
    if existing is not None:
        return jsonify({"error": {"code": "EMAIL_TAKEN",
                                  "message": "an account with this email already exists"}}), 409

    user = User(
        name=name,
        email=email,
        role="consumer",
        password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
    )
    db.session.add(user)
    db.session.commit()

    token = create_access_token(
        identity=str(user.id),
        additional_claims={"role": user.role, "name": user.name})
    return jsonify({"access_token": token,
                    "user": {"id": str(user.id), "name": user.name,
                             "email": user.email, "role": user.role}}), 201


@bp.get("/me")
@jwt_required()
def me():
    claims = get_jwt()
    return jsonify({"id": get_jwt_identity(),
                    "role": claims.get("role"),
                    "name": claims.get("name")}), 200


def init_auth(app):
    """Register the blueprint and the JWT failure handlers on the app."""
    app.register_blueprint(bp)

    @jwt.unauthorized_loader
    def _missing(reason):
        return jsonify({"error": {"code": "AUTH_REQUIRED", "message": reason}}), 401

    @jwt.invalid_token_loader
    def _invalid(reason):
        return jsonify({"error": {"code": "INVALID_TOKEN", "message": reason}}), 401

    @jwt.expired_token_loader
    def _expired(header, payload):
        return jsonify({"error": {"code": "TOKEN_EXPIRED",
                                  "message": "token has expired"}}), 401