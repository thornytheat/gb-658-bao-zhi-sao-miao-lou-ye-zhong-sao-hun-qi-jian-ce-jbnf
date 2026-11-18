import csv
import io

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from app.web import page
from .service import build, history, missing_rows, reassign

router = APIRouter()
ROWS = [
    {"key": "2026-11-01|01|004", "status": "待裁决", "value": "WAIT"},
    {"key": "2026-11-01|01|005", "status": "待裁决", "value": "WAIT"},
    {"key": "2026-11-02|01|002", "status": "缺页", "value": "RESCAN"},
]
FIELDS = ["business_key", "status", "action", "reason_code", "candidates", "line_no", "raw_value"]


@router.get("/missing")
def view(request: Request):
    return page(request, "m7.html", "缺页清单", "/api/missing/list", ROWS)


@router.post("/api/missing/list")
def list_missing(payload: dict | None = None):
    return build(payload)


@router.post("/api/missing/reassign")
def confirm_reassign(payload: dict | None = None):
    return reassign(payload)


@router.get("/api/missing/history")
def version_history(version_id: str | None = None):
    stored = history(version_id)
    return {**stored, "rows": missing_rows(stored["listing"])}


@router.get("/api/export/missing.csv")
def export_missing(version_id: str | None = None):
    stored = history(version_id)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(missing_rows(stored["listing"]))
    return PlainTextResponse(
        output.getvalue(),
        media_type="text/csv",
        headers={"content-disposition": "attachment; filename=missing.csv"},
    )
