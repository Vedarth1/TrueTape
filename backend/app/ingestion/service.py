from __future__ import annotations
from app.ingestion.normalizer import normalize_file
from app.services.audit import log_event

import csv
import hashlib
import os
import threading
import uuid
from pathlib import Path

from flask import current_app

from app.extensions import db
from app.models import RawFile, RawRecord

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
CHUNK = 500

# file_kind → source_system. This is the single place the two stay in lockstep,
# and it must match the trust_config.json keys exactly, or survivorship finds no
# per-field trust for a source. The manifest CSV carries no source_system column,
# so this map is the ONLY place its source is assigned.
FILE_KIND_SOURCE = {
    "loan_tape": "OriginationCore",
    "servicer_update": "ServicerFeed",
    "document_manifest": "DocumentManifest",
}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_existing(file_hash: str):
    """A prior upload of the same bytes that did NOT fail. A failed import must
    not block a re-upload, so those are excluded."""
    return db.session.execute(
        db.select(RawFile)
        .filter(RawFile.file_hash == file_hash, RawFile.status != "failed")
        .order_by(RawFile.uploaded_at.desc())
    ).scalars().first()


def store_bytes(raw_file_id, filename: str, data: bytes) -> str:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = os.path.basename(filename).replace("/", "_")
    path = UPLOAD_DIR / f"{raw_file_id}__{safe}"
    path.write_bytes(data)
    return str(path)


def _read_rows(storage_path: str):
    """Yield (row_number, byte-faithful dict, parse_error|None).
    utf-8-sig strips a leading BOM if present. Only *structural* problems are
    parse errors here (ragged rows); value-level messiness is the normalizer's job."""
    with open(storage_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = list(reader.fieldnames or [])
        for i, row in enumerate(reader, start=1):
            err = None
            extra = row.pop(None, None)          # values beyond the header
            if extra:
                err = f"{len(extra)} extra column(s) beyond header"
            missing = [k for k in headers if row.get(k) is None]
            if missing:
                err = (err + "; " if err else "") + f"missing column(s): {','.join(missing)}"
            yield i, {k: row.get(k) for k in headers}, err


def spawn_processing(raw_file_id) -> None:
    app = current_app._get_current_object()
    threading.Thread(target=_process_file, args=(app, raw_file_id), daemon=True).start()


def _process_file(app, raw_file_id) -> None:
    """Runs in its own thread → its own app context and (thread-local) session.
    Reads the file off disk, so nothing crosses the thread boundary but an id."""
    with app.app_context():
        try:
            rf = db.session.get(RawFile, raw_file_id)
            if rf is None:
                return
            batch, total, failed = [], 0, 0
            for row_number, payload, err in _read_rows(rf.storage_path):
                total += 1
                if err:
                    failed += 1
                batch.append({
                    "id": uuid.uuid4(), "raw_file_id": raw_file_id,
                    "row_number": row_number, "raw_payload": payload, "parse_error": err,
                })
                if len(batch) >= CHUNK:
                    db.session.bulk_insert_mappings(RawRecord, batch)
                    rf.row_count, rf.parsed_count, rf.failed_count = total, total - failed, failed
                    db.session.commit()          # progress becomes visible to pollers
                    batch.clear()
            if batch:
                db.session.bulk_insert_mappings(RawRecord, batch)
            rf.row_count, rf.parsed_count, rf.failed_count = total, total - failed, failed
            
            # ---- Stage 2: normalize parsed rows -> canonical loan_records ----
            norm = normalize_file(raw_file_id)   # builds loans/loan_records/exceptions; no commit

            # ---- Stage 1 audit: one record_imported per file (system actor) ----
            log_event(
                event_type="record_imported",
                entity_type="raw_file",
                entity_id=raw_file_id,
                actor_id=None,
                actor_type="system",
                after_value={
                    "row_count": rf.row_count,
                    "parsed_count": rf.parsed_count,
                    "failed_count": rf.failed_count,
                    "loan_records_created": norm["loan_records_created"],
                    "quarantined_count": norm["quarantined_count"],
                },
            )
            rf.status = "completed"
            db.session.commit()   # normalize work + audit event + status: one atomic commit
        except Exception as exc:  # noqa: BLE001 - worker must never crash silently
            db.session.rollback()
            rf = db.session.get(RawFile, raw_file_id)
            if rf is not None:
                rf.status = "failed"
                db.session.commit()
            current_app.logger.exception("ingestion failed for %s: %s", raw_file_id, exc)
        finally:
            db.session.remove()                  # return the connection to the pool