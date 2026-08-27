"""Extension singletons. Imported by create_app() and by every model module."""
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Alembic cannot emit "ALTER TABLE ... DROP CONSTRAINT <no name>". Postgres
# auto-names unnamed constraints things like loans_status_check1, and those
# names are not stable across databases. The first time you need to change a
# CHECK -- and you will, audit_events.event_type has fifteen values in it --
# the migration is unwritable and you are hand-editing SQL at 2am. Five lines
# now buys every future migration.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative base, so models get real Mapped[] typing."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


db = SQLAlchemy(model_class=Base)
migrate = Migrate()
cors = CORS()
jwt = JWTManager()