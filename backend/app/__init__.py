from flask import Flask

from .config import Config
from .extensions import db, migrate, jwt, cors


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)

    db.init_app(app)
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