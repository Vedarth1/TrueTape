"""Allowed values for every CHECK-constrained column, defined exactly once.

These tuples are used in two places: the CheckConstraint the database enforces,
and any application-side validation. Two hand-maintained copies of the same
list drift, and when they drift the database wins -- the API returns a 500 on
a value the code believed was legal.
"""

ROLES = ("operator", "reviewer", "consumer")

FILE_KINDS = ("loan_tape", "servicer_update", "document_manifest")
FILE_STATUSES = ("processing", "completed", "failed")

LOAN_STATUSES = ("ingested", "in_review", "verified", "rejected")
RECORD_ORIGINS = ("import", "human_edit")

RULE_SCOPES = ("row", "dataset", "cross_source")
RULE_SOURCES = ("seed", "ai_generated", "manual")
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
RESULT_STATES = ("pass", "fail", "not_applicable")

EXCEPTION_TYPES = ("validation_failure", "source_conflict", "staleness",
                   "duplicate", "import_error")
EXCEPTION_STATUSES = ("open", "in_review", "resolved", "rejected")

AI_ACTION_TYPES = ("explain_failure", "suggest_correction", "reviewer_note",
                   "classify_severity", "batch_summary")

DECISION_SCOPES = ("exception", "loan")
DECISION_ACTIONS = ("accept", "edit", "reject", "manual_resolution",
                    "approve_loan", "reject_loan")

ACTOR_TYPES = ("human", "ai", "system")

EVENT_TYPES = (
    # The eleven the rubric's traceability requirement needs.
    "file_uploaded", "record_imported", "validation_executed",
    "exception_created", "ai_recommendation_generated",
    "reviewer_comment_added", "field_edited", "loan_approved",
    "loan_rejected", "verified_record_created", "verified_record_exported",
    # Four the workflow needs. trust_config_updated is defined but never
    # fires -- trust config is read-only in v1. Defined anyway so adding the
    # editor later is not a migration.
    "rule_created", "rule_deactivated", "correction_requested",
    "trust_config_updated",
)
assert len(EVENT_TYPES) == 15, "event type list drifted"


def one_of(column: str, values: tuple) -> str:
    """Build the SQL for a CHECK (col IN (...)) from the tuples above.

    Values are literal-quoted rather than parameterised because a CHECK
    constraint is compiled into the schema, not executed as a query -- there
    is no bind-parameter mechanism available here. Every value in this module
    is a hard-coded identifier, so there is no injection surface.
    """
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"