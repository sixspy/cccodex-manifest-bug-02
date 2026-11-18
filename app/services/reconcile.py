"""对象态 / 批次态核齐对账：对象进度、闭合卡片、批次附页、扫描台共用一份结局。"""
from app.db import (
    CLOSED,
    COLLECTING,
    INTERRUPTED,
    OBJECT_CLOSED,
    REGISTERED,
    get_db,
)


def reconcile_batch(batch_id: int, db_path: str | None = None) -> dict:
    """返回批次收口视角的唯一真相，任何页面不得自行另算。"""
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            return {}
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM landing_objects "
            "WHERE batch_id=? GROUP BY status",
            (batch_id,),
        ).fetchall()
        by_status = {r["status"]: r["c"] for r in rows}
        registered_n = by_status.get(REGISTERED, 0)
        closed_n = by_status.get(OBJECT_CLOSED, 0)
        total = registered_n + closed_n + by_status.get("已作废", 0)

        stored_count = batch["closed_object_count"]
        status = batch["status"]

        # 漂移定义：批次自称已闭合，却仍挂着已登记对象；或固化数与已闭合段对不上
        drifted = False
        drift_reasons = []
        if status == CLOSED and registered_n > 0:
            drifted = True
            drift_reasons.append("批次已闭合但仍有对象挂已登记")
        if stored_count is not None and stored_count != closed_n and status == CLOSED:
            drifted = True
            drift_reasons.append("固化对象数与已闭合段不一致")
        half_written = status == INTERRUPTED or (
            status == COLLECTING and closed_n > 0 and registered_n > 0
        )

        last_run = conn.execute(
            "SELECT * FROM close_runs WHERE batch_id=? ORDER BY id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        last_scan = conn.execute(
            "SELECT * FROM scan_runs WHERE batch_id=? ORDER BY id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()

        progress_pct = round(closed_n * 100 / total) if total else 0
        return {
            "batch_id": batch_id,
            "batch_status": status,
            "total": total,
            "registered_count": registered_n,
            "closed_count": closed_n,
            "stored_closed_count": stored_count,
            "progress_pct": progress_pct,
            "half_written": half_written,
            "drifted": drifted,
            "drift_reasons": drift_reasons,
            "resumable": (status == INTERRUPTED) or drifted,
            "chip": dict(last_run)["outcome"] if last_run else None,
            "last_close_run": dict(last_run) if last_run else None,
            "last_scan_run": dict(last_scan) if last_scan else None,
        }
