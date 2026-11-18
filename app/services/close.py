import sqlite3

from app.config import Config
from app.db import (
    CHIP_RETAIN,
    CHIP_WIPE,
    CLOSED,
    COLLECTING,
    INTERRUPTED,
    OBJECT_CLOSED,
    REGISTERED,
    connect,
)
from app.services.objects import ServiceError
from app.services.reconcile import reconcile_batch


def _record_run(
    db_path: str | None,
    batch_id: int,
    object_count: int,
    ok: int,
    outcome: str,
    resumed: int,
    detail: str,
) -> None:
    """独立短事务留痕：主事务即使整笔回滚，抹掉/失败痕迹仍在。"""
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO close_runs(batch_id, object_count, ok, outcome, policy, resumed, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (batch_id, object_count, ok, outcome, Config.CLOSE_POLICY, resumed, detail),
        )
        conn.commit()
    finally:
        conn.close()


def close_batch(batch_id: int, db_path: str | None = None) -> dict:
    """继续闭合：钉死“留存补缺口”单轨，一把写事务一次做完。

    - 收集中批次首闭：全部已登记对象翻已闭合后才升批次态
    - 闭合中断/漂移批次：已闭合段原样留存，只补翻缺口
    - 失败整笔回滚（当笔写入全抹），close_runs 留“抹掉”痕
    - BEGIN IMMEDIATE 抢写锁 + 状态复查，两名仓管同点不会双写
    """
    conn = connect(db_path)
    try:
        # 立即拿写锁：第二个并发请求在这阻塞，超时后抛 OperationalError
        conn.execute("BEGIN IMMEDIATE")

        batch = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            conn.rollback()
            raise ServiceError("批次不存在", 404)

        status = batch["status"]
        if status == CLOSED:
            # 已闭合也要核齐：真闭合拒重复；半挂漂移转补缺口
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM landing_objects "
                "WHERE batch_id=? AND status=?",
                (batch_id, REGISTERED),
            ).fetchone()["c"]
            if pending == 0:
                conn.rollback()
                raise ServiceError("批次已闭合", 409)
        elif status not in (COLLECTING, INTERRUPTED):
            conn.rollback()
            raise ServiceError("仅收集中或闭合中断批次可闭合", 409)

        objs = conn.execute(
            "SELECT id FROM landing_objects WHERE batch_id=? AND status=?",
            (batch_id, REGISTERED),
        ).fetchall()
        if not objs:
            conn.rollback()
            raise ServiceError("空批不可闭合", 400)

        # 核齐校验：已闭合段必须与固化数对得上，漂了拒，不带着歧义收口
        already_closed = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_objects "
            "WHERE batch_id=? AND status=?",
            (batch_id, OBJECT_CLOSED),
        ).fetchone()["c"]
        stored = batch["closed_object_count"]
        if stored is not None and stored != already_closed:
            conn.rollback()
            _record_run(
                db_path, batch_id, len(objs), 0, CHIP_WIPE,
                1 if status == INTERRUPTED else 0,
                "对象态与批次态漂了，收口拒绝",
            )
            raise ServiceError("对象态与批次态不一致，已拒绝收口", 409)

        resumed = 1 if status == INTERRUPTED or already_closed > 0 else 0
        # 补缺口：只翻仍挂已登记的对象，已闭合段原样留存
        conn.executemany(
            "UPDATE landing_objects SET status=? WHERE id=? AND status=?",
            [(OBJECT_CLOSED, r["id"], REGISTERED) for r in objs],
        )
        final_closed = already_closed + len(objs)
        conn.execute(
            "UPDATE landing_batches SET status=?, closed_object_count=? WHERE id=?",
            (CLOSED, final_closed, batch_id),
        )
        cur = conn.execute(
            "INSERT INTO close_runs(batch_id, object_count, ok, outcome, policy, resumed, detail) "
            "VALUES (?,?,1,?,?,?,?)",
            (
                batch_id,
                final_closed,
                CHIP_RETAIN,
                Config.CLOSE_POLICY,
                resumed,
                f"留存补缺口：本次补 {len(objs)} 个，已闭合段 {final_closed} 个"
                if resumed
                else f"闭合 {final_closed} 个对象",
            ),
        )
        run = conn.execute(
            "SELECT * FROM close_runs WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        conn.commit()
        return dict(run)
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        locked = "locked" in msg or "busy" in msg
        if not locked:
            # 非锁类操作错误：与其它意外同等待遇，整笔回滚 + 抹掉留痕
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            _record_run(
                db_path, batch_id, 0, 0, CHIP_WIPE, 0,
                f"收口失败已整笔回滚：{exc}",
            )
            raise ServiceError("收口失败，已整笔回滚", 500) from exc
        # 写锁被另一笔收口持有：双写在这被挡死
        conn.rollback()
        raise ServiceError("另一笔收口正在执行，请稍后再试", 409) from exc
    except ServiceError:
        raise
    except Exception as exc:  # 任何意外：整笔回滚，半截写入一抹干净
        conn.rollback()
        _record_run(
            db_path, batch_id, 0, 0, CHIP_WIPE, 0, f"收口失败已整笔回滚：{exc}"
        )
        raise ServiceError("收口失败，已整笔回滚", 500) from exc
    finally:
        conn.close()
