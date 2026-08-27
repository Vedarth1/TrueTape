import pytest

from app import create_app
from app.extensions import db as _db


@pytest.fixture(scope="session")
def app():
    app = create_app()
    app.config.update(TESTING=True)
    with app.app_context():
        yield app


@pytest.fixture
def session(app):
    """Each test runs in a transaction that is rolled back, so audit rows never
    persist and every test starts from the committed (empty) chain tip."""
    yield _db.session
    _db.session.rollback()
    _db.session.remove()