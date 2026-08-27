# backend/tests/test_audit.py
import uuid

from sqlalchemy import text

from app.extensions import db
from app.models import AuditEvent
from app.services.audit import canonical_json, log_event, verify_chain


def test_canonical_json_is_order_independent():
    a = canonical_json({"b": 1, "a": 2, "nested": {"y": 1, "x": 2}})
    b = canonical_json({"a": 2, "nested": {"x": 2, "y": 1}, "b": 1})
    assert a == b
    assert a == '{"a":2,"b":1,"nested":{"x":2,"y":1}}'


def test_chain_appends_and_verifies(session):
    e1 = log_event(event_type="file_uploaded", entity_type="raw_file",
                   entity_id=uuid.uuid4(), actor_type="system",
                   after_value={"filename": "tape.csv"})
    e2 = log_event(event_type="record_imported", entity_type="raw_record",
                   entity_id=uuid.uuid4(), actor_type="system",
                   after_value={"rows": 1212})

    assert e2.prev_event_hash == e1.event_hash   # e2 links to e1
    assert e1.event_hash != e2.event_hash        # distinct events, distinct hashes

    ok, detail = verify_chain()
    assert ok, detail


def test_tampering_is_detected(session):
    log_event(event_type="validation_executed", entity_type="loan",
              entity_id=uuid.uuid4(), actor_type="system",
              after_value={"rules": 18})
    db.session.flush()

    # INSERT is permitted for the app role — appends are legal. But a forged
    # event whose hash doesn't match its contents is still caught, because the
    # chain math, not the grants, is what makes the log unforgeable.
    tip = db.session.execute(text(
        "SELECT event_hash FROM audit_events ORDER BY seq DESC LIMIT 1")).scalar()
    forged = AuditEvent(
        id=uuid.uuid4(), event_type="loan_approved", entity_type="loan",
        entity_id=uuid.uuid4(), actor_type="system",
        after_value={"status": "verified"},
        event_hash="deadbeef" * 8,
        prev_event_hash=tip)
    db.session.add(forged)
    db.session.flush()

    ok, detail = verify_chain()
    assert not ok
    assert detail["reason"] == "hash mismatch"