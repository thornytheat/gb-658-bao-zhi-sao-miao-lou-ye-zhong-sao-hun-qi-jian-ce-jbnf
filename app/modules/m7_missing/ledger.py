import json
import sqlite3
from datetime import datetime, timezone
from hashlib import sha256

from app.config import DATABASE_PATH, RULE_VERSION

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS listing_versions (
        version_id TEXT PRIMARY KEY,
        ordinal INTEGER NOT NULL,
        input_sha256 TEXT NOT NULL,
        input_json TEXT NOT NULL,
        listing_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    # 追加式改期事件：一件扫描的当前归属取最新事件；重试按当前状态判定，不靠唯一键。
    """CREATE TABLE IF NOT EXISTS reassignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_key TEXT NOT NULL,
        scan_key TEXT NOT NULL,
        from_issue TEXT NOT NULL,
        edition TEXT NOT NULL,
        page TEXT NOT NULL,
        file_name TEXT NOT NULL,
        to_issue TEXT NOT NULL,
        version_id TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_reassignments_scan ON reassignments (scan_key, id)",
    "CREATE INDEX IF NOT EXISTS idx_reassignments_request ON reassignments (request_key)",
)


def connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def init_db() -> None:
    with connect() as db:
        for statement in SCHEMA:
            db.execute(statement)


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def version_id(raw_scans: list[dict], expected: dict, overrides: dict) -> str:
    payload = {"rule_version": RULE_VERSION, "scans": raw_scans, "expected": expected, "overrides": overrides}
    return f"{RULE_VERSION}-{sha256(canonical(payload).encode()).hexdigest()[:12]}"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _version(row) -> dict:
    return {
        "version_id": row["version_id"],
        "ordinal": row["ordinal"],
        "input": json.loads(row["input_json"]),
        "listing": json.loads(row["listing_json"]),
        "created_at": row["created_at"],
    }


def latest_version() -> dict | None:
    init_db()
    with connect() as db:
        row = db.execute("SELECT * FROM listing_versions ORDER BY ordinal DESC LIMIT 1").fetchone()
    return _version(row) if row is not None else None


def get_version(target: str) -> dict | None:
    init_db()
    with connect() as db:
        row = db.execute("SELECT * FROM listing_versions WHERE version_id = ?", (target,)).fetchone()
    return _version(row) if row is not None else None


def save_listing(manifest_input: dict, listing: dict) -> dict:
    """同一输入（含规则版本）只落一版清单；重复出单复用旧版，不新增快照。"""
    init_db()
    target = version_id(manifest_input["raw_scans"], manifest_input["expected"], manifest_input["overrides"])
    input_text = canonical(manifest_input)
    input_sha = sha256(input_text.encode()).hexdigest()
    with connect() as db:
        existing = db.execute("SELECT * FROM listing_versions WHERE version_id = ?", (target,)).fetchone()
        if existing is not None:
            return {"version_id": target, "ordinal": existing["ordinal"], "reused": True, "created_at": existing["created_at"]}
        ordinal = db.execute("SELECT COALESCE(MAX(ordinal), 0) + 1 FROM listing_versions").fetchone()[0]
        created_at = _now()
        db.execute(
            "INSERT INTO listing_versions VALUES (?, ?, ?, ?, ?, ?)",
            (target, ordinal, input_sha, input_text, canonical(listing), created_at),
        )
    return {"version_id": target, "ordinal": ordinal, "reused": False, "created_at": created_at}


def find_request(request_key: str) -> dict | None:
    """同一请求只认一次：按请求键找到已落地的改期事件即视为重试。"""
    init_db()
    with connect() as db:
        row = db.execute(
            """SELECT r.*, v.ordinal AS ordinal FROM reassignments r
               LEFT JOIN listing_versions v ON v.version_id = r.version_id
               WHERE r.request_key = ? ORDER BY r.id DESC LIMIT 1""",
            (request_key,),
        ).fetchone()
    return dict(row) if row is not None else None


def current_reassignment(scan_key: str) -> dict | None:
    init_db()
    with connect() as db:
        row = db.execute(
            """SELECT r.*, v.ordinal AS ordinal FROM reassignments r
               LEFT JOIN listing_versions v ON v.version_id = r.version_id
               WHERE r.scan_key = ? ORDER BY r.id DESC LIMIT 1""",
            (scan_key,),
        ).fetchone()
    return dict(row) if row is not None else None


def overrides_for(scan_keys: list[str]) -> dict[str, str]:
    """每件扫描取最新一次改期，重出清单必须带着已生效的归属。"""
    if not scan_keys:
        return {}
    init_db()
    placeholders = ",".join("?" for _ in scan_keys)
    with connect() as db:
        rows = db.execute(
            f"""SELECT r.scan_key, r.to_issue FROM reassignments r
                JOIN (SELECT scan_key, MAX(id) AS id FROM reassignments
                      WHERE scan_key IN ({placeholders}) GROUP BY scan_key) latest
                  ON latest.id = r.id""",
            tuple(scan_keys),
        ).fetchall()
    return {row["scan_key"]: row["to_issue"] for row in rows}


def apply_reassignment(record: dict, manifest_input: dict, listing: dict, fault=None) -> dict:
    """改期与新版清单在同一事务内提交：任一步失败则回滚，旧版清单与改期事件都不落地。"""
    init_db()
    target = version_id(manifest_input["raw_scans"], manifest_input["expected"], manifest_input["overrides"])
    input_text = canonical(manifest_input)
    input_sha = sha256(input_text.encode()).hexdigest()
    with connect() as db:
        version = db.execute("SELECT ordinal FROM listing_versions WHERE version_id = ?", (target,)).fetchone()
        if version is None:
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal), 0) + 1 FROM listing_versions").fetchone()[0]
            db.execute(
                "INSERT INTO listing_versions VALUES (?, ?, ?, ?, ?, ?)",
                (target, ordinal, input_sha, input_text, canonical(listing), _now()),
            )
        else:
            ordinal = version["ordinal"]
        created_at = _now()
        db.execute(
            """INSERT INTO reassignments
               (request_key, scan_key, from_issue, edition, page, file_name, to_issue, version_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["request_key"],
                record["scan_key"],
                record["from_issue"],
                record["edition"],
                record["page"],
                record["file_name"],
                record["to_issue"],
                target,
                created_at,
            ),
        )
        if fault is not None:
            fault(db)
    return {
        "created": True,
        "record": {**record, "version_id": target, "created_at": created_at},
        "version_id": target,
        "ordinal": ordinal,
    }


def reassignment_count() -> int:
    init_db()
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM reassignments").fetchone()[0]
