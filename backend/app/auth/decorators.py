# backend/app/auth/decorators.py
from functools import wraps

from flask import jsonify
from flask_jwt_extended import get_jwt, verify_jwt_in_request


def role_required(*roles):
    """Backend-enforced RBAC. Verifies the JWT, then checks the role claim.
    Usage: @role_required("reviewer")  or  @role_required("operator", "reviewer")
    """
    def wrapper(fn):
        @wraps(fn)
        def decorated(*args, **kwargs):
            verify_jwt_in_request()
            role = get_jwt().get("role")
            if role not in roles:
                return jsonify({"error": {
                    "code": "FORBIDDEN",
                    "message": "your role may not perform this action",
                    "details": {"required": list(roles), "actual": role},
                }}), 403
            return fn(*args, **kwargs)
        return decorated
    return wrapper