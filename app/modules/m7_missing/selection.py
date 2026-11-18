from app.modules.m3_pages.sequence import inspect_pages
from app.modules.m4_rescans.decision import choose_scan


def select_page(scans: list[dict]) -> tuple[dict, list[dict]]:
    """同一页多件时只留一件：先按版本取新版（陈旧重扫不得覆盖新版），
    再用 M4 的清晰度、拍摄时间、文件名稳定择优；结果与输入顺序无关。"""
    if not scans:
        raise ValueError("EMPTY_GROUP")
    newest = max(scan["version"] for scan in scans)
    contenders = [scan for scan in scans if scan["version"] == newest]
    chosen = choose_scan(contenders)
    superseded = [scan for scan in scans if scan is not chosen]
    superseded.sort(key=lambda scan: (scan["page"], scan["version"], scan["clarity"], scan["captured_at"], scan["file_name"]))
    return chosen, superseded


def select_pages(scans: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for scan in scans:
        grouped.setdefault(scan["page"], []).append(scan)
    selected: dict[str, dict] = {}
    superseded: list[dict] = []
    for page in sorted(grouped):
        chosen, dropped = select_page(grouped[page])
        selected[page] = chosen
        superseded.extend(dropped)
    return selected, superseded


def coverage(selected: dict[str, dict], expected_count: int | None) -> str:
    if not expected_count or expected_count <= 0:
        return "0.000"
    return f"{len(selected) / expected_count:.3f}"


def page_status(selected: dict[str, dict], expected_count: int | None) -> dict:
    """复用 M3 页序检查；页码对外始终是三位字符串。"""
    if expected_count is None:
        return {"missing": [], "duplicates": [], "unexpected": []}
    checked = inspect_pages(list(selected), expected_count)
    return {
        "missing": [f"{n:03d}" for n in checked["missing"]],
        "duplicates": [f"{n:03d}" for n in checked["duplicates"]],
        "unexpected": [f"{n:03d}" for n in checked["unexpected"]],
    }


def expected_set(count: int) -> list[str]:
    return expected_pages(count)
