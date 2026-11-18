from fastapi.testclient import TestClient
from app.main import app


def test_all_module_pages_accessible():
    client = TestClient(app)
    for path in ("/intake", "/issues", "/pages", "/rescans", "/mixed", "/batches", "/missing"):
        response = client.get(path)
        assert response.status_code == 200 and "panel" in response.text

def test_full_sample_flow_has_business_results():
    client = TestClient(app)
    endpoints = ("/api/scans/import", "/api/issues/normalize", "/api/pages/check", "/api/rescans/evaluate", "/api/mixed/evaluate", "/api/reconcile")
    results = [client.post(path).json() for path in endpoints]
    assert results[0]["accepted"] == 2 and results[2]["missing"] == [3] and results[4]["decision"] == "MIXED_ISSUE"

def test_export_matches_page_evidence():
    client = TestClient(app)
    response = client.get("/api/export/evidence.csv")
    assert response.status_code == 200 and "MISSING_PAGE" in response.text



def test_missing_page_endpoints_work_together(tmp_path, monkeypatch):
    import app.modules.m7_missing.ledger as ledger

    monkeypatch.setattr(ledger, "DATABASE_PATH", tmp_path / "e2e-m7.db")
    client = TestClient(app)

    listing = client.post("/api/missing/list", json={}).json()
    first = listing["listing"]["groups"][0]
    assert listing["version_id"] and "004" in {item["page"] for item in first["held"]}

    moved = client.post("/api/missing/reassign", json={"file_name": "scan_004.tif", "evidence": {"header_issue": "2026-11-02"}}).json()
    assert moved["created"] is True and moved["version_id"] != listing["version_id"]
    assert [item["issue"] for item in moved["recomputed"]] == ["2026-11-01", "2026-11-02"]

    retry = client.post("/api/missing/reassign", json={"file_name": "scan_004.tif", "evidence": {"header_issue": "2026-11-02"}}).json()
    assert retry["created"] is False and retry["version_id"] == moved["version_id"]

    exported = client.get("/api/export/missing.csv")
    assert exported.status_code == 200 and "WAIT" in exported.text and "RESCAN" in exported.text


def test_missing_reassign_error_is_structured(tmp_path, monkeypatch):
    import app.modules.m7_missing.ledger as ledger

    monkeypatch.setattr(ledger, "DATABASE_PATH", tmp_path / "e2e-err.db")
    client = TestClient(app)
    client.post("/api/missing/list", json={})
    response = client.post("/api/missing/reassign", json={"file_name": "ghost.tif", "evidence": {"header_issue": "2026-11-02"}})
    assert response.status_code == 404 and response.json()["detail"]["code"] == "UNKNOWN_REFERENCE"
