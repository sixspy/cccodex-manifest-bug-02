"""收口事务边界测试：钉死“留存补缺口”单轨。

覆盖：
1. 正常闭合一次性翻齐全部对象（植入 bug 回归）
2. 半截态（闭合中断）继续闭合只补缺口，已闭合段留存
3. 对象态/批次态漂了直接拒，且不产生新写入
4. 两名仓管同点继续：写锁互斥，无双写
5. 目录扫描半截保留已登记段，再扫补缺口，最后收口同一结局
6. 收口芯片只有 留存/抹掉，页面四处读同一份 reconcile
"""
import os
import sqlite3
import threading

import pytest

from app.db import (
    CHIP_RETAIN,
    CHIP_WIPE,
    CLOSED,
    COLLECTING,
    INTERRUPTED,
    OBJECT_CLOSED,
    REGISTERED,
    connect,
    init_db,
)
from app.services import close_batch, reconcile_batch, scan_directory
from app.services.objects import ServiceError, register_object


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "edge.db")
    os.environ["DATABASE_PATH"] = path
    init_db(path)
    return path


def _new_collecting_batch(db_path, zone="zone_x", n=4):
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO landing_batches(zone_code, zone_name, biz_date, status) "
            "VALUES (?,?,?,?)",
            (zone, "试验区", "2026-11-17", COLLECTING),
        )
        bid = cur.lastrowid
    for i in range(n):
        register_object(bid, f"k-{i}.parquet", f"h-{i}", 100 + i, db_path)
    return bid


def _raw(db_path, sql, args=()):
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def test_full_close_flips_every_object(db_path):
    bid = _new_collecting_batch(db_path)
    run = close_batch(bid, db_path)
    assert run["outcome"] == CHIP_RETAIN
    states = {
        r["status"]
        for r in _raw(
            db_path,
            "SELECT status FROM landing_objects WHERE batch_id=?",
            (bid,),
        )
    }
    assert states == {OBJECT_CLOSED}
    rc = reconcile_batch(bid, db_path)
    assert rc["drifted"] is False
    assert rc["progress_pct"] == 100
    assert rc["chip"] == CHIP_RETAIN


def test_interrupted_resume_keeps_closed_segment(db_path):
    bid = _new_collecting_batch(db_path, n=5)
    # 模拟闭合中断：批次挂闭合中断，前 2 个已闭合、后 3 个仍登记
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE landing_batches SET status=?, closed_object_count=2 WHERE id=?",
            (INTERRUPTED, bid),
        )
        conn.execute(
            "UPDATE landing_objects SET status=? WHERE batch_id=? AND id IN "
            "(SELECT id FROM landing_objects WHERE batch_id=? ORDER BY id LIMIT 2)",
            (OBJECT_CLOSED, bid, bid),
        )

    before = _raw(
        db_path,
        "SELECT id, status FROM landing_objects WHERE batch_id=? AND status=?",
        (bid, OBJECT_CLOSED),
    )
    run = close_batch(bid, db_path)
    after = _raw(
        db_path,
        "SELECT id, status FROM landing_objects WHERE batch_id=? AND status=?",
        (bid, OBJECT_CLOSED),
    )
    # 已闭合段原样留存，只是补上缺口
    assert [r["id"] for r in before] == [r["id"] for r in after[:2]]
    assert run["resumed"] == 1
    assert run["outcome"] == CHIP_RETAIN

    rc = reconcile_batch(bid, db_path)
    assert rc["batch_status"] == CLOSED
    assert rc["closed_count"] == 5
    assert rc["registered_count"] == 0
    assert rc["drifted"] is False


def test_drifted_batch_is_rejected_without_partial_write(db_path):
    bid = _new_collecting_batch(db_path, n=4)
    # 漂移：批次自称已闭合、固化数 4，实际只有 1 个对象闭合
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE landing_batches SET status=?, closed_object_count=4 WHERE id=?",
            (CLOSED, bid),
        )
        conn.execute(
            "UPDATE landing_objects SET status=? WHERE batch_id=? ORDER BY id LIMIT 1",
            (OBJECT_CLOSED, bid),
        )
    with pytest.raises(ServiceError) as ei:
        close_batch(bid, db_path)
    assert ei.value.status_code == 409
    # 抹掉芯片留痕，对象态一个都没动
    last = _raw(
        db_path,
        "SELECT * FROM close_runs WHERE batch_id=? ORDER BY id DESC LIMIT 1",
        (bid,),
    )[0]
    assert last["outcome"] == CHIP_WIPE
    assert last["ok"] == 0
    rc = reconcile_batch(bid, db_path)
    assert rc["drifted"] is True
    assert rc["closed_count"] == 1
    assert rc["registered_count"] == 3


def test_concurrent_resume_no_double_write(db_path):
    bid = _new_collecting_batch(db_path, n=6)
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE landing_batches SET status=?, closed_object_count=3 WHERE id=?",
            (INTERRUPTED, bid),
        )
        conn.execute(
            "UPDATE landing_objects SET status=? WHERE batch_id=? AND id IN "
            "(SELECT id FROM landing_objects WHERE batch_id=? ORDER BY id LIMIT 3)",
            (OBJECT_CLOSED, bid, bid),
        )

    errors = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        try:
            close_batch(bid, db_path)
        except ServiceError as exc:
            errors.append((exc.status_code, exc.message))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(10); t2.join(10)

    # 恰好一个成功、一个被锁/状态挡回
    assert len(errors) == 1
    assert errors[0][0] == 409
    rc = reconcile_batch(bid, db_path)
    assert rc["batch_status"] == CLOSED
    assert rc["closed_count"] == 6
    assert rc["registered_count"] == 0
    # 没有重复 close_run 成功记录
    succ = _raw(
        db_path,
        "SELECT COUNT(*) AS c FROM close_runs WHERE batch_id=? AND ok=1",
        (bid,),
    )[0]["c"]
    assert succ == 1


def test_scan_halfway_then_resume_same_outcome(db_path, tmp_path, monkeypatch):
    bid = _new_collecting_batch(db_path, zone="zone_scan", n=0)
    root = tmp_path / "landing"
    (root / "sub").mkdir(parents=True)
    for name in ("a.parquet", "b.parquet", "sub/c.parquet"):
        p = root / name
        p.write_bytes(name.encode())
    # 中间文件不可读 → 扫描在该文件中断，前后已登记段保留
    bad = root / "b.parquet"
    bad.chmod(0o000)
    monkeypatch.setenv("SCAN_ROOT", str(root))

    run = scan_directory(bid, "", db_path)
    assert run["status"] == "中断"
    assert run["ingested_count"] == 2
    bad.chmod(0o644)

    rc = reconcile_batch(bid, db_path)
    assert rc["half_written"] is True
    assert rc["registered_count"] == 2

    # 再扫一次补缺口（已登记文件跳过）
    run2 = scan_directory(bid, "", db_path)
    assert run2["status"] == "扫完"
    assert run2["ingested_count"] == 1
    assert run2["skipped_count"] == 2

    close = close_batch(bid, db_path)
    assert close["outcome"] == CHIP_RETAIN
    rc = reconcile_batch(bid, db_path)
    assert rc["batch_status"] == CLOSED
    assert rc["closed_count"] == 3
    assert rc["progress_pct"] == 100
    assert rc["drifted"] is False


def test_close_failure_leaves_wipe_chip_and_rolls_back(db_path, monkeypatch):
    bid = _new_collecting_batch(db_path, n=3)
    from app.services import close as close_mod

    real_connect = close_mod.connect

    class FaultConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args):
            if sql.startswith("UPDATE landing_objects"):
                raise sqlite3.OperationalError("injected fault")
            return self._inner.execute(sql, *args)

        def executemany(self, sql, params):
            if sql.startswith("UPDATE landing_objects"):
                raise sqlite3.OperationalError("injected fault")
            return self._inner.executemany(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def faulty_connect(path=None):
        return FaultConn(real_connect(path))

    monkeypatch.setattr(close_mod, "connect", faulty_connect)
    with pytest.raises(ServiceError) as ei:
        close_batch(bid, db_path)
    assert ei.value.status_code == 500
    monkeypatch.undo()

    # 整笔回滚：对象全部仍是已登记，批次仍收集中
    rc = reconcile_batch(bid, db_path)
    assert rc["batch_status"] == COLLECTING
    assert rc["registered_count"] == 3
    assert rc["closed_count"] == 0
    last = _raw(
        db_path,
        "SELECT * FROM close_runs WHERE batch_id=? ORDER BY id DESC LIMIT 1",
        (bid,),
    )[0]
    assert last["outcome"] == CHIP_WIPE

    # 同一批还能重新收口成功
    run = close_batch(bid, db_path)
    assert run["outcome"] == CHIP_RETAIN
    assert reconcile_batch(bid, db_path)["progress_pct"] == 100
