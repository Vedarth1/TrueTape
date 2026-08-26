import os


class Config:
    SQLALCHEMY_DATABASE_URI = os.environ["DATABASE_URL"]
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "dev-only-never-in-prod")
    CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")

    AI_PROVIDER = os.environ.get("AI_PROVIDER", "deterministic")
    AI_API_KEY = os.environ.get("AI_API_KEY") or None
    AI_MODEL = os.environ.get("AI_MODEL") or None

    UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/data/uploads")
    MAX_CONTENT_LENGTH = 64 * 1024 * 1024   # 64 MB


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TEST_DATABASE_URL", os.environ["DATABASE_URL"]
    )