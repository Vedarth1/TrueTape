"""Import every model module so Alembic sees the complete metadata.

Alembic autogenerate diffs db.metadata against the live database. A model
class in a module nobody imported is not in the metadata, so its table is
silently omitted -- the most common cause of "flask db migrate produced an
empty file". Adding a model means adding it here.
"""
from app.models.ai import AiRecommendation  # noqa: F401
from app.models.audit import AuditEvent  # noqa: F401
from app.models.decision import ReviewerDecision  # noqa: F401
from app.models.exception import (ExceptionCluster, ExceptionComment,  # noqa: F401
                                  ExceptionRecord)
from app.models.ingestion import RawFile, RawRecord  # noqa: F401
from app.models.loan import Loan, LoanCanonical, LoanRecord  # noqa: F401
from app.models.trust import SourceTrustConfig  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.validation import ValidationResult, ValidationRule  # noqa: F401
from app.models.verified import VerifiedRecord  # noqa: F401

__all__ = [
    "User",
    "RawFile", "RawRecord",
    "Loan", "LoanRecord", "LoanCanonical",
    "ValidationRule", "ValidationResult",
    "SourceTrustConfig",
    "ExceptionCluster", "ExceptionRecord", "ExceptionComment",
    "AiRecommendation", "ReviewerDecision", "VerifiedRecord", "AuditEvent",
]