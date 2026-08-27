from flask import Flask

from .config import Config
from .extensions import db, migrate, jwt, cors


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)

    if app.config["AI_PROVIDER"] == "groq" and not app.config["GROQ_API_KEY"]:
        raise RuntimeError(
            "AI_PROVIDER=groq but GROQ_API_KEY is empty. Set the key, or set "
            "AI_PROVIDER=deterministic to choose the stub explicitly."
        )
        

    db.init_app(app)
    from app import models
    from app.cli import register_cli
    register_cli(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    cors.init_app(
        app,
        resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}},
        supports_credentials=True,
    )

    # One blueprint per module. Registered here and nowhere else.
    from .api.health import bp as health_bp
    app.register_blueprint(health_bp, url_prefix="/api")

    return app