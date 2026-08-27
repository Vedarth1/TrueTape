def init_ingestion(app):
    from app.ingestion.routes import bp
    app.register_blueprint(bp)