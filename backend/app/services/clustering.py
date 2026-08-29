# backend/app/services/clustering.py
"""Deterministic exception clustering.

Groups exceptions by their root-cause signal so 40 identical failures resolve
as one decision instead of forty. The pass is idempotent: a rule_code cluster
is created once, re-runs refresh member_count, and exceptions that are already
clustered (or already resolved) are left alone.

Strategy: group by rule_code -- every exception a rule produced shares a root
cause by construction, and the cluster's root_cause_signal names the rule so
the UI can explain WHY these rows sit together. import_error quarantine rows
(there is no rule) cluster under their own label.
"""
from __future__ import annotations

import sqlalchemy as sa

from app.extensions import db
from app.models import ExceptionCluster, ExceptionRecord, ValidationRule


def assign_rule_clusters() -> dict:
    """Cluster open exceptions by rule_code. Idempotent. Does NOT commit."""
    # Open, unclustered exceptions with their grouping key. Group by
    # (rule_id, exception_type): rule-backed exceptions share their rule's
    # cluster, but rule-less types must NOT merge -- source_conflicts (87) and
    # import_error quarantines (4) are unrelated root causes that both happen
    # to have rule_id NULL.
    rows = db.session.execute(
        db.select(
            ExceptionRecord.rule_id,
            ExceptionRecord.exception_type,
            sa.func.count(ExceptionRecord.id),
        )
        .filter(ExceptionRecord.status == "open",
                ExceptionRecord.cluster_id.is_(None))
        .group_by(ExceptionRecord.rule_id, ExceptionRecord.exception_type)
    ).all()

    clusters_created, exceptions_assigned = 0, 0

    _RULELESS_LABELS = {
        "source_conflict": "source conflicts (OriginationCore vs ServicerFeed)",
        "import_error": "import errors (quarantined rows)",
    }

    for rule_id, exc_type, n in rows:
        if rule_id is not None:
            rule = db.session.get(ValidationRule, rule_id)
            label = f"{rule.rule_code} v{rule.version}" if rule else "unmapped rule"
            signal = {"rule_code": rule.rule_code if rule else None,
                      "strategy": "rule_code"}
        else:
            label = _RULELESS_LABELS.get(exc_type, f"unclustered {exc_type}")
            signal = {"exception_type": exc_type, "strategy": "rule_code"}

        cluster = db.session.execute(
            db.select(ExceptionCluster).filter(
                ExceptionCluster.cluster_label == label)
        ).scalars().first()
        if cluster is None:
            cluster = ExceptionCluster(
                cluster_label=label, root_cause_signal=signal, member_count=0)
            db.session.add(cluster)
            db.session.flush()
            clusters_created += 1

        key_filter = (ExceptionRecord.rule_id == rule_id if rule_id is not None
                      else (ExceptionRecord.rule_id.is_(None) &
                            (ExceptionRecord.exception_type == exc_type)))
        assigned = db.session.execute(
            sa.update(ExceptionRecord)
            .where(ExceptionRecord.status == "open",
                   ExceptionRecord.cluster_id.is_(None),
                   key_filter)
            .values(cluster_id=cluster.id)
        )
        exceptions_assigned += assigned.rowcount or 0

    # Refresh the denormalised counters for every cluster from the source of
    # truth -- open members only, so resolving rows shrinks the cluster the
    # UI sees without needing this pass to run first.
    counts = dict(db.session.execute(
        db.select(ExceptionRecord.cluster_id,
                  sa.func.count(ExceptionRecord.id))
        .filter(ExceptionRecord.status == "open",
                ExceptionRecord.cluster_id.isnot(None))
        .group_by(ExceptionRecord.cluster_id)
    ).all())
    for cluster in db.session.execute(db.select(ExceptionCluster)).scalars():
        cluster.member_count = counts.get(cluster.id, 0)

    return {"clusters_created": clusters_created,
            "exceptions_assigned": exceptions_assigned,
            "open_by_cluster": {str(k): v for k, v in counts.items()}}
