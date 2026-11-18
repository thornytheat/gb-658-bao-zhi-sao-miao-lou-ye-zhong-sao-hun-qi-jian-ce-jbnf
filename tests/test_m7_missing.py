import pytest

import app.modules.m7_missing.ledger as ledger
from app.errors import DomainError
from app.modules.m7_missing.attribution import normalize_scan
from app.modules.m7_missing.listing import build_listing
from app.modules.m7_missing.selection import page_status, select_pages
from app.modules.m7_missing.service import build, history, missing_rows, reassign

SCANS = [
    {"issue": "2026-11-01", "edition": "01", "page": "001", "file_name": "p1.tif", "sha256": "a" * 64, "phash": "0f0f", "clarity": 90, "header_issue": "2026-11-01", "line_no": 2},
    {"issue": "2026-11-01", "edition": "01", "page": "002", "file_name": "p2_old.tif", "sha256": "b" * 64, "phash": "0f1f", "clarity": 71, "version": 1, "header_issue": "2026-11-01", "line_no": 3},
    {"issue": "2026-11-01", "edition": "01", "page": "002", "file_name": "p2_new.tif", "sha256": "c" * 64, "phash": "0f1e", "clarity": 94, "version": 2, "header_issue": "2026-11-01", "line_no": 4},
    {"issue": "2026-11-01", "edition": "01", "page": "003", "file_name": "p3_blur.tif", "sha256": "d" * 64, "phash": "ffff", "clarity": 84, "header_issue": "2026-11-02", "line_no": 5},
    {"issue": "2026-11-01", "edition": "01", "page": "004", "file_name": "p4_nohdr.tif", "sha256": "e" * 64, "phash": "0f0f", "clarity": 82, "header_issue": "", "line_no": 6},
    {"issue": "2026-11-02", "edition": "01", "page": "001", "file_name": "q1.tif", "sha256": "a" * 64, "phash": "0f0f", "clarity": 82, "header_issue": "2026-11-02", "line_no": 7},
]
EXPECTED = {"2026-11-01|01": 5, "2026-11-02|01": 3}


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DATABASE_PATH", tmp_path / "m7.db")


def listing_of(result):
    return build_listing(normalize_scans_raw(result), {("2026-11-01", "01"): 5, ("2026-11-02", "01"): 3})


def normalize_scans_raw(rows):
    from app.modules.m7_missing.service import normalize_scans

    return normalize_scans(rows)


def group(listing, issue, edition="01"):
    return next(item for item in listing["groups"] if item["issue"] == issue and item["edition"] == edition)


# --- 归属：页眉看不清绝不能当一致 --------------------------------------


def test_blank_header_is_unknown_not_confirmed():
    scan = normalize_scans_raw([SCANS[4]])[0]
    assert scan["attribution"]["status"] == "UNKNOWN" and scan["attribution"]["reason_code"] == "HEADER_UNKNOWN"


def test_unreadable_header_is_unknown():
    bad = {"issue": "2026-11-01", "edition": "01", "page": "001", "header_issue": "???"}
    scan = normalize_scans_raw([bad])[0]
    assert scan["attribution"]["status"] == "UNKNOWN" and scan["attribution"]["reason_code"] == "BAD_HEADER"


def test_conflicting_header_stays_pending_with_candidate():
    scan = normalize_scans_raw([SCANS[3]])[0]
    assert scan["attribution"]["status"] == "PENDING"
    assert scan["attribution"]["candidate_issue"] == "2026-11-02"


# --- 列单：印务一眼分清“必须补扫”和“先别扫” ----------------------------


def test_clean_missing_page_is_rescan_task():
    result = listing_of(SCANS)
    pages = {item["page"]: item for item in group(result, "2026-11-01")["missing"]}
    assert pages["005"]["status"] == "MISSING" and pages["005"]["action"] == "RESCAN"
    assert pages["005"]["reason_code"] == "NO_CANDIDATE"


def test_blurry_rescan_page_is_held_not_counted():
    result = listing_of(SCANS)
    held = {item["page"]: item for item in group(result, "2026-11-01")["held"]}
    assert held["003"]["action"] == "WAIT" and held["003"]["reason_code"] == "CANDIDATE_MIXED"
    assert held["004"]["reason_code"] == "CANDIDATE_HEADER_UNKNOWN"
    assert [item["page"] for item in group(result, "2026-11-01")["missing"]] == ["005"]
    # 待定件跨两期可见，但按扫描件去重，印务只需等一次裁决。
    assert result["totals"]["wait"] == 2 and result["totals"]["wait_pages"] == 3
    assert result["totals"]["rescan"] == 2  # 11-01 第 005 页 + 11-02 第 002 页


def test_held_candidate_keeps_evidence_fields():
    held = {item["page"]: item for item in group(listing_of(SCANS), "2026-11-01")["held"]}
    candidate = held["003"]["candidates"][0]
    assert candidate["file_name"] == "p3_blur.tif" and candidate["line_no"] == 5
    assert candidate["header_issue"] == "2026-11-02" and candidate["sha256"] == "d" * 64


def test_mixed_candidate_visible_to_both_issues():
    result = listing_of(SCANS)
    assert "003" in {item["page"] for item in group(result, "2026-11-01")["held"]}
    assert "003" in {item["page"] for item in group(result, "2026-11-02")["held"]}


def test_held_page_never_marked_covered():
    result = listing_of(SCANS)
    first = group(result, "2026-11-01")
    assert first["covered"] == 2  # 只有 001、002；003/004 待定不计
    assert not {item["page"] for item in first["held"]} & {item["page"] for item in first["selected"]}
    assert first["balanced"]


def test_missing_rows_export_one_row_per_page():
    rows = missing_rows(listing_of(SCANS))
    assert {row["business_key"] for row in rows} == {
        "2026-11-01|01|003",
        "2026-11-01|01|004",
        "2026-11-01|01|005",
        "2026-11-02|01|002",
        "2026-11-02|01|003",
    }
    actions = {row["business_key"]: row["action"] for row in rows}
    assert actions["2026-11-01|01|005"] == "RESCAN"
    assert actions["2026-11-01|01|003"] == "WAIT"


# --- 选片：混期裁决后的选片直接决定缺页 --------------------------------


def test_newer_version_wins_over_clearer_stale_scan():
    scans = normalize_scans_raw(SCANS)
    selected, superseded = select_pages([scan for scan in scans if scan["page"] == "002"])
    assert selected["002"]["file_name"] == "p2_new.tif"
    assert [scan["file_name"] for scan in superseded] == ["p2_old.tif"]


def test_selection_is_order_independent():
    scans = normalize_scans_raw(SCANS)
    forward, _ = select_pages(scans)
    backward, _ = select_pages(list(reversed(scans)))
    assert {page: scan["file_name"] for page, scan in forward.items()} == {page: scan["file_name"] for page, scan in backward.items()}


def test_out_of_range_page_is_unexpected_not_missing():
    status = page_status({"001": {}, "009": {}}, 2)
    assert status["unexpected"] == ["009"] and "009" not in status["missing"]


# --- 改期：两期同算、回滚留旧版、重试不重复 ----------------------------


def test_build_is_idempotent_for_same_input():
    first = build({"scans": SCANS, "expected": EXPECTED})
    second = build({"scans": SCANS, "expected": EXPECTED})
    assert first["version_id"] == second["version_id"]
    assert second["reused"] is True and second["ordinal"] == first["ordinal"]


def test_reassign_recomputes_both_issues():
    build({"scans": SCANS, "expected": EXPECTED})
    result = reassign({"file_name": "p3_blur.tif", "evidence": {"header_issue": "2026-11-02"}})
    assert result["created"] is True
    by_issue = {item["issue"]: item for item in result["recomputed"]}
    assert by_issue["2026-11-01"]["before_rescan"] == ["005"]
    assert "003" in by_issue["2026-11-01"]["rescan"]
    # 改期后新期第 003 页由缺页变为已有候选，两期都要重算。
    assert "003" not in by_issue["2026-11-02"]["rescan"]
    assert "003" in {item["page"] for item in group(result["listing"], "2026-11-02")["selected"]}
    assert by_issue["2026-11-02"]["changed"] is True


def test_reassign_moves_page_coverage_across_issues():
    build({"scans": SCANS, "expected": {"2026-11-01|01": 5, "2026-11-02|01": 3}})
    result = reassign({"file_name": "p3_blur.tif", "evidence": {"header_issue": "2026-11-02"}})
    source = group(result["listing"], "2026-11-01")
    target = group(result["listing"], "2026-11-02")
    assert "003" in {item["page"] for item in source["missing"]}
    assert "003" in {item["page"] for item in target["selected"]} or "003" in {item["page"] for item in target["selected"]}


def test_reassign_requires_evidence_header():
    build({"scans": SCANS, "expected": EXPECTED})
    with pytest.raises(DomainError) as caught:
        reassign({"file_name": "p3_blur.tif", "evidence": {}})
    assert caught.value.code == "EMPTY_VALUE"


def test_reassign_rejects_header_contradicting_evidence():
    build({"scans": SCANS, "expected": EXPECTED})
    with pytest.raises(DomainError) as caught:
        reassign({"file_name": "p3_blur.tif", "evidence": {"header_issue": "2026-11-09"}})
    assert caught.value.code == "VALUE_CONFLICT"


def test_reassign_unknown_scan_reported():
    build({"scans": SCANS, "expected": EXPECTED})
    with pytest.raises(DomainError) as caught:
        reassign({"file_name": "ghost.tif", "evidence": {"header_issue": "2026-11-02"}})
    assert caught.value.code == "UNKNOWN_REFERENCE"


def test_interrupted_reassign_keeps_old_listing():
    first = build({"scans": SCANS, "expected": EXPECTED})

    def fault(db):
        raise RuntimeError("write interrupted")

    with pytest.raises(RuntimeError):
        reassign({"file_name": "p3_blur.tif", "evidence": {"header_issue": "2026-11-02"}}, fault=fault)
    latest = history()
    assert latest["version_id"] == first["version_id"] and latest["ordinal"] == first["ordinal"]
    assert ledger.reassignment_count() == 0


def test_retry_after_interruption_records_once():
    build({"scans": SCANS, "expected": EXPECTED})

    def fault(db):
        raise RuntimeError("write interrupted")

    with pytest.raises(RuntimeError):
        reassign({"file_name": "p3_blur.tif", "evidence": {"header_issue": "2026-11-02"}}, fault=fault)
    retry = reassign({"file_name": "p3_blur.tif", "evidence": {"header_issue": "2026-11-02"}})
    assert retry["created"] is True and ledger.reassignment_count() == 1
    latest = history()
    assert latest["input"]["overrides"] == {retry["reassignment"]["scan_key"]: "2026-11-02"}


def test_repeated_reassign_is_not_recorded_twice():
    build({"scans": SCANS, "expected": EXPECTED})
    first = reassign({"file_name": "p3_blur.tif", "evidence": {"header_issue": "2026-11-02"}})
    second = reassign({"file_name": "p3_blur.tif", "evidence": {"header_issue": "2026-11-02"}})
    assert first["created"] is True and second["created"] is False
    assert second["version_id"] == first["version_id"] and second["ordinal"] == first["ordinal"]
    assert ledger.reassignment_count() == 1


def test_same_request_key_retry_is_idempotent():
    build({"scans": SCANS, "expected": EXPECTED})
    payload = {"file_name": "p3_blur.tif", "evidence": {"header_issue": "2026-11-02"}, "request_key": "manual-42"}
    assert reassign(payload)["created"] is True
    assert reassign(payload)["created"] is False
    assert ledger.reassignment_count() == 1


def test_revoked_reassignment_can_be_reapplied():
    build({"scans": SCANS, "expected": EXPECTED})
    first = reassign({"file_name": "p4_nohdr.tif", "evidence": {"header_issue": "2026-11-03"}})
    assert first["created"] is True

    revoked = reassign({"file_name": "p4_nohdr.tif", "evidence": {"header_issue": "2026-11-01"}})
    assert revoked["created"] is True
    assert revoked["reassignment"]["from_issue"] == "2026-11-03"
    assert revoked["reassignment"]["to_issue"] == "2026-11-01"

    repeated = reassign({"file_name": "p4_nohdr.tif", "evidence": {"header_issue": "2026-11-01"}})
    assert repeated["created"] is False

    reapplied = reassign({"file_name": "p4_nohdr.tif", "evidence": {"header_issue": "2026-11-03"}})
    assert reapplied["created"] is True and ledger.reassignment_count() == 3


def test_request_key_cannot_retarget():
    build({"scans": SCANS, "expected": EXPECTED})
    reassign({"file_name": "p3_blur.tif", "evidence": {"header_issue": "2026-11-02"}, "request_key": "fixed"})
    with pytest.raises(DomainError) as caught:
        reassign({"file_name": "p3_blur.tif", "evidence": {"header_issue": "2026-11-04"}, "request_key": "fixed"})
    assert caught.value.code == "VALUE_CONFLICT" and ledger.reassignment_count() == 1


def test_reassignment_survives_rebuild():
    build({"scans": SCANS, "expected": EXPECTED})
    reassign({"file_name": "p3_blur.tif", "evidence": {"header_issue": "2026-11-02"}})
    rebuilt = build({"scans": SCANS, "expected": EXPECTED})
    assert "003" in {item["page"] for item in group(rebuilt["listing"], "2026-11-02")["selected"]}


def test_reassign_same_issue_rejected():
    build({"scans": SCANS, "expected": EXPECTED})
    with pytest.raises(DomainError) as caught:
        reassign({"file_name": "p4_nohdr.tif", "evidence": {"header_issue": "2026-11-01"}})
    assert caught.value.code == "VALUE_CONFLICT"


def test_history_unknown_version_is_404():
    with pytest.raises(DomainError) as caught:
        history("nope")
    assert caught.value.status == 404


def test_missing_expected_keeps_selected_visible():
    result = build({"scans": SCANS, "expected": {}})
    first = group(result["listing"], "2026-11-01")
    assert first["expected_known"] is False and first["expected"] is None
    assert [item["page"] for item in first["selected"]] == ["001", "002"]


# --- 边界：不猜、不覆盖、不串期 ------------------------------------------


def test_scan_list_without_expected_does_not_borrow_sample_counts():
    result = build({"scans": SCANS})
    assert all(group["expected"] is None for group in result["listing"]["groups"])


def test_blank_header_never_counts_as_consistent():
    scan = normalize_scans_raw([{"issue": "2026-11-01", "edition": "01", "page": "001", "header_issue": ""}])[0]
    assert scan["attribution"]["status"] == "UNKNOWN"


def test_unknown_file_name_is_not_silently_matched():
    build({"scans": SCANS, "expected": EXPECTED})
    with pytest.raises(DomainError) as caught:
        reassign({"file_name": "scan_dup.tif", "evidence": {"header_issue": "2026-11-02"}})
    assert caught.value.code == "UNKNOWN_REFERENCE"


def test_duplicate_file_name_requires_digest():
    twin = {**SCANS[0], "sha256": "f" * 64}
    rows = [SCANS[0], twin]
    build({"scans": rows, "expected": {"2026-11-01|01": 1}})
    with pytest.raises(DomainError) as caught:
        reassign({"scans": rows, "file_name": "p1.tif", "evidence": {"header_issue": "2026-11-02"}})
    assert caught.value.code == "DUPLICATE_KEY"


def test_page_numbers_stay_three_digit_strings():
    listing = build({"scans": SCANS, "expected": EXPECTED})["listing"]
    for group in listing["groups"]:
        for item in [*group["missing"], *group["held"]]:
            assert isinstance(item["page"], str) and len(item["page"]) == 3


def test_versioned_listing_keeps_old_snapshot():
    first = build({"scans": SCANS, "expected": EXPECTED})
    second = build({"scans": SCANS, "expected": {"2026-11-01|01": 6}})
    assert second["version_id"] != first["version_id"]
    assert history(first["version_id"])["listing"]["totals"]["expected"] == 8
