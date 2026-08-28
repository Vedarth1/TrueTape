# backend/app/exceptions/__init__.py
"""Exception queue API: list, inspect, resolve, batch-resolve.

Named `app.exceptions` alongside `app.models.exception` the same way
`app.ingestion` sits alongside `app.models.ingestion` -- service package
and its tables, one per module. The class is `ExceptionRecord`, not
`Exception`, so there is no builtin shadowing at the model level, and
`app.exceptions` as a subpackage does not shadow the top-level `exceptions`
module either.
"""

def init_exceptions(app):
    from app.exceptions.routes import bp
    app.register_blueprint(bp)
