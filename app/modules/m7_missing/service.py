from hashlib import sha256

from app.errors import DomainError
from app.modules.m2_issue.rules import normalize_issue
from .attribution import normalize_scan
from .ledger import (
    apply_reassignment,
    canonical,
    current_reassignment,
    find_request,
    get_version,
    latest_version,
    overrides_for,
    save_listing,
)
from .listing import build_listing, group_of, parse_group

SAMPLE_SCANS = [
    {"issue": "2026-11-01", "edition": "01", "page": "001", "file_name": "scan_001.tif", "sha256": "a" * 64, "phash": "0f0f", "clarity": 90, "header_issue": "2026-11-01", "line_no": 2},
    {"issue": "2026-11-01", "edition": "01", "page": "002", "file_name": "scan_002_old.tif", "sha256": "b" * 64, "phash": "0f1f", "clarity": 71, "version": 1, "header_issue": "2026-11-01", "line_no": 3},
    {"issue": "2026-11-01", "edition": "01", "page": "002", "file_name": "scan_002_new.tif", "sha256": "c" * 64, "phash": "0f1e", "clarity": 94, "version": 2, "header_issue": "2026-11-01", "line_no": 4},
    {"issue": "2026-11-01", "edition": "01", "page": "004", "file_name": "scan_004.tif", "sha256": "d" * 64, "phash": "ffff", "clarity": 84, "header_issue": "2026-11-02", "line_no": 5},
    {"issue": "2026-11-01", "edition": "01", "page": "005", "file_name": "scan_005.tif", "sha256": "e" * 64, "phash": "0f0f", "clarity": 82, "header_issue": "", "line_no": 6},
    {"issue": "2026-11-02", "edition": "01", "page": "001", "file_name": "scan_copy.tif", "sha256": "a" * 64, "phash": "0f0f", "clarity": 82, "header_issue": "2026-11-02", "line_no": 7},
]

SAMPLE_EXPECTED = {"2026-11-01|01": 6, "2026-11-02|01": 2}


def scan_key(scan: dict) -> str:
    identity = {"file_name": scan["file_name"], "sha256": scan["sha256"], "issue": scan["issue"], "edition": scan["edition"], "page": scan["page"]}
    return sha256(canonical(identity).encode()).hexdigest()[:16]


def normalize_expected(payload: dict | None) -> dict[tuple[str, str], int | None]:
    expected: dict[tuple[str, str], int | None] = {}
    for key, value in (payload or {}).items():
        issue, edition = parse_group(str(key))
        try:
            issue = normalize_issue(issue)
        except (TypeError, ValueError) as exc:
            raise DomainError("BAD_FORMAT", f"期号不规范: {key}") from exc
        if value is None or str(value).strip() == "":
            expected[(issue, edition)] = None
            continue
        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            raise DomainError("BAD_FORMAT", f"应有页数不是整数: {key}") from exc
        if count < 1:
            raise DomainError("BAD_FORMAT", f"应有页数必须为正: {key}")
        expected[(issue, edition)] = count
    return expected


def normalize_scans(raw_scans, overrides: dict[str, str] | None = None) -> list[dict]:
    if not isinstance(raw_scans, list) or not raw_scans:
        raise DomainError("EMPTY_VALUE", "扫描件列表为空")
    overrides = overrides or {}
    scans = []
    for index, raw in enumerate(raw_scans, 1):
        provisional = normalize_scan(raw, index)
        key = scan_key(provisional)
        if key in overrides:
            provisional = normalize_scan(raw, index, confirmed_issue=overrides[key])
            provisional["scan_key"] = key
        else:
            provisional["scan_key"] = key
        scans.append(provisional)
    return scans


def _manifest_input(raw_scans: list[dict], expected: dict[tuple[str, str], int | None], scans: list[dict]) -> dict:
    return {
        "raw_scans": raw_scans,
        "expected": {f"{issue}|{edition}": count for (issue, edition), count in sorted(expected.items())},
        "overrides": {scan["scan_key"]: scan["attribution"]["issue"] for scan in scans if scan["attribution"]["status"] == "REASSIGNED"},
    }


def build(payload: dict | None = None) -> dict:
    """出具缺页清单；同一输入重复出单复用已有版本，不新增快照。"""
    payload = payload or {}
    if "expected" in payload:
        expected = normalize_expected(payload["expected"])
    elif "scans" in payload:
        # 自带清单却没给应有页数：不猜样例值，按“页数未知”出单。
        expected = {}
    else:
        expected = normalize_expected(SAMPLE_EXPECTED)
    raw_scans = payload["scans"] if "scans" in payload else SAMPLE_SCANS
    provisional = normalize_scans(raw_scans)
    overrides = overrides_for([scan["scan_key"] for scan in provisional])
    scans = normalize_scans(raw_scans, overrides)
    listing = build_listing(scans, expected)
    saved = save_listing(_manifest_input(raw_scans, expected, scans), listing)
    return {
        "version_id": saved["version_id"],
        "ordinal": saved["ordinal"],
        "reused": saved["reused"],
        "created_at": saved["created_at"],
        "listing": listing,
    }


def _select_scan(scans: list[dict], payload: dict) -> dict:
    wanted = str(payload.get("scan_key", "")).strip()
    file_name = str(payload.get("file_name", "")).strip()
    sha = str(payload.get("sha256", "")).strip().lower()
    if wanted:
        matches = [scan for scan in scans if scan["scan_key"] == wanted]
    elif file_name:
        matches = [scan for scan in scans if scan["file_name"] == file_name and (not sha or scan["sha256"] == sha)]
    else:
        raise DomainError("EMPTY_VALUE", "改期必须指定 scan_key 或 file_name")
    if not matches:
        raise DomainError("UNKNOWN_REFERENCE", "改期指向的扫描件不存在", status=404)
    if len(matches) > 1:
        raise DomainError("DUPLICATE_KEY", "改期指向的扫描件不唯一，请补 file_name 或 sha256")
    return matches[0]


def _evidence_issue(payload: dict, scan: dict) -> str:
    """补证页眉是改期的唯一依据；可读页眉不得被改成与之冲突的期。"""
    evidence = payload.get("evidence") or {}
    raw = evidence.get("header_issue", payload.get("confirmed_header_issue"))
    if raw is None or str(raw).strip() == "":
        raise DomainError("EMPTY_VALUE", "改期必须提供补证页眉")
    try:
        issue = normalize_issue(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise DomainError("BAD_FORMAT", "补证页眉期号不规范") from exc
    if scan["header_issue"] and not scan["header_bad"] and scan["header_issue"] != issue:
        raise DomainError("VALUE_CONFLICT", "补证页眉与原页眉冲突")
    return issue


def _request_key(payload: dict, scan: dict, to_issue: str) -> str:
    supplied = str(payload.get("request_key", "")).strip()
    if supplied:
        return supplied
    return sha256(canonical({"scan_key": scan["scan_key"], "to_issue": to_issue}).encode()).hexdigest()[:20]


def _reassign_response(record: dict, after: dict, before: dict, created: bool, ordinal: int | None = None) -> dict:
    from_after = group_of(after, record["from_issue"], record["edition"])
    to_after = group_of(after, record["to_issue"], record["edition"])
    from_before = group_of(before, record["from_issue"], record["edition"])
    to_before = group_of(before, record["to_issue"], record["edition"])
    recomputed = []
    for group, previous in ((from_after, from_before), (to_after, to_before)):
        if group is None:
            continue
        recomputed.append(
            {
                "issue": group["issue"],
                "edition": group["edition"],
                "expected": group["expected"],
                "before_rescan": [item["page"] for item in (previous or {}).get("missing", [])],
                "rescan": [item["page"] for item in group["missing"]],
                "wait": [item["page"] for item in group["held"]],
                "covered": group["covered"],
                "coverage": group["coverage"],
                "changed": (previous or {}).get("missing") != group["missing"] or (previous or {}).get("held") != group["held"],
            }
        )
    return {
        "created": created,
        "version_id": record["version_id"],
        "ordinal": ordinal,
        "reassignment": {
            "scan_key": record["scan_key"],
            "file_name": record["file_name"],
            "edition": record["edition"],
            "page": record["page"],
            "from_issue": record["from_issue"],
            "to_issue": record["to_issue"],
            "created_at": record["created_at"],
        },
        "recomputed": recomputed,
        "listing": after,
    }


def reassign(payload: dict | None = None, fault=None) -> dict:
    """补证确认改期：旧期与新期缺页同一次重算；中断则整体回滚保留旧版，重试不重复记改期。"""
    payload = payload or {}
    stored = latest_version()
    if "expected" in payload:
        expected = normalize_expected(payload["expected"])
    elif stored is not None:
        expected = normalize_expected(stored["input"]["expected"])
    else:
        expected = normalize_expected(SAMPLE_EXPECTED)
    raw_scans = payload.get("scans") or (stored["input"]["raw_scans"] if stored else SAMPLE_SCANS)

    provisional = normalize_scans(raw_scans)
    overrides = overrides_for([scan["scan_key"] for scan in provisional])
    scans = normalize_scans(raw_scans, overrides)
    scan = _select_scan(scans, payload)
    to_issue = _evidence_issue(payload, scan)
    current_issue = scan["attribution"]["issue"]
    request_key = _request_key(payload, scan, to_issue)

    # 幂等以“当前归属是否已是目标期”为准：中断回滚后重试会补记一次；
    # 已生效后的重复提交不再记改期。同一请求标识不得指向不同目标期。
    seen = find_request(request_key)
    if seen is not None and seen["to_issue"] != to_issue:
        raise DomainError("VALUE_CONFLICT", "同一请求标识指向了不同的目标期")
    current = current_reassignment(scan["scan_key"])

    before = build_listing(scans, expected)
    if to_issue == current_issue:
        if current is not None and current["to_issue"] == to_issue:
            return _reassign_response(current, before, before, created=False, ordinal=current.get("ordinal"))
        raise DomainError("VALUE_CONFLICT", "补证期号与原期号相同，无需改期")

    new_overrides = dict(overrides)
    new_overrides[scan["scan_key"]] = to_issue
    updated = normalize_scans(raw_scans, new_overrides)
    after = build_listing(updated, expected)

    record = {
        "request_key": request_key,
        "scan_key": scan["scan_key"],
        "from_issue": current_issue,
        "edition": scan["edition"],
        "page": scan["page"],
        "file_name": scan["file_name"],
        "to_issue": to_issue,
    }
    saved = apply_reassignment(record, _manifest_input(raw_scans, expected, updated), after, fault=fault)
    if not saved["created"]:
        return _reassign_response(saved["record"], after, before, created=False, ordinal=saved.get("ordinal"))
    return _reassign_response(saved["record"], after, before, created=True, ordinal=saved["ordinal"])


def history(target: str | None = None) -> dict:
    version = get_version(target) if target else latest_version()
    if version is None:
        raise DomainError("UNKNOWN_REFERENCE", "清单版本不存在", status=404)
    return version


def missing_rows(listing: dict) -> list[dict]:
    """一份导出同时给出“必须补扫”和“先别扫、等裁决”的页，印务不必猜。"""
    rows = []
    for group in listing["groups"]:
        for item in [*group["missing"], *group["held"]]:
            rows.append(
                {
                    "business_key": f"{item['issue']}|{item['edition']}|{item['page']}",
                    "status": item["status"],
                    "action": item["action"],
                    "reason_code": item["reason_code"],
                    "candidates": " ".join(candidate["file_name"] for candidate in item["candidates"]),
                    "line_no": item["candidates"][0]["line_no"] if item["candidates"] else "",
                    "raw_value": item["page"],
                }
            )
    return rows


def demo() -> dict:
    return build()
