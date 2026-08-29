# backend/app/api/pipeline.py
"""Pipeline trigger for the operator dashboard.

The upload worker handles stages 1-2 (parse, normalise) per file. Stages 3-5
(validation, canonical blend, clustering) run here: one synchronous call,
one commit, per-stage results back to the UI. Typical runtime on the seed
dataset is a few seconds.

    POST /api/pipeline/run
"""
from __future__ import annotations

import sqlalchemy as sa
from flask import Blueprint, jsonify
from flask_jwt_extended import get_jwt_identity

from app.auth.decorators import role_required
from app.extensions import db
from app.services.audit import log_event
from app.services.canonical import build_canonical
from app.services.clustering import assign_rule_clusters
from app.validation.runner import (
    run_cross_source_validation,
    run_dataset_validation,
    run_row_validation,
)

bp = Blueprint("pipeline", __name__, url_prefix="/api/pipeline")


@bp.post("/run")
@role_required("operator", "reviewer")
def run_pipeline():
    """Run validation + canonical + clustering stages over imported data.

    Idempotent: stages with existing results report skipped unless
    {"force": true} is passed in the body. Force re-runs refuse if a reviewer
    has already touched a validation_failure exception (the runners' own
    guard) — that surfaces as a 409 with the guard's message.
    """
    from flask import request

    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))

    stages = [
        ("row_validation", run_row_validation),
        ("dataset_validation", run_dataset_validation),
        ("cross_source_validation", run_cross_source_validation),
        ("canonical_blend", build_canonical),
        ("cluster_grouping", assign_rule_clusters),
    ]

    results = {}
    for name, fn in stages:
        try:
            results[name] = fn(force=force) if name.endswith("validation") else fn()
        except RuntimeError as exc:
            # The runners' guards (no seed rules / reviewer-touched rows on
            # force) come back as 409s so the UI can explain, not just fail.
            db.session.rollback()
            return jsonify({"error": {"code": "PIPELINE_BLOCKED",
                                      "message": str(exc),
                                      "details": {"stage": name,
                                                  "stages_completed": list(results)}}}), 409

    reviewer_id = get_jwt_identity()
    log_event(
        event_type="validation_executed",
        entity_type="pipeline",
        entity_id=None,
        actor_id=reviewer_id if reviewer_id else None,
        actor_type="human" if reviewer_id else "system",
        after_value={"trigger": "api", "force": force,
                     "stages": list(results)},
        reason="pipeline run from dashboard",
    )
    db.session.commit()

    return jsonify({"ok": True, "stages": results}), 200
