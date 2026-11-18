"""目录扫描台：把落地目录里的文件逐个登记为批次对象。

与收口同走“留存补缺口”轨：每个文件独立短事务，扫到半截时
已登记段保留，批次挂“闭合中断”，再扫/继续闭合即补缺口。
"""
import hashlib
from pathlib import Path

from app.config import Config
from app.db import (
    COLLECTING,
    INTERRUPTED,
    REGISTERED,
    connect,
    get_db,
)
from app.services.objects import ServiceError

SCAN_DONE = "扫完"
SCAN_INTERRUPTED = "中断"


def _resolve_scan_dir(rel_dir: str) -> Path:
    root = Path(Config.scan_root()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = (root / (rel_dir or "")).resolve()
    # 禁止逃出扫描根
    if root != target and root not in target.parents:
        raise ServiceError("扫描路径越界，拒绝", 400)
    if not target.exists() or not target.is_dir():
        raise ServiceError("扫描目录不存在", 404)
    return target


def _scan_files(target: Path) -> list[Path]:
    return sorted(p for p in target.rglob("*") if p.is_file())


def scan_directory(
    batch_id: int, rel_dir: str = "", db_path: str | None = None
) -> dict:
    target = _resolve_scan_dir(rel_dir)

    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] not in (COLLECTING, INTERRUPTED):
            raise ServiceError("仅收集中或闭合中断批次可扫描入库", 409)

    files = _scan_files(target)
    if not files:
        raise ServiceError("扫描目录下没有文件", 400)

    total = len(files)
    ingested = 0
    skipped = 0
    failures: list[str] = []

    for f in files:
        rel_key = str(f.relative_to(target))
        # 逐文件独立事务：半截失败时前面的登记一个不回滚
        conn = connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            dup = conn.execute(
                "SELECT id FROM landing_objects WHERE batch_id=? AND object_key=?",
                (batch_id, rel_key),
            ).fetchone()
            if dup:
                skipped += 1
                conn.rollback()
                continue
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            size = f.stat().st_size
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                "VALUES (?,?,?,?,?)",
                (batch_id, rel_key, digest, size, REGISTERED),
            )
            ingested += 1
            conn.commit()
        except OSError as exc:
            conn.rollback()
            failures.append(f"{rel_key}：{exc.strerror or '读失败'}")
        finally:
            conn.close()

    interrupted = bool(failures)
    # 半截写入的统一结局：批次挂闭合中断，等待继续闭合补缺口
    with get_db(db_path) as conn:
        if interrupted:
            conn.execute(
                "UPDATE landing_batches SET status=? WHERE id=? AND status=?",
                (INTERRUPTED, batch_id, COLLECTING),
            )
        cur = conn.execute(
            "INSERT INTO scan_runs(batch_id, scan_root, total_files, ingested_count, "
            "skipped_count, status, detail) VALUES (?,?,?,?,?,?,?)",
            (
                batch_id,
                str(target),
                total,
                ingested,
                skipped,
                SCAN_INTERRUPTED if interrupted else SCAN_DONE,
                "；".join(failures)[:500],
            ),
        )
        run = conn.execute(
            "SELECT * FROM scan_runs WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(run)


def list_scan_runs(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM scan_runs ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
