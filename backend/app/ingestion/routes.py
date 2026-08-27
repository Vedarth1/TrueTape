from __future__ import annotations

import uuid

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity

from app.auth.decorators import role_required
from app.extensions import db
from app.ingestion.service import (
    FILE_KIND_SOURCE, find_existing, sha256_hex, spawn_processing, store_bytes,
)
from app.models import RawFile, RawRecord
from app.services.audit import log_event

bp = Blueprint("files", __name__, url_prefix="/api/files")


def _err(code, message, status, **details):
    return jsonify({"error": {"code": code, "message": message, "details": details}}), status


def _summary(rf: RawFile) -> dict:
    total, parsed = rf.row_count or 0, rf.parsed_count or 0
    pct = 100 if rf.status == "completed" else (int(parsed * 100 / total) if total else 0)
    return {
        "id": str(rf.id), "filename": rf.filename, "file_kind": rf.file_kind,
        "source_system": rf.source_system, "status": rf.status,
        "row_count": rf.row_count, "parsed_count": rf.parsed_count,
        "failed_count": rf.failed_count, "progress_pct": pct,
        "uploaded_at": rf.uploaded_at.isoformat() if rf.uploaded_at else None,
    }


@bp.post("")
@role_required("operator")
def upload_file():
    upload = request.files.get("file")
    file_kind = (request.form.get("file_kind") or "").strip()
    if upload is None or not upload.filename:
        return _err("NO_FILE", "attach a CSV under the 'file' field", 400)
    if file_kind not in FILE_KIND_SOURCE:
        return _err("BAD_FILE_KIND", "file_kind must be loan_tape, servicer_update, "
                    "or document_manifest", 400, allowed=list(FILE_KIND_SOURCE))

    data = upload.read()
    if not data:
        return _err("EMPTY_FILE", "the uploaded file is empty", 400)

    file_hash = sha256_hex(data)
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    if not force:
        dup = find_existing(file_hash)
        if dup is not None:
            return _err("DUPLICATE_FILE",
                        "these exact bytes were already uploaded; re-send with "
                        "?force=true to ingest again", 409,
                        existing_file_id=str(dup.id), existing_status=dup.status,
                        uploaded_at=dup.uploaded_at.isoformat() if dup.uploaded_at else None)

    actor = uuid.UUID(get_jwt_identity())
    raw_file_id = uuid.uuid4()
    storage_path = store_bytes(raw_file_id, upload.filename, data)
    rf = RawFile(
        id=raw_file_id, filename=upload.filename, file_kind=file_kind,
        source_system=FILE_KIND_SOURCE[file_kind], file_hash=file_hash,
        storage_path=storage_path, uploaded_by=actor, status="processing",
    )
    db.session.add(rf)
    log_event(
        event_type="file_uploaded", entity_type="raw_file", entity_id=raw_file_id,
        actor_id=actor, actor_type="human",
        after_value={"filename": upload.filename, "file_kind": file_kind,
                     "source_system": rf.source_system, "file_hash": file_hash,
                     "forced": force},
    )
    db.session.commit()          # raw_file + audit row are atomic
    spawn_processing(raw_file_id)  # only after the row is committed & visible
    return jsonify(_summary(rf)), 202


@bp.get("")
@role_required("operator", "reviewer")
def list_files():
    rows = db.session.execute(
        db.select(RawFile).order_by(RawFile.uploaded_at.desc())).scalars().all()
    return jsonify({"files": [_summary(r) for r in rows]})


@bp.get("/<uuid:file_id>")
@role_required("operator", "reviewer")
def get_file(file_id):
    rf = db.session.get(RawFile, file_id)
    if rf is None:
        return _err("NOT_FOUND", "no such file", 404)
    summary = _summary(rf)
    failed = db.session.execute(
        db.select(RawRecord.row_number, RawRecord.parse_error)
        .filter(RawRecord.raw_file_id == file_id, RawRecord.parse_error.isnot(None))
        .order_by(RawRecord.row_number).limit(200)).all()
    summary["failed_rows"] = [{"row_number": r[0], "parse_error": r[1]} for r in failed]
    summary["quarantined_rows"] = []  # filled in the normalization block (rows with no loan_id)
    return jsonify(summary)