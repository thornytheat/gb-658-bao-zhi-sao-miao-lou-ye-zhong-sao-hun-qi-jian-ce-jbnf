from app.modules.m2_issue.rules import expected_pages
from .attribution import CONFIRMED, REASSIGNED
from .selection import page_status, select_pages

COUNTABLE = (CONFIRMED, REASSIGNED)

RESCAN = "RESCAN"
WAIT = "WAIT"
MISSING = "MISSING"
HOLD = "HOLD"


def group_key(issue: str, edition: str) -> str:
    return f"{issue}|{edition}"


def parse_group(value: str) -> tuple[str, str]:
    parts = value.split("|")
    if len(parts) != 2 or not all(part.strip() for part in parts):
        raise ValueError("BAD_GROUP_KEY")
    return parts[0].strip(), parts[1].strip()


def split_groups(scans: list[dict]) -> tuple[dict[tuple[str, str], list[dict]], dict[tuple[str, str], dict[str, dict]]]:
    """已定归属的进入计页池；页眉看不清或待裁决的进入待定池，不参与计页。

    待裁决件的页可能属于原期或页眉指向的期，两期都要能看到这个候选，
    否则印务会在一期里误判为“无候选”而重复补扫。
    """
    countable: dict[tuple[str, str], list[dict]] = {}
    held: dict[tuple[str, str], dict[str, dict]] = {}
    for scan in scans:
        attribution = scan["attribution"]
        if attribution["status"] in COUNTABLE:
            countable.setdefault((attribution["issue"], scan["edition"]), []).append(scan)
            continue
        keys = {(scan["issue"], scan["edition"])}
        if attribution["candidate_issue"]:
            keys.add((attribution["candidate_issue"], scan["edition"]))
        for key in keys:
            held.setdefault(key, {})[scan["scan_key"]] = scan
    return countable, held


def _wait_reason(candidates: list[dict]) -> str:
    if any(scan["attribution"]["status"] == "UNKNOWN" for scan in candidates):
        return "CANDIDATE_HEADER_UNKNOWN"
    return "CANDIDATE_MIXED"


def _candidate_view(scan: dict) -> dict:
    return {
        "file_name": scan["file_name"],
        "line_no": scan["line_no"],
        "sha256": scan["sha256"],
        "header_issue": scan["header_issue"],
        "attribution": scan["attribution"]["status"],
        "candidate_issue": scan["attribution"]["candidate_issue"],
        "reason_code": scan["attribution"]["reason_code"],
    }


def _candidates(held: list[dict], page: str) -> list[dict]:
    return sorted((scan for scan in held if scan["page"] == page), key=lambda scan: (scan["line_no"], scan["file_name"]))


def build_group(issue: str, edition: str, scans: list[dict], held: list[dict], expected_count: int | None) -> dict:
    countable = [scan for scan in scans if scan["attribution"]["status"] in COUNTABLE]
    selected, superseded = select_pages(countable)
    status = page_status(selected, expected_count)

    missing = []
    for page in status["missing"]:
        if _candidates(held, page):
            continue
        missing.append({"issue": issue, "edition": edition, "page": page, "status": MISSING, "action": RESCAN, "reason_code": "NO_CANDIDATE", "candidates": []})

    held_pages = []
    pages = status["missing"] if expected_count is not None else sorted({scan["page"] for scan in held}, key=int)
    for page in pages:
        candidates = _candidates(held, page)
        if not candidates:
            continue
        held_pages.append({
            "issue": issue,
            "edition": edition,
            "page": page,
            "status": HOLD,
            "action": WAIT,
            "reason_code": _wait_reason(candidates),
            "candidates": [_candidate_view(scan) for scan in candidates],
        })

    pending = []
    extra_pages = sorted({scan["page"] for scan in held} - set(pages), key=int)
    for page in extra_pages:
        candidates = _candidates(held, page)
        if not candidates:
            continue
        pending.append({
            "issue": issue,
            "edition": edition,
            "page": page,
            "covered_by": selected[page]["file_name"] if page in selected else None,
            "reason_code": _wait_reason(candidates),
            "candidates": [_candidate_view(scan) for scan in candidates],
        })

    in_range = set(expected_pages(expected_count)) if expected_count else None
    covered_pages = [page for page in sorted(selected, key=int) if in_range is None or page in in_range]
    return {
        "issue": issue,
        "edition": edition,
        "group": group_key(issue, edition),
        "expected": expected_count,
        "expected_known": expected_count is not None,
        "covered": len(covered_pages),
        "coverage": f"{len(covered_pages) / expected_count:.3f}" if expected_count else None,
        "missing": missing,
        "held": held_pages,
        "pending": pending,
        "rescan": len(missing),
        "wait": len(held_pages),
        "selected": [{"page": page, "file_name": selected[page]["file_name"], "line_no": selected[page]["line_no"]} for page in sorted(selected, key=int)],
        "superseded": [{"page": scan["page"], "file_name": scan["file_name"], "line_no": scan["line_no"]} for scan in superseded],
        "unexpected": status["unexpected"],
        "balanced": expected_count is None or len(covered_pages) + len(missing) + len(held_pages) == expected_count,
    }


def build_listing(scans: list[dict], expected: dict[tuple[str, str], int | None]) -> dict:
    countable, held = split_groups(scans)
    keys = set(countable) | set(held)
    groups = [
        build_group(issue, edition, countable.get((issue, edition), []), list(held.get((issue, edition), {}).values()), expected.get((issue, edition)))
        for issue, edition in sorted(keys)
    ]
    totals = {
        "groups": len(groups),
        "expected": sum(group["expected"] or 0 for group in groups),
        "covered": sum(group["covered"] for group in groups),
        "rescan": sum(group["rescan"] for group in groups),
        # 待定按扫描件去重：同一件跨两期可见，只等一次裁决。
        "wait": len({candidate["sha256"] or candidate["file_name"] for group in groups for item in group["held"] for candidate in item["candidates"]}),
        "wait_pages": sum(group["wait"] for group in groups),
        "pending": sum(len(group["pending"]) for group in groups),
        "superseded": sum(len(group["superseded"]) for group in groups),
        "unexpected": sum(len(group["unexpected"]) for group in groups),
    }
    totals["balanced"] = all(group["balanced"] for group in groups)
    return {"groups": groups, "totals": totals}


def group_of(listing: dict, issue: str, edition: str) -> dict | None:
    for group in listing["groups"]:
        if group["issue"] == issue and group["edition"] == edition:
            return group
    return None
