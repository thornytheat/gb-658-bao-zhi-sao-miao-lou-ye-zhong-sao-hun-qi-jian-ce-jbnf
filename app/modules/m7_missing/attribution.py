from app.errors import DomainError
from app.modules.m2_issue.rules import normalize_edition, normalize_issue, normalize_page
from app.modules.m5_mixed.evidence import header_match

CONFIRMED = "CONFIRMED"
REASSIGNED = "REASSIGNED"
PENDING = "PENDING"
UNKNOWN = "UNKNOWN"

RESOLVED = (CONFIRMED, REASSIGNED)


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _integer(value, field: str, index: int) -> int:
    if value is None or _text(value) == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DomainError("BAD_FORMAT", f"扫描件第 {index} 条的 {field} 不是整数") from exc


def normalize_header(value) -> tuple[str | None, bool]:
    """页眉走与期号相同的规范化；规范化失败记为坏页眉，绝不当成空串。"""
    text = _text(value)
    if not text:
        return None, False
    try:
        return normalize_issue(text), False
    except (TypeError, ValueError):
        return None, True


def attribute(row: dict, confirmed_issue: str | None = None) -> dict:
    """补证确认优先；页眉为空或不可辨一律未知，交人工，不能算一致。"""
    if confirmed_issue:
        return {"status": REASSIGNED, "issue": confirmed_issue, "candidate_issue": confirmed_issue, "reason_code": "REASSIGNED"}
    if row["header_bad"]:
        return {"status": UNKNOWN, "issue": row["issue"], "candidate_issue": None, "reason_code": "BAD_HEADER"}
    if header_match(row["issue"], row["header_issue"]) is None:
        return {"status": UNKNOWN, "issue": row["issue"], "candidate_issue": None, "reason_code": "HEADER_UNKNOWN"}
    if row["header_issue"] == row["issue"]:
        return {"status": CONFIRMED, "issue": row["issue"], "candidate_issue": None, "reason_code": "HEADER_MATCH"}
    return {"status": PENDING, "issue": row["issue"], "candidate_issue": row["header_issue"], "reason_code": "MIXED_ISSUE"}


def normalize_scan(raw: dict, index: int, confirmed_issue: str | None = None) -> dict:
    if any(not _text(raw.get(key)) for key in ("issue", "edition", "page")):
        raise DomainError("EMPTY_VALUE", f"扫描件第 {index} 条缺少期号、版次或页码")
    try:
        issue = normalize_issue(_text(raw["issue"]))
        edition = normalize_edition(_text(raw["edition"]))
        page = normalize_page(_text(raw["page"]))
    except (TypeError, ValueError) as exc:
        raise DomainError("BAD_FORMAT", f"扫描件第 {index} 条字段不规范: {exc}") from exc
    header_issue, header_bad = normalize_header(raw.get("header_issue"))
    row = {
        "issue": issue,
        "edition": edition,
        "page": page,
        "file_name": _text(raw.get("file_name")),
        "sha256": _text(raw.get("sha256")).lower(),
        "phash": _text(raw.get("phash")),
        "clarity": _integer(raw.get("clarity"), "clarity", index),
        "version": _integer(raw.get("version"), "version", index),
        "captured_at": _text(raw.get("captured_at")),
        "header_issue": header_issue,
        "header_bad": header_bad,
        "line_no": _integer(raw.get("line_no", index), "line_no", index) or index,
    }
    row["attribution"] = attribute(row, confirmed_issue)
    return row
