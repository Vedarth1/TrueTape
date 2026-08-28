import os
from datetime import timedelta


def _require_jwt_secret(env: str) -> str:
    """Resolve JWT_SECRET_KEY, refusing the weak dev fallback in production.

    In dev/CI a known fallback keeps `flask run` and pytest frictionless. In
    production a predictable signing key means anyone can forge a reviewer
    token, so an unset-or-default secret is a hard boot failure rather than a
    silent weak key. Generate one with:
        python -c "import secrets; print(secrets.token_hex(32))"
    """
    secret = os.environ.get("JWT_SECRET_KEY")
    if env == "production" and (not secret or secret == "dev-only-never-in-prod"):
        raise RuntimeError(
            "JWT_SECRET_KEY must be set to a strong random value when "
            "APP_ENV=production; refusing to boot with the dev fallback.")
    return secret or "dev-only-never-in-prod"


class Config:
    # Deployment environment, read once here so BOTH this module's production
    # guard and the CLI's reset-db safety check read the same value. Flask
    # stopped populating config["ENV"] in 2.3, so without this the reset-db
    # "refuses to run in production" check silently sees None and never fires.
    ENV = os.environ.get("APP_ENV") or os.environ.get("FLASK_ENV") or "development"

    SQLALCHEMY_DATABASE_URI = os.environ["DATABASE_URL"]
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    JWT_SECRET_KEY = _require_jwt_secret(ENV)
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=12)
    CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")

    AI_PROVIDER = os.environ.get("AI_PROVIDER", "deterministic")
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    GROQ_REASONING_EFFORT = os.environ.get("GROQ_REASONING_EFFORT", "low")
    AI_TIMEOUT_SECONDS = int(os.environ.get("AI_TIMEOUT_SECONDS", "20"))
    MIGRATION_DATABASE_URL = os.environ.get("MIGRATION_DATABASE_URL", "")

    UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/data/uploads")
    MAX_CONTENT_LENGTH = 64 * 1024 * 1024   # 64 MB


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TEST_DATABASE_URL", os.environ["DATABASE_URL"]
    )